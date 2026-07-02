import pytest

import base_client
from base_client import ProxyProviderError
from validator.proxy.chutes_client import ChutesClient
from validator.proxy.models import InferenceProvider, InferenceRequest, ProxyMetricsContext


class FakeResponse:
    status_code = 200
    text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return {
            "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
            "model": "actual-response-model",
            "usage": {},
        }


class FakeErrorResponse:
    status_code = 402
    text = "payment required"

    def raise_for_status(self):
        import requests

        raise requests.HTTPError("payment required")


class FakeRetryResponse:
    status_code = 429
    text = "rate limited"

    def raise_for_status(self):
        import requests

        raise requests.HTTPError("rate limited")


class FakeLogger:
    def __init__(self):
        self.infos = []

    def info(self, message):
        self.infos.append(message)

    def warning(self, message):
        pass

    def error(self, message):
        pass

    def exception(self, message):
        pass


class FakeMetricsRecorder:
    def __init__(self):
        self.attempts = []

    def record_llm_attempt(self, ctx, attempt):
        self.attempts.append((ctx.model, attempt))


def test_provider_client_uses_response_model_for_metrics_when_available(monkeypatch):
    def fake_post(*args, **kwargs):
        return FakeResponse()

    monkeypatch.setattr("base_client.SESSION.post", fake_post)

    ctx = ProxyMetricsContext(
        agent_id="456",
        job_run_id="123",
        phase="execution",
        req_model="requested-model",
        model="requested-model",
        provider="chutes",
    )

    response = ChutesClient().call(
        InferenceRequest(model="requested-model", messages=[{"role": "user", "content": "hi"}]),
        InferenceProvider(api_key="cpk_test"),
        ctx,
    )

    assert response.model == "actual-response-model"
    assert ctx.req_model == "requested-model"
    assert ctx.model == "actual-response-model"


def test_provider_client_keeps_requested_model_when_response_model_unavailable(monkeypatch):
    def fake_post(*args, **kwargs):
        return FakeErrorResponse()

    monkeypatch.setattr("base_client.SESSION.post", fake_post)

    ctx = ProxyMetricsContext(
        agent_id="456",
        job_run_id="123",
        phase="execution",
        req_model="requested-model",
        model="requested-model",
        provider="chutes",
    )

    with pytest.raises(ProxyProviderError):
        ChutesClient().call(
            InferenceRequest(model="requested-model", messages=[{"role": "user", "content": "hi"}]),
            InferenceProvider(api_key="cpk_test"),
            ctx,
        )

    assert ctx.req_model == "requested-model"
    assert ctx.model == "requested-model"


def test_provider_client_logs_request_metadata_when_metrics_disabled(monkeypatch):
    def fake_post(*args, **kwargs):
        return FakeResponse()

    fake_logger = FakeLogger()
    monkeypatch.setattr("base_client.SESSION.post", fake_post)
    monkeypatch.setattr(base_client, "logger", fake_logger)

    log_ctx = ProxyMetricsContext(
        agent_id="agent-123",
        job_run_id=None,
        phase="execution",
        req_model="requested-model",
        model="requested-model",
        provider="chutes",
    )

    ChutesClient().call(
        InferenceRequest(model="requested-model", messages=[{"role": "user", "content": "hi"}]),
        InferenceProvider(api_key="cpk_test"),
        metrics_ctx=None,
        log_ctx=log_ctx,
    )

    assert any(
        'Request from [A:agent-123|JR:unknown|Phase:execution] | provider="chutes" | model="requested-model"'
        in message
        for message in fake_logger.infos
    )


def test_provider_client_flushes_retry_metrics_with_final_response_model(monkeypatch):
    responses = [FakeRetryResponse(), FakeResponse()]

    def fake_post(*args, **kwargs):
        return responses.pop(0)

    recorder = FakeMetricsRecorder()
    monkeypatch.setattr("base_client.SESSION.post", fake_post)
    monkeypatch.setattr("base_client.time.sleep", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(base_client, "get_metrics_recorder", lambda: recorder)

    ctx = ProxyMetricsContext(
        agent_id="456",
        job_run_id="123",
        phase="execution",
        req_model="requested-model",
        model="requested-model",
        provider="chutes",
    )

    response = ChutesClient().call(
        InferenceRequest(model="requested-model", messages=[{"role": "user", "content": "hi"}]),
        InferenceProvider(api_key="cpk_test"),
        ctx,
    )

    assert response.model == "actual-response-model"
    assert [model for model, _attempt in recorder.attempts] == [
        "actual-response-model",
        "actual-response-model",
    ]
    assert [attempt.status_code for _model, attempt in recorder.attempts] == [429, 200]
