import json
import os

from config import settings
from loggers.logger import get_logger, PrefixedLogger
from validator.models.platform import AgentEvaluation, Status
from validator.platform_client import PlatformError
from validator.scorer import ScaBenchScorerV2


logger = get_logger()


class AgentEvaluator:
    def __init__(
        self,
        job_run,
        platform_client,
        agent_execution_id: int,
        project_key: str,
        eval_max_vulns=100,
    ):
        self.job_run = job_run
        self.platform_client = platform_client
        self.eval_max_vulns = eval_max_vulns
        self.agent_execution_id = agent_execution_id
        self.agent_evaluation_id: int | None = None
        self.project_key = project_key
        self.init_logger()

    def init_logger(self):
        prefix = f"[A:{self.job_run.agent_id}|JR:{self.job_run.id}|P:{self.project_key}] "
        self.logger = PrefixedLogger(logger, prefix)

    def eval_agent_execution(self, agent_execution):
        self.eval_max_vulns = agent_execution.eval_max_vulns or self.eval_max_vulns

        report_payload = agent_execution.report
        if isinstance(report_payload, dict) and "success" in report_payload and "report" in report_payload:
            report_data = report_payload
        else:
            report_data = {
                "success": agent_execution.success,
                "report": report_payload,
                "stdout": agent_execution.stdout,
                "stderr": agent_execution.stderr,
                "error": agent_execution.error,
                "status": agent_execution.status,
            }

        return self.score_report_data(report_data)

    def submit_agent_evaluation(self, project_scoring_results):
        if not self.agent_execution_id:
            self.logger.info("Not running from agent execution. Skipping submit evaluation")
            return None

        scoring_data = {}
        scoring_data["agent_execution_id"] = self.agent_execution_id
        scoring_data["status"] = project_scoring_results["status"]
        scoring_data.update(project_scoring_results["result"])

        agent_evaluation = AgentEvaluation.model_validate(scoring_data)

        try:
            resp = self.platform_client.submit_agent_evaluation(agent_evaluation)
            evaluation_id = resp.get("id")
            if not evaluation_id:
                self.logger.warning("Evaluation ID not received")

            return evaluation_id

        except PlatformError as e:
            self.logger.exception(f"Platform submission failed for agent evaluation: {e}")
            return None

    def score_report_data(self, report_data: dict):
        self.logger.info("Starting evaluation")

        if not settings.chutes_api_key:
            self.logger.error("Validator CHUTES_API_KEY not set. Cannot run evaluation.")
            return {"status": Status.ERROR, "error": "validator chutes_api_key not configured"}

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

        scorer_config = {
            "api_key": settings.chutes_api_key,
            "api_url": settings.proxy_url,
            "debug": True,
            "verbose": True,
            "agent_id": self.job_run.agent_id,
            "job_run_id": self.job_run.id,
            "confidence_threshold": 0.75,
            "strict_matching": False,
        }
        scorer = ScaBenchScorerV2(scorer_config)

        try:
            try:
                agent_findings = report_data.get("report", {}).get("vulnerabilities", [])
            except AttributeError as e:
                logger.error(f"Invalid report vulnerabilities ({e}): {report_data.get('report')}")
                agent_findings = []

            agent_findings = agent_findings[: self.eval_max_vulns]

            self.logger.info(
                f"Scoring {self.project_key}: {len(expected_findings)} expected vs {len(agent_findings)} found"
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

            self.agent_evaluation_id = self.submit_agent_evaluation(project_scoring_results=scoring_result)

            detection_pct = round(result.detection_rate * 100)

            msg = (
                f"Evaluation complete | "
                f"Result: {final_result} | "
                f"Detection: {detection_pct}% | "
                f"Found: {result.true_positives} | "
                f"Expected: {result.total_expected}"
            )
            if not report_data.get("success", False):
                error_msg = report_data.get("error", "Unknown error")
                msg += f" | Execution error: {error_msg}"

            self.logger.info(msg)
            self.logger.info(
                "Tokens | "
                f"Input: {result.input_tokens} (cached: {result.cached_tokens}) | "
                f"Output: {result.output_tokens}"
            )

            return scoring_result

        except Exception as e:
            self.logger.exception("Evaluation crashed")
            return {"status": Status.ERROR, "error": str(e)}
