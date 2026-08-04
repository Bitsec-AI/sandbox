import asyncio
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock

import pytest

from validator.manager import SandboxManager
from validator.models.platform import MockJobRun, Status, SubmittedAgentExecution
from validator.proxy_client import ProxyClientError


class FakeProxyClient:
    def __init__(self, auth_error=False):
        self.auth_error = auth_error
        self.resets = []
        self.fetches = []
        self.auth_checks = []

    def validate_auth(self, api_key):
        self.auth_checks.append(api_key)
        if self.auth_error:
            raise ProxyClientError("validation failed")

    def reset_job_run_summary(self, job_run_id):
        self.resets.append(job_run_id)
        return {"status": "ok"}

    def get_job_run_summary(self, job_run_id):
        self.fetches.append(job_run_id)
        return {"job_run_id": str(job_run_id), "stats": []}


class FakePlatformClient:
    def __init__(self, execution_jobs=None, scoring_jobs=None, executions=None):
        self.execution_jobs = list(execution_jobs or [])
        self.scoring_jobs = list(scoring_jobs or [])
        self.executions = executions or {}
        self.started = []
        self.completed = []
        self.execution_submissions = []
        self.evaluation_starts = []
        self.evaluation_submissions = []
        self.proxy_submissions = []

    def send_heartbeat(self):
        return {"status": "ok"}

    def get_next_job_run(self, validator_id):
        if not self.execution_jobs:
            return None
        return self.execution_jobs.pop(0)

    def get_next_scoring_job_run(self, validator_id):
        if not self.scoring_jobs:
            return None
        return self.scoring_jobs.pop(0)

    def get_job_run_agent(self, job_run_id):
        return {
            "project_keys": ["project-a", "project-b"],
            "execution_api_key": "cpk_test",
            "eval_max_vulns": 2,
        }

    def start_job_run(self, job_run_id):
        self.started.append(job_run_id)
        return {"id": job_run_id}

    def start_job_run_evaluation(self, job_run_id):
        self.evaluation_starts.append(job_run_id)
        return {"id": job_run_id}

    def complete_job_run(self, job_run_id, status="success"):
        self.completed.append((job_run_id, status))
        return {"id": job_run_id}

    def get_job_run_executions(self, job_run_id):
        return self.executions.get(job_run_id, [])

    def submit_job_run_proxy_summary(self, job_run_id, payload):
        self.proxy_submissions.append((job_run_id, payload))
        return {"id": 1}


class FakeExecutionAgentExecutor:
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
        self.project_key = project_key
        self.platform_client = platform_client
        self.agent_execution_id = None

    def run_execution(self):
        return None

    def submit_agent_execution(self):
        self.platform_client.execution_submissions.append((self.job_run.id, self.project_key))
        return len(self.platform_client.execution_submissions)


class FakeAgentEvaluator:
    def __init__(
        self,
        job_run,
        platform_client,
        agent_execution_id,
        project_key,
        eval_max_vulns=100,
    ):
        self.job_run = job_run
        self.agent_execution_id = agent_execution_id
        self.project_key = project_key
        self.platform_client = platform_client

    def eval_agent_execution(self, execution):
        self.platform_client.evaluation_submissions.append((self.job_run.id, execution.id, execution.project))
        return {"status": Status.SUCCESS, "result": {"project": execution.project}}


def make_manager(platform_client, proxy_client=None, tmp_path=None):
    manager = SandboxManager.__new__(SandboxManager)
    manager.is_local = True
    manager.validator_id = 7
    manager.platform_client = platform_client
    manager.proxy_client = proxy_client or FakeProxyClient()
    manager.all_jobs_dir = str(tmp_path / "jobs") if tmp_path else "jobs"
    manager.host_jobs_dir = manager.all_jobs_dir
    manager.execution_pool = ThreadPoolExecutor(max_workers=2)
    manager.scoring_pool = ThreadPoolExecutor(max_workers=2)
    manager.execution_queue_task = None
    manager.scoring_queue_task = None
    return manager


def close_manager(manager):
    manager.execution_pool.shutdown(wait=True)
    manager.scoring_pool.shutdown(wait=True)


