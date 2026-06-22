from typing import Any, Literal

import requests
from requests.adapters import HTTPAdapter, Retry

from config import settings


class ProxyClientError(Exception):
    pass


class APIProxyClient:
    def __init__(self, base_url: str | None = None, timeout: int = 10):
        self.base_url = (base_url or settings.proxy_url).rstrip("/")
        self.timeout = timeout
        self.session = self.init_session()

    def init_session(self):
        session = requests.Session()
        retry = Retry(
            total=3,
            backoff_factor=0.2,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=None,
        )
        session.mount("https://", HTTPAdapter(max_retries=retry))
        session.mount("http://", HTTPAdapter(max_retries=retry))
        return session

    def _call_proxy(
        self,
        method: Literal["GET", "POST"],
        endpoint: str,
        *,
        timeout: int | None = None,
    ) -> dict[str, Any] | None:
        url = f"{self.base_url}/{endpoint.lstrip('/')}"

        try:
            response = self.session.request(method=method, url=url, timeout=timeout or self.timeout)
            response.raise_for_status()
        except requests.RequestException as exc:
            raise ProxyClientError(f"Proxy request failed: {exc}") from exc

        if not response.text.strip():
            return None

        try:
            return response.json()
        except ValueError as exc:
            raise ProxyClientError(f"Expected JSON response from {url}, got invalid JSON.") from exc

    def reset_job_run_summary(self, job_run_id: int) -> dict[str, Any] | None:
        return self._call_proxy("POST", f"metrics/job-runs/{job_run_id}/summary/reset", timeout=5)

    def get_job_run_summary(self, job_run_id: int) -> dict[str, Any] | None:
        return self._call_proxy("GET", f"metrics/job-runs/{job_run_id}/summary", timeout=10)


class ProxyClient:
    def __init__(self, *args, **kwargs):
        self._client = APIProxyClient(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)
