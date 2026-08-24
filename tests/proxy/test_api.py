from fastapi.testclient import TestClient

import api
from base_client import ProxyProviderError
from models import InferenceResponse


class FakeMetricsRecorder:
    def __init__(self):
        self.completions = []

    def record_proxy_complete(self, *args, **kwargs):
        self.completions.append((args, kwargs))


class FakeProviderClient:
    def __init__(self, default_model="default-model"):
        self.default_model = default_model
        self.metrics_ctx = "not-called"
        self.log_ctx = "not-called"
        self.request = None
        self.provider = None

    def call(self, request, provider, metrics_ctx=None, log_ctx=None):
        self.request = request
        self.provider = provider
        self.metrics_ctx = metrics_ctx
        self.log_ctx = log_ctx
        return InferenceResponse(
            model=request.model,
            choices=[{"message": {"content": "{}"}, "finish_reason": "stop"}],
            content="{}",
        )


class FailingProviderClient(FakeProviderClient):
    def call(self, request, provider, metrics_ctx=None, log_ctx=None):
        self.metrics_ctx = metrics_ctx
        self.log_ctx = log_ctx
        raise ProxyProviderError("openrouter error: response unusable (finish_reason=length)")


class RateLimitedProviderClient(FakeProviderClient):
    def call(self, request, provider, metrics_ctx=None, log_ctx=None):
        self.metrics_ctx = metrics_ctx
        self.log_ctx = log_ctx
        raise ProxyProviderError(
            "chutes error: retry limit reached (status 429)",
            error_code="rate_limited",
            upstream_status=429,
        )


class ExplodingProviderClient(FakeProviderClient):
    def call(self, request, provider, metrics_ctx=None, log_ctx=None):
        raise RuntimeError("unexpected error")


def test_validate_auth_routes_chutes_to_validation_model(monkeypatch):
    provider_client = FakeProviderClient(default_model="Qwen/Qwen3-32B-TEE")
    provider_names = []

    def get_client(provider_name):
        provider_names.append(provider_name)
        return provider_client

    monkeypatch.setattr(api, "get_provider_client", get_client)

    response = TestClient(api.app).post(
        "/validate_auth",
        headers={"x-inference-api-key": "cpk_test"},
    )

    assert response.status_code == 200
    assert response.json() == {"valid": True}
    assert provider_names == ["chutes"]
    assert provider_client.request.model == "Qwen/Qwen3-32B-TEE"
    assert provider_client.request.model_extra["response_format"] == {"type": "text"}
    assert provider_client.metrics_ctx is None
    assert provider_client.log_ctx is None


def test_validate_auth_routes_openrouter_to_auto_beta(monkeypatch):
    provider_client = FakeProviderClient(default_model="openrouter/auto-beta")
    monkeypatch.setattr(api, "get_provider_client", lambda _provider_name: provider_client)

    response = TestClient(api.app).post(
        "/validate_auth",
        headers={"x-inference-api-key": "sk-or-test"},
    )

    assert response.status_code == 200
    assert provider_client.request.model == "openrouter/auto-beta"
    assert provider_client.provider.name == "openrouter"


def test_validate_auth_rejects_missing_and_unsupported_keys():
    client = TestClient(api.app)

    assert client.post("/validate_auth").status_code == 422
    assert client.post("/validate_auth", headers={"x-inference-api-key": "bad-key"}).status_code == 422


def test_validate_auth_maps_provider_failure_to_bad_gateway(monkeypatch):
    monkeypatch.setattr(api, "get_provider_client", lambda _provider_name: FailingProviderClient())

    response = TestClient(api.app).post(
        "/validate_auth",
        headers={"x-inference-api-key": "sk-or-test"},
    )

    assert response.status_code == 502
    assert response.json() == {"detail": "Inference provider authentication failed"}


def test_validate_auth_maps_unexpected_failure_to_internal_error(monkeypatch):
    monkeypatch.setattr(api, "get_provider_client", lambda _provider_name: ExplodingProviderClient())
    client = TestClient(api.app, raise_server_exceptions=False)

    response = client.post(
        "/validate_auth",
        headers={"x-inference-api-key": "cpk_test"},
    )

    assert response.status_code == 500


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


def test_inference_records_proxy_status_code_on_success(monkeypatch):
    recorder = FakeMetricsRecorder()
    provider_client = FakeProviderClient()
    monkeypatch.setattr(api, "get_metrics_recorder", lambda: recorder)
    monkeypatch.setattr(api, "get_provider_client", lambda provider_name: provider_client)

    response = TestClient(api.app).post(
        "/inference",
        headers={"x-inference-api-key": "cpk_test", "x-job-run-id": "123"},
        json={"model": "requested-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 200
    assert recorder.completions[0][1]["success"] is True
    assert recorder.completions[0][1]["status_code"] == 200


def test_inference_records_proxy_status_code_on_provider_error(monkeypatch):
    recorder = FakeMetricsRecorder()
    provider_client = FailingProviderClient()
    monkeypatch.setattr(api, "get_metrics_recorder", lambda: recorder)
    monkeypatch.setattr(api, "get_provider_client", lambda provider_name: provider_client)

    response = TestClient(api.app).post(
        "/inference",
        headers={"x-inference-api-key": "cpk_test", "x-job-run-id": "123"},
        json={"model": "requested-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502
    assert recorder.completions[0][1]["success"] is False
    assert recorder.completions[0][1]["status_code"] == 502


def test_inference_returns_structured_upstream_rate_limit(monkeypatch):
    recorder = FakeMetricsRecorder()
    monkeypatch.setattr(api, "get_metrics_recorder", lambda: recorder)
    monkeypatch.setattr(api, "get_provider_client", lambda provider_name: RateLimitedProviderClient())

    response = TestClient(api.app).post(
        "/inference",
        headers={"x-inference-api-key": "cpk_test", "x-job-run-id": "123"},
        json={"model": "requested-model", "messages": [{"role": "user", "content": "hi"}]},
    )

    assert response.status_code == 502
    assert response.json() == {
        "detail": "chutes error: retry limit reached (status 429)",
        "error_code": "rate_limited",
        "upstream_status": 429,
    }
    assert recorder.completions[0][1]["success"] is False
    assert recorder.completions[0][1]["status_code"] == 502