def test_execution_starts_evaluation_after_all_execution_submissions(monkeypatch, tmp_path):
    monkeypatch.setattr("validator.manager.AgentExecutor", FakeExecutionAgentExecutor)
    job_run = MockJobRun(id=1, job_id=1, validator_id=7, agent_id=9)
    platform = FakePlatformClient(execution_jobs=[job_run])
    proxy = FakeProxyClient()
    manager = make_manager(platform, proxy_client=proxy, tmp_path=tmp_path)

    try:
        assert asyncio.run(manager.poll_execution_job_run()) is True
    finally:
        close_manager(manager)

    assert platform.started == [1]
    assert proxy.auth_checks == ["cpk_test"]
    assert platform.completed == []
    assert platform.execution_submissions == [
        (1, "project-a"),
        (1, "project-b"),
    ]
    assert platform.evaluation_starts == [1]


def test_execution_auth_failure_completes_job_without_executors(monkeypatch, tmp_path):
    executor = MagicMock()
    monkeypatch.setattr("validator.manager.AgentExecutor", executor)
    job_run = MockJobRun(id=1, job_id=1, validator_id=7, agent_id=9)
    platform = FakePlatformClient(execution_jobs=[job_run])
    proxy = FakeProxyClient(auth_error=True)
    manager = make_manager(platform, proxy_client=proxy, tmp_path=tmp_path)

    try:
        assert asyncio.run(manager.poll_execution_job_run()) is True
    finally:
        close_manager(manager)

    assert proxy.auth_checks == ["cpk_test"]
    assert platform.completed == [(1, "error")]
    assert platform.execution_submissions == []
    assert platform.evaluation_starts == []
    executor.assert_not_called()


def test_execution_missing_key_does_not_call_auth_validation(monkeypatch, tmp_path):
    job_run = MockJobRun(id=1, job_id=1, validator_id=7, agent_id=9)

    class MissingKeyPlatformClient(FakePlatformClient):
        def get_job_run_agent(self, job_run_id):
            agent = super().get_job_run_agent(job_run_id)
            agent["execution_api_key"] = None
            return agent

    platform = MissingKeyPlatformClient(execution_jobs=[job_run])
    proxy = FakeProxyClient()
    manager = make_manager(platform, proxy_client=proxy, tmp_path=tmp_path)

    try:
        assert asyncio.run(manager.poll_execution_job_run()) is True
    finally:
        close_manager(manager)

    assert proxy.auth_checks == []
    assert platform.completed == [(1, "error")]
    assert platform.evaluation_starts == []


def test_execution_can_claim_next_job_before_prior_scoring_completes(monkeypatch, tmp_path):
    monkeypatch.setattr("validator.manager.AgentExecutor", FakeExecutionAgentExecutor)
    jobs = [
        MockJobRun(id=1, job_id=1, validator_id=7, agent_id=9),
        MockJobRun(id=2, job_id=2, validator_id=7, agent_id=10),
    ]
    platform = FakePlatformClient(execution_jobs=jobs)
    manager = make_manager(platform, tmp_path=tmp_path)

    try:
        assert asyncio.run(manager.poll_execution_job_run()) is True
        assert asyncio.run(manager.poll_execution_job_run()) is True
    finally:
        close_manager(manager)

    assert platform.started == [1, 2]
    assert platform.completed == []
    assert [submission[0] for submission in platform.execution_submissions] == [1, 1, 2, 2]
    assert platform.evaluation_starts == [1, 2]


def test_scoring_fetches_platform_executions_and_completes_after_scoring(monkeypatch, tmp_path):
    monkeypatch.setattr("validator.manager.AgentEvaluator", FakeAgentEvaluator)
    job_run = MockJobRun(id=1, job_id=1, validator_id=7, agent_id=9)
    execution = SubmittedAgentExecution(
        id=123,
        validator_id=7,
        job_run_id=1,
        project="project-a",
        success=True,
        report={"vulnerabilities": []},
        status=Status.SUCCESS,
    )
    proxy = FakeProxyClient()
    platform = FakePlatformClient(scoring_jobs=[job_run], executions={1: [execution]})
    manager = make_manager(platform, proxy_client=proxy, tmp_path=tmp_path)

    try:
        assert asyncio.run(manager.poll_scoring_job_run()) is True
    finally:
        close_manager(manager)

    assert platform.evaluation_submissions == [(1, 123, "project-a")]
    assert platform.completed == [(1, "success")]
    assert proxy.fetches == [1]
    assert platform.proxy_submissions == [(1, {"job_run_id": "1", "stats": []})]


