import json
import os
from datetime import datetime
from pathlib import Path

from python_on_whales import docker
from python_on_whales.exceptions import DockerException

from config import settings
from loggers.logger import get_logger, PrefixedLogger
from validator.evaluator import AgentEvaluator
from validator.models.platform import AgentExecution, Status
from validator.platform_client import PlatformError


logger = get_logger()

SANDBOX_CONTAINER_TMPL = "bitsec_sandbox_{job_run_id}_{project_key}"
PROJECT_IMAGE_TAG_TMPL = "ghcr.io/bitsec-ai/{project_key}:latest"


class AgentExecutor:
    def __init__(
        self,
        job_run,
        agent_filepath,
        project_key,
        job_run_reports_dir,
        platform_client,
        execution_api_key=None,
        eval_max_vulns=100,
    ):
        self.job_run = job_run
        self.agent_filepath = agent_filepath
        self.project_key = project_key
        self.job_run_reports_dir = job_run_reports_dir
        self.platform_client = platform_client
        self.execution_api_key = execution_api_key
        self.eval_max_vulns = eval_max_vulns

        self.project_report_dir = os.path.join(self.job_run_reports_dir, f"{self.project_key}")
        os.makedirs(self.project_report_dir, exist_ok=True)

        self.agent_execution_id: int | None = None
        self.agent_evaluation_id: int | None = None
        self.started_at = None

        self.init_logger()

    def init_logger(self):
        prefix = f"[A:{self.job_run.agent_id}|JR:{self.job_run.id}|P:{self.project_key}] "

        self.logger = PrefixedLogger(logger, prefix)

    def remove_container(self, container_name):
        try:
            docker.remove(container_name, force=True)

        except DockerException as e:
            self.logger.error(f"Exit code {e.return_code} while running {e.docker_command}")
            raise

    def pull_latest_image(self, image_tag):
        """
        Pull the latest image.
        """
        try:
            self.logger.info(f"Pulling latest image: {image_tag}")
            docker.pull(image_tag, quiet=True)
            self.logger.info(f"Image {image_tag} is up-to-date")
        except DockerException:
            self.logger.warning(f"Failed to pull image {image_tag} Will attempt to use local image if available.")

    def run(self):
        if not settings.skip_execution:
            self.run_execution()
            self.agent_execution_id = self.submit_agent_execution()

        if not settings.skip_evaluation:
            self.eval_job_run()

    def run_execution(self):
        self.started_at = datetime.utcnow()
        self.run_project()

    def run_project(self):
        sandbox_container = SANDBOX_CONTAINER_TMPL.format(
            job_run_id=self.job_run.id,
            project_key=self.project_key,
        )

        # clear any previous container runs
        self.remove_container(sandbox_container)

        project_image_tag = PROJECT_IMAGE_TAG_TMPL.format(project_key=self.project_key)

        # pull the latest image
        self.pull_latest_image(project_image_tag)

        self.logger.info("Starting container")
        container = docker.run(
            project_image_tag,
            name=sandbox_container,
            networks=[settings.proxy_network],
            volumes=[
                (self.agent_filepath, "/app/agent.py"),
            ],
            envs={
                "AGENT_ID": str(self.job_run.agent_id),
                "JOB_RUN_ID": str(self.job_run.id),
                "PROJECT_KEY": self.project_key,
                "INFERENCE_API_KEY": self.execution_api_key,
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
        report_filepath = os.path.join(self.project_report_dir, "report.json")
        if not Path(report_filepath).is_file():
            self.logger.error("Report not found")
            return None  # TODO: submit with error

        with open(report_filepath, "r", encoding="utf-8") as f:
            report_dict = json.load(f)

        report_dict["validator_id"] = self.job_run.validator_id
        report_dict["job_run_id"] = self.job_run.id
        report_dict["project"] = self.project_key
        report_dict["started_at"] = self.started_at
        report_dict["completed_at"] = datetime.utcnow()

        if "report" not in report_dict and report_dict.get("error") == "Agent timeout":
            report_dict["status"] = Status.TIMED_OUT

        elif isinstance(report_dict.get("report"), dict) and report_dict["report"].get("vulnerabilities") is not None:
            report_dict["status"] = Status.SUCCESS

        else:
            report_dict["status"] = Status.ERROR
            report_dict["report"] = {
                "report_parsing_error": str(report_dict.get("report", {})),
                "vulnerabilities": [],
            }

        agent_execution = AgentExecution.model_validate(report_dict)

        try:
            resp = self.platform_client.submit_agent_execution(agent_execution)

            execution_id = resp.get("id")
            if not execution_id:
                self.logger.warning("Execution ID not received")

            return execution_id

        except PlatformError as e:
            self.logger.exception(f"Platform submission failed for agent execution: {e}")
            return None

    def eval_job_run(self):
        """
        Evaluate a single report.json using ScaBenchScorerV2.
        """
        report_file = Path(self.job_run_reports_dir) / self.project_key / "report.json"

        if not report_file.exists():
            self.logger.error(f"Report not found: {report_file}")
            return {"status": Status.ERROR, "error": "report.json not found"}

        try:
            with open(report_file, "r", encoding="utf-8") as f:
                report_data = json.load(f)
        except Exception as e:
            self.logger.exception("Failed to read report")
            return {"status": Status.ERROR, "error": str(e)}

        evaluator = AgentEvaluator(
            job_run=self.job_run,
            platform_client=self.platform_client,
            agent_execution_id=self.agent_execution_id,
            project_key=self.project_key,
            eval_max_vulns=self.eval_max_vulns,
        )
        scoring_result = evaluator.score_report_data(report_data)

        evaluation_path = os.path.join(self.project_report_dir, "evaluation.json")
        try:
            with open(evaluation_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "agent_execution_id": evaluator.agent_execution_id,
                        "project": self.project_key,
                        "status": str(scoring_result.get("status")),
                        "result": scoring_result.get("result", {}),
                    },
                    f,
                    default=str,
                    indent=2,
                )
            self.logger.info(f"Saved evaluation to {evaluation_path}")
        except Exception as e:
            self.logger.error(f"Failed to write evaluation file: {e}")

        self.agent_evaluation_id = evaluator.agent_evaluation_id
        return scoring_result
