from datetime import datetime

from validator.models.platform import AgentExecution, Status
from validator.platform_client import APIPlatformClient, MockPlatformClient


def make_execution():
    return AgentExecution(
        validator_id=1,
        job_run_id=2,
        project="project-a",
        success=True,
        report={"vulnerabilities": []},
        status=Status.SUCCESS,
        started_at=datetime(2026, 6, 18, 0, 0, 0),
        completed_at=datetime(2026, 6, 18, 0, 1, 0),
    )


def test_submit_agent_execution_keeps_existing_payload(monkeypatch):
    captured = {}
    client = APIPlatformClient.__new__(APIPlatformClient)

    def fake_call_api(method, endpoint, *, json=None, authenticate=False, params=None):
        captured.update(
            {
                "method": method,
                "endpoint": endpoint,
                "json": json,
                "authenticate": authenticate,
                "params": params,
            }
        )
        return {"id": 123}

    monkeypatch.setattr(client, "_call_api", fake_call_api)

    assert client.submit_agent_execution(make_execution()) == {"id": 123}
    assert captured["method"] == "post"
    assert captured["endpoint"] == "agents/execution/"
    assert captured["authenticate"] is True
    assert captured["params"] is None
    assert captured["json"]["job_run_id"] == 2


def test_start_job_run_evaluation_uses_transition_endpoint(monkeypatch):
    captured = {}
    client = APIPlatformClient.__new__(APIPlatformClient)

    def fake_call_api(method, endpoint, *, json=None, authenticate=False, params=None):
        captured.update(
            {
                "method": method,
                "endpoint": endpoint,
                "json": json,
                "authenticate": authenticate,
                "params": params,
            }
        )
        return {"id": 123}

    monkeypatch.setattr(client, "_call_api", fake_call_api)

    assert client.start_job_run_evaluation(123) == {"id": 123}
    assert captured["method"] == "post"
    assert captured["endpoint"] == "jobs/runs/123/evaluating"
    assert captured["authenticate"] is True
    assert captured["params"] is None
    assert captured["json"] is None


def test_scoring_job_run_client_methods_use_expected_endpoints(monkeypatch):
    calls = []
    now = datetime(2026, 6, 18, 0, 0, 0).isoformat()
    client = APIPlatformClient.__new__(APIPlatformClient)

    def fake_call_api(method, endpoint, *, json=None, authenticate=False, params=None):
        calls.append((method, endpoint, authenticate, params))
        if endpoint == "jobs/runs/validator/7/evaluating":
            return {
                "id": 2,
                "job_id": 1,
                "validator_id": 7,
                "status": "evaluating",
                "started_at": now,
                "evaluation_started_at": now,
                "completed_at": None,
                "created_at": now,
                "updated_at": now,
                "agent_id": 9,
            }
        return [
            {
                "id": 123,
                "validator_id": 7,
                "job_run_id": 2,
                "project": "project-a",
                "success": True,
                "report": {"vulnerabilities": []},
                "status": "success",
            }
        ]

    monkeypatch.setattr(client, "_call_api", fake_call_api)

    job_run = client.get_next_scoring_job_run(7)
    executions = client.get_job_run_executions(2)

    assert job_run.status == "evaluating"
    assert job_run.evaluation_started_at is not None
    assert executions[0].id == 123
    assert executions[0].project == "project-a"
    assert calls == [
        ("post", "jobs/runs/validator/7/evaluating", True, None),
        ("get", "jobs/runs/2/executions", True, None),
    ]


def test_mock_platform_moves_current_job_to_scoring_queue_on_evaluation_transition():
    client = MockPlatformClient()
    job_run = client.get_next_job_run(7)
    execution = make_execution().model_copy(update={"job_run_id": job_run.id})

    response = client.submit_agent_execution(execution)
    transition = client.start_job_run_evaluation(job_run.id)
    scoring_job_run = client.get_next_scoring_job_run(7)
    executions = client.get_job_run_executions(job_run.id)

    assert response == {"id": 1}
    assert transition == {"id": job_run.id}
    assert scoring_job_run == job_run
    assert executions[0].id == 1
    assert executions[0].job_run_id == job_run.id


def test_mock_platform_requires_evaluation_transition_for_scoring_queue():
    client = MockPlatformClient()
    job_run = client.get_next_job_run(7)
    execution = make_execution().model_copy(update={"job_run_id": job_run.id})

    client.submit_agent_execution(execution)

    assert client.get_next_scoring_job_run(7) is None
    assert client.get_job_run_executions(job_run.id)[0].id == 1