def test_scoring_missing_platform_executions_marks_job_error(tmp_path):
    job_run = MockJobRun(id=1, job_id=1, validator_id=7, agent_id=9)
    platform = FakePlatformClient(scoring_jobs=[job_run], executions={})
    manager = make_manager(platform, tmp_path=tmp_path)

    try:
        assert asyncio.run(manager.poll_scoring_job_run()) is True
    finally:
        close_manager(manager)

    assert platform.evaluation_submissions == []
    assert platform.completed == [(1, "error")]


def test_local_run_executes_then_scores_same_job(monkeypatch, tmp_path):
    monkeypatch.setattr("validator.manager.AgentExecutor", FakeExecutionAgentExecutor)
    monkeypatch.setattr("validator.manager.AgentEvaluator", FakeAgentEvaluator)
    job_run = MockJobRun(id=1, job_id=1, validator_id=7, agent_id=9)
    execution = SubmittedAgentExecution(
        id=123,
        validator_id=7,
        job_run_id=1,
        project="project-a",
        success=True,
        report={"vulnerabilities": []},
        status=Status.SUCCESS,
    )

    class LocalPlatformClient(FakePlatformClient):
        def get_next_scoring_job_run(self, validator_id):
            if 1 not in self.evaluation_starts:
                return None
            return job_run

    platform = LocalPlatformClient(execution_jobs=[job_run], executions={1: [execution]})
    manager = make_manager(platform, tmp_path=tmp_path)

    try:
        asyncio.run(manager.run())
    finally:
        close_manager(manager)

    assert platform.execution_submissions == [
        (1, "project-a"),
        (1, "project-b"),
    ]
    assert platform.evaluation_starts == [1]
    assert platform.evaluation_submissions == [(1, 123, "project-a")]
    assert platform.completed == [(1, "success")]


def test_run_once_starts_independent_queue_tasks_once(monkeypatch, tmp_path):
    monkeypatch.setattr("validator.manager.settings.skip_execution", False)
    monkeypatch.setattr("validator.manager.settings.skip_evaluation", False)
    manager = make_manager(FakePlatformClient(), tmp_path=tmp_path)

    async def wait_forever():
        await asyncio.Future()

    manager.run_execution_queue = wait_forever
    manager.run_scoring_queue = wait_forever

    async def run_test():
        assert await manager.run_once() is True
        execution_task = manager.execution_queue_task
        scoring_task = manager.scoring_queue_task

        assert execution_task is not None
        assert scoring_task is not None
        assert execution_task is not scoring_task

        assert await manager.run_once() is False
        assert manager.execution_queue_task is execution_task
        assert manager.scoring_queue_task is scoring_task

        execution_task.cancel()
        scoring_task.cancel()
        await asyncio.gather(execution_task, scoring_task, return_exceptions=True)

    try:
        asyncio.run(run_test())
    finally:
        close_manager(manager)


def test_raise_queue_errors_bubbles_crashed_queue(monkeypatch, tmp_path):
    monkeypatch.setattr("validator.manager.settings.skip_execution", False)
    monkeypatch.setattr("validator.manager.settings.skip_evaluation", True)
    manager = make_manager(FakePlatformClient(), tmp_path=tmp_path)

    async def crash():
        raise ValueError("execution failed")

    manager.run_execution_queue = crash

    async def run_test():
        assert manager.ensure_queue_tasks() is True
        await asyncio.sleep(0)

        with pytest.raises(RuntimeError, match="execution queue crashed") as exc_info:
            manager.raise_queue_errors()

        assert isinstance(exc_info.value.__cause__, ValueError)
        assert str(exc_info.value.__cause__) == "execution failed"

    try:
        asyncio.run(run_test())
    finally:
        close_manager(manager)


def test_raise_queue_errors_treats_cancelled_queue_as_fatal(tmp_path):
    manager = make_manager(FakePlatformClient(), tmp_path=tmp_path)

    async def run_test():
        task = asyncio.create_task(asyncio.sleep(60))
        manager.scoring_queue_task = task
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)

        with pytest.raises(RuntimeError, match="scoring queue task was cancelled"):
            manager.raise_queue_errors()

    try:
        asyncio.run(run_test())
    finally:
        close_manager(manager)
