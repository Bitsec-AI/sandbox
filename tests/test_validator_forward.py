import asyncio
import importlib

forward_module = importlib.import_module("template.validator.forward")


class FakeSandboxManager:
    def __init__(self):
        self.calls = []
        self.raise_error = None

    def raise_queue_errors(self):
        self.calls.append("raise_queue_errors")
        if self.raise_error:
            raise self.raise_error

    def ensure_queue_tasks(self):
        self.calls.append("ensure_queue_tasks")


class FakeValidator:
    def __init__(self):
        self.sandbox_manager = FakeSandboxManager()
        self.top_score_updates = 0

    def update_top_miner_scores(self):
        self.top_score_updates += 1


async def no_sleep(seconds):
    return None


def test_forward_checks_and_starts_sandbox_manager_queues(monkeypatch):
    monkeypatch.setattr(forward_module.asyncio, "sleep", no_sleep)
    validator = FakeValidator()

    async def run_forward_twice():
        await forward_module.forward(validator)
        await forward_module.forward(validator)

    asyncio.run(run_forward_twice())

    assert validator.sandbox_manager.calls == [
        "raise_queue_errors",
        "ensure_queue_tasks",
        "raise_queue_errors",
        "ensure_queue_tasks",
    ]
    assert validator.top_score_updates == 2


def test_forward_bubbles_sandbox_manager_queue_errors(monkeypatch):
    monkeypatch.setattr(forward_module.asyncio, "sleep", no_sleep)
    validator = FakeValidator()
    validator.sandbox_manager.raise_error = RuntimeError("manager failed")

    async def run_forward():
        await forward_module.forward(validator)

    try:
        asyncio.run(run_forward())
    except RuntimeError as e:
        assert str(e) == "manager failed"
    else:
        raise AssertionError("Expected manager error to bubble out of forward")

    assert validator.sandbox_manager.calls == ["raise_queue_errors"]
