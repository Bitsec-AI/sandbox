import json
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from config import settings
from validator.executor import AgentExecutor
from validator.models.platform import MockJobRun, Status
from validator.scorer import ScaBenchScorerV2


class CapturingPlatformClient:
    def __init__(self):
        self.evaluations = []

    def submit_agent_evaluation(self, agent_evaluation):
        self.evaluations.append(agent_evaluation)
        return {"id": 123}


@patch("validator.scorer.requests.post")
def test_scorer_sends_evaluation_tracking_headers(mock_post):
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "{}"}}],
        "usage": {},
    }
    mock_resp.raise_for_status.return_value = None
    mock_post.return_value = mock_resp

    scorer = ScaBenchScorerV2(
        {
            "api_key": "cpk_test",
            "api_url": "http://fake:8000",
            "agent_id": 123,
            "job_run_id": 456,
        }
    )
    scorer.prompt("prompt", "system")

    headers = mock_post.call_args.kwargs["headers"]
    assert headers["x-inference-api-key"] == "cpk_test"
    assert headers["x-agent-id"] == "123"
    assert headers["x-job-run-id"] == "456"
    assert headers["x-request-phase"] == "evaluation"
    assert "x-job-id" not in headers
    assert "x-project-id" not in headers


def test_timeout_report_scores_as_zero_findings_without_llm_matching(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "chutes_api_key", "test-key")

    def fail_if_called(*args, **kwargs):
        raise AssertionError("LLM matching should not run for empty findings")

    monkeypatch.setattr(ScaBenchScorerV2, "find_match_in_results", fail_if_called)

    project_key = "code4rena_coded-estate-invitational_2024_12"
    reports_dir = tmp_path / "reports"
    project_dir = reports_dir / project_key
    project_dir.mkdir(parents=True)
    report_path = project_dir / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "success": False,
                "error": "Agent timeout",
                "stdout": "",
                "stderr": "",
            }
        ),
        encoding="utf-8",
    )

    platform_client = CapturingPlatformClient()
    executor = AgentExecutor(
        job_run=MockJobRun(id=1, job_id=1, validator_id=1, agent_id=1),
        agent_filepath="",
        project_key=project_key,
        job_run_reports_dir=str(reports_dir),
        platform_client=platform_client,
    )
    executor.agent_execution_id = 42

    scoring_result = executor.eval_job_run()

    assert scoring_result["status"] == Status.SUCCESS
    result = scoring_result["result"]
    assert result["total_found"] == 0
    assert result["true_positives"] == 0
    assert result["false_negatives"] == result["total_expected"]
    assert result["detection_rate"] == 0
    assert result["matched_findings"] == []
    assert result["extra_findings"] == []
    assert len(result["missed_findings"]) == result["total_expected"]

    assert len(platform_client.evaluations) == 1
    evaluation_path = project_dir / "evaluation.json"
    assert evaluation_path.exists()


def test_eval_job_run_limits_findings_with_configured_eval_max_vulns(monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "chutes_api_key", "test-key")

    captured = {}

    def capture_score_project(self, expected_findings, tool_findings, project_name):
        captured["tool_findings_len"] = len(tool_findings)
        return SimpleNamespace(
            project=project_name,
            timestamp="2026-06-18T00:00:00",
            total_expected=len(expected_findings),
            total_found=len(tool_findings),
            true_positives=0,
            false_negatives=len(expected_findings),
            false_positives=len(tool_findings),
            detection_rate=0,
            precision=0,
            f1_score=0,
            matched_findings=[],
            missed_findings=[],
            extra_findings=[],
            undecided_findings=[],
            input_tokens=0,
            output_tokens=0,
            cached_tokens=0,
        )

    monkeypatch.setattr(ScaBenchScorerV2, "score_project", capture_score_project)

    project_key = "code4rena_coded-estate-invitational_2024_12"
    reports_dir = tmp_path / "reports"
    project_dir = reports_dir / project_key
    project_dir.mkdir(parents=True)
    report_path = project_dir / "report.json"
    report_path.write_text(
        json.dumps(
            {
                "success": True,
                "report": {
                    "vulnerabilities": [{"title": f"finding {idx}", "description": "test finding"} for idx in range(5)]
                },
            }
        ),
        encoding="utf-8",
    )

    executor = AgentExecutor(
        job_run=MockJobRun(id=1, job_id=1, validator_id=1, agent_id=1),
        agent_filepath="",
        project_key=project_key,
        job_run_reports_dir=str(reports_dir),
        platform_client=CapturingPlatformClient(),
        eval_max_vulns=2,
    )

    scoring_result = executor.eval_job_run()

    assert scoring_result["status"] == Status.SUCCESS
    assert captured["tool_findings_len"] == 2
    assert scoring_result["result"]["total_found"] == 2
