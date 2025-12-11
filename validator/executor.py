import json
import logging
import os
from datetime import datetime
from pathlib import Path

from python_on_whales import docker
from python_on_whales.exceptions import DockerException

from config import settings
from loggers.logger import get_logger, PrefixedLogger
from validator.models.platform import AgentExecution, AgentEvaluation, Status
from validator.platform_client import PlatformError
from validator.scorer import ScaBenchScorerV2


logger = get_logger()

SANDBOX_CONTAINER_TMPL = 'bitsec_sandbox_{job_run_id}_{project_key}'
PROJECT_IMAGE_TAG_TMPL = 'ghcr.io/bitsec-ai/{project_key}:latest'


class AgentExecutor:
    def __init__(
        self,
        job_run,
        agent_filepath,
        project_key,
        job_run_reports_dir,
        platform_client,
    ):
        self.job_run = job_run
        self.agent_filepath = agent_filepath
        self.project_key = project_key
        self.job_run_reports_dir = job_run_reports_dir
        self.platform_client = platform_client

        self.project_report_dir = os.path.join(self.job_run_reports_dir, f"{self.project_key}")
        os.makedirs(self.project_report_dir, exist_ok=True)

        self.agent_execution_id: int | None = None
        self.agent_evaluation_id: int | None = None
        self.started_at = None

        self.init_logger()

    def init_logger(self):
        prefix = f"[J:{self.job_run.job_id}|JR:{self.job_run.id}|P:{self.project_key}] "

        self.logger = PrefixedLogger(logger, prefix)

    def remove_container(self, container_name):
        try:
            docker.remove(container_name, force=True)

        except DockerException as e:
            self.logger.error(f"Exit code {e.return_code} while running {e.docker_command}")
            raise

    def run(self):
        self.started_at = datetime.utcnow()

        if not settings.skip_execution:
            self.run_project()
            self.agent_execution_id = self.submit_agent_execution()

        if not settings.skip_evaluation:
            self.eval_job_run()

    def run_project(self):
        sandbox_container = SANDBOX_CONTAINER_TMPL.format(
            job_run_id=self.job_run.id,
            project_key=self.project_key,
        )

        # clear any previous container runs
        self.remove_container(sandbox_container)

        project_image_tag = PROJECT_IMAGE_TAG_TMPL.format(project_key=self.project_key)

        self.logger.info("Starting container")
        container = docker.run(
            project_image_tag,
            name=sandbox_container,
            networks=[settings.proxy_network],
            volumes=[
                (self.agent_filepath, '/app/agent.py'),
            ],
            envs={
                "JOB_RUN_ID": self.job_run.id,
                "PROJECT_KEY": self.project_key,
            },
            # read_only=True,
            memory="512m",
            cpu_quota=25000,
            pids_limit=64,
            detach=True,
        )
        docker.wait(container)

        try:
            docker.copy((container, "/app/report.json"), self.project_report_dir)
            self.logger.info(f"Finished processing. Report copied: {self.project_key} {self.project_report_dir}")

        except DockerException as e:
            if e.return_code == 1 and "does not exist" in str(e):
                self.logger.error("Report not found in container")
            else:
                raise

        container.remove()

    def submit_agent_execution(self):
        report_filepath = os.path.join(self.project_report_dir, 'report.json')
        if not Path(report_filepath).is_file():
            self.logger.error("Report not found")
            return None # TODO: submit with error

        with open(report_filepath, "r", encoding="utf-8") as f:
            report_dict = json.load(f)

        report_dict['validator_id'] = self.job_run.validator_id
        report_dict['job_run_id'] = self.job_run.id
        report_dict['project'] = self.project_key
        report_dict['status'] = 'success'
        report_dict['started_at'] = self.started_at
        report_dict['completed_at'] = datetime.utcnow()

        agent_execution = AgentExecution.model_validate(report_dict)

        try:
            resp = self.platform_client.submit_agent_execution(agent_execution)

            execution_id = resp.get('id')
            if not execution_id:
                self.logger.warning("Execution ID not received")

            return execution_id

        except PlatformError as e:
            self.logger.exception(f"Platform submission failed for agent execution: {e}")
            return None

    def submit_agent_evaluation(self, project_scoring_results):
        if not self.agent_execution_id:
            self.logger.info("Not running from agent execution. Skipping submit evaluation")
            return None

        scoring_data = {}
        scoring_data['agent_execution_id'] = self.agent_execution_id
        scoring_data['status'] = project_scoring_results['status']
        scoring_data.update(project_scoring_results['result'])

        # Persist evaluation locally for inspection
        evaluation_path = os.path.join(self.project_report_dir, "evaluation.json")
        try:
            with open(evaluation_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "agent_execution_id": scoring_data.get("agent_execution_id"),
                        "project": scoring_data.get("project"),
                        "status": str(scoring_data.get("status")),
                        "result": project_scoring_results.get("result", {}),
                    },
                    f,
                    default=str,
                    indent=2,
                )
            self.logger.info(f"Saved evaluation to {evaluation_path}")
        except Exception as e:
            self.logger.error(f"Failed to write evaluation file: {e}")

        agent_evaluation = AgentEvaluation.model_validate(scoring_data)

        try:
            resp = self.platform_client.submit_agent_evaluation(agent_evaluation)
            evaluation_id = resp.get('id')
            if not evaluation_id:
                self.logger.warning("Evaluation ID not received")

            return evaluation_id

        except PlatformError as e:
            self.logger.exception(f"Platform submission failed for agent evaluation: {e}")
            return None

    def eval_job_run(self):
        """
        Evaluate a single report.json using ScaBenchScorerV2.
        """
        self.logger.info("Starting evaluation")

        report_file = Path(self.job_run_reports_dir) / self.project_key / "report.json"

        if not report_file.exists():
            self.logger.error(f"Report not found: {report_file}")
            return {"status": Status.ERROR, "error": "report.json not found"}

        benchmark_file = os.path.join(settings.validator_dir, "curated-highs-only-2025-08-08.json")
        if not os.path.exists(benchmark_file):
            self.logger.error(f"Benchmark file not found: {benchmark_file}")
            return {"status": Status.ERROR, "error": "benchmark file not found"}

        with open(benchmark_file, "r", encoding="utf-8") as f:
            benchmark_data = json.load(f)

        benchmark_map = {
            e["project_id"]: e.get("vulnerabilities", [])
            for e in benchmark_data
            if e.get("project_id") and e.get("vulnerabilities")
        }

        expected_findings = benchmark_map.get(self.project_key)
        if not expected_findings:
            self.logger.error(f"No benchmark data for project {self.project_key}")
            return {"status": Status.ERROR, "error": "no benchmark data for project"}

        scorer = ScaBenchScorerV2({
            "api_key": settings.chutes_api_key,
            "api_url": settings.proxy_url,
            "debug": True,
            "verbose": True,
            "confidence_threshold": 0.75,
            "strict_matching": False,
        })

        try:
            with open(report_file, "r", encoding="utf-8") as f:
                report_data = json.load(f)

            if not report_data.get("success", False):
                error_msg = report_data.get("error", "Unknown error")
                self.logger.error(f"Agent execution failed: {error_msg}")
                return {
                    "status": Status.ERROR,
                    "error": error_msg,
                    "stdout": report_data.get("stdout", ""),
                    "stderr": report_data.get("stderr", ""),
                }

            # Extract agent findings
            agent_findings = report_data.get("report", {}).get("vulnerabilities", [])

            self.logger.info(
                f"Scoring {self.project_key}: "
                f"{len(expected_findings)} expected vs {len(agent_findings)} found"
            )

            result = scorer.score_project(
                expected_findings=expected_findings,
                tool_findings=agent_findings,
                project_name=self.project_key,
            )

            final_result = "PASS" if result.detection_rate == 1 else "FAIL"

            scoring_result = {
                "status": Status.SUCCESS,
                "result": {
                    "project": result.project,
                    "timestamp": result.timestamp,
                    "total_expected": result.total_expected,
                    "total_found": result.total_found,
                    "true_positives": result.true_positives,
                    "false_negatives": result.false_negatives,
                    "false_positives": result.false_positives,
                    "detection_rate": result.detection_rate,
                    "result": final_result,
                    "precision": result.precision,
                    "f1_score": result.f1_score,
                    "matched_findings": result.matched_findings,
                    "missed_findings": result.missed_findings,
                    "extra_findings": result.extra_findings,
                    "undecided_findings": result.undecided_findings,
                },
            }

            self.agent_evaluation_id = self.submit_agent_evaluation(
                project_scoring_results=scoring_result
            )

            detection_pct = round(result.detection_rate * 100)

            self.logger.info(
                f"Evaluation complete | "
                f"Result: {final_result} | "
                f"Detection: {detection_pct}% | "
                f"Found: {result.true_positives} | "
                f"Expected: {result.total_expected}"
            )
            self.logger.info(
                "Tokens | "
                f"Input: {result.input_tokens} (cached: {result.cached_tokens}) | "
                f"Output: {result.output_tokens}"
            )

            return scoring_result

        except Exception as e:
            self.logger.exception("Evaluation crashed")
            return {"status": Status.ERROR, "error": str(e)}
