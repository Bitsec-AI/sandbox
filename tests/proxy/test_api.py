from fastapi.testclient import TestClient

import api
from models import InferenceResponse


class FakeMetricsRecorder:
    def __init__(self):
        self.completions = []

    def record_proxy_complete(self, *args, **kwargs):
        self.completions.append((args, kwargs))


class FakeProviderClient:
    default_model = "default-model"

    def __init__(self):
        self.metrics_ctx = "not-called"
        self.log_ctx = "not-called"

    def call(self, request, provider, metrics_ctx=None, log_ctx=None):
        self.metrics_ctx = metrics_ctx
        self.log_ctx = log_ctx
        return InferenceResponse(
            model=request.model,
            choices=[{"message": {"content": "{}"}, "finish_reason": "stop"}],
            content="{}",
        )


def test_inference_skips_metrics_without_job_run_id(monkeypatch):
    recorder = FakeMetricsRecorder()
    provider_client = FakeProviderClient()
    monkeypatch.setattr(api, "get_metrics_recorder", lambda: recorder)
    monkeypatch.setattr(api, "get_provider_client", lambda provider_name: provider_client)

    response = TestClient(api.app).post(
        "/inference",
        headers={"x-inference-api-key": "cpk_test"},
        json={"model": "requested-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert provider_client.metrics_ctx is None
    assert provider_client.log_ctx.agent_id == "unknown"
    assert provider_client.log_ctx.job_run_id == "unknown"
    assert provider_client.log_ctx.phase == "unknown"
    assert recorder.completions == []
