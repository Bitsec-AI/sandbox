import pytest
import requests

from validator.manager import SandboxManager
from validator.platform_client import APIPlatformClient, MockPlatformClient
from validator.proxy_client import APIProxyClient, ProxyClientError


class FakeResponse:
    def __init__(self, payload=None):
        self.payload = payload or {}
        self.text = "{}"

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class CapturingPlatform:
    def __init__(self):
        self.submissions = []

    def submit_job_run_proxy_summary(self, job_run_id, payload):
        self.submissions.append((job_run_id, payload))
        return {"id": 1}


class FakeProxyClient:
    def __init__(self, summary=None, fail=False):
        self.summary = summary or {"job_run_id": "123", "stats": []}
        self.fail = fail
        self.resets = []
        self.fetches = []

    def reset_job_run_summary(self, job_run_id):
        if self.fail:
            raise RuntimeError("boom")
        self.resets.append(job_run_id)
        return {"status": "ok"}

    def get_job_run_summary(self, job_run_id):
        if self.fail:
            raise RuntimeError("boom")
        self.fetches.append(job_run_id)
        return self.summary


def test_proxy_client_resets_and_fetches_expected_summary_endpoints():
    summary = {"job_run_id": "123", "stats": []}
    calls = []

    class FakeSession:
        def request(self, method, url, timeout):
            calls.append((method, url, timeout))
            return FakeResponse(summary)

    client = APIProxyClient.__new__(APIProxyClient)
    client.base_url = "http://proxy:8000"
    client.timeout = 10
    client.session = FakeSession()

    assert client.reset_job_run_summary(123) == summary
    assert client.get_job_run_summary(123) == summary

    assert calls == [
        ("POST", "http://proxy:8000/metrics/job-runs/123/summary/reset", 5),
        ("GET", "http://proxy:8000/metrics/job-runs/123/summary", 10),
    ]


def test_proxy_client_validate_auth_uses_single_direct_request(monkeypatch):
    calls = []

    def fake_post(url, headers, timeout):
        calls.append((url, headers, timeout))
        return FakeResponse({"valid": True})

    monkeypatch.setattr("validator.proxy_client.requests.post", fake_post)
    client = APIProxyClient.__new__(APIProxyClient)
    client.base_url = "http://proxy:8000"

    assert client.validate_auth("cpk_secret") is None
    assert calls == [
        (
            "http://proxy:8000/validate_auth",
            {"x-inference-api-key": "cpk_secret"},
            30,
        )
    ]


@pytest.mark.parametrize("payload", [{"valid": False}, {}, {"valid": True, "extra": "field"}])
def test_proxy_client_validate_auth_rejects_unexpected_response(monkeypatch, payload):
    monkeypatch.setattr(
        "validator.proxy_client.requests.post",
        lambda *_args, **_kwargs: FakeResponse(payload),
    )
    client = APIProxyClient.__new__(APIProxyClient)
    client.base_url = "http://proxy:8000"

    with pytest.raises(ProxyClientError, match="Inference authentication validation failed"):
        client.validate_auth("cpk_secret")


def test_proxy_client_validate_auth_sanitizes_request_failure(monkeypatch):
    monkeypatch.setattr(
        "validator.proxy_client.requests.post",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(requests.Timeout("secret provider details")),
    )
    client = APIProxyClient.__new__(APIProxyClient)
    client.base_url = "http://proxy:8000"

    with pytest.raises(ProxyClientError) as exc_info:
        client.validate_auth("cpk_secret")

    assert "cpk_secret" not in str(exc_info.value)
    assert "provider details" not in str(exc_info.value)


def test_manager_resets_fetches_and_submits_proxy_summary():
    summary = {"job_run_id": "123", "stats": []}
    proxy_client = FakeProxyClient(summary)
    platform_client = CapturingPlatform()

    manager = SandboxManager.__new__(SandboxManager)
    manager.proxy_client = proxy_client
    manager.platform_client = platform_client

    manager.reset_proxy_summary(123, 456)
    manager.submit_proxy_summary(123, 456)

    assert proxy_client.resets == [123]
    assert proxy_client.fetches == [123]
    assert platform_client.submissions == [(123, summary)]


def test_manager_proxy_summary_failures_do_not_raise():
    platform_client = CapturingPlatform()
    manager = SandboxManager.__new__(SandboxManager)
    manager.proxy_client = FakeProxyClient(fail=True)
    manager.platform_client = platform_client

    manager.reset_proxy_summary(123, 456)
    manager.submit_proxy_summary(123, 456)

    assert platform_client.submissions == []


def test_platform_client_submits_proxy_summary_to_expected_endpoint(monkeypatch):
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
        return {"id": 1}

    monkeypatch.setattr(client, "_call_api", fake_call_api)
    payload = {"job_run_id": "123", "stats": []}

    assert client.submit_job_run_proxy_summary(123, payload) == {"id": 1}
    assert captured == {
        "method": "post",
        "endpoint": "jobs/runs/123/proxy-summary",
        "json": payload,
        "authenticate": True,
        "params": None,
    }


def test_mock_platform_client_prints_proxy_summary(capsys):
    payload = {"job_run_id": "123", "stats": []}

    assert MockPlatformClient().submit_job_run_proxy_summary(123, payload) == {"id": 1}
    assert '"job_run_id": "123"' in capsys.readouterr().out
