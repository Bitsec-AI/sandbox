from typing import Any, Literal
import time

import requests
from requests.adapters import HTTPAdapter, Retry

from config import settings
from loggers.logger import get_logger


logger = get_logger()


class ProxyClientError(Exception):
    pass


INFERENCE_429_RETRY_INTERVAL_SECONDS = 60
INFERENCE_429_MAX_RETRIES = 12 * 60


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

    def validate_auth(self, api_key: str) -> None:
        url = f"{self.base_url}/validate_auth"
        try:
            response = requests.post(
                url,
                headers={"x-inference-api-key": api_key},
            )
            response.raise_for_status()
            payload = response.json()
        except (requests.RequestException, ValueError) as exc:
            raise ProxyClientError("Inference authentication validation failed") from exc

        if payload != {"valid": True}:
            raise ProxyClientError("Inference authentication validation failed")

    def inference(
        self,
        payload: dict[str, Any],
        *,
        headers: dict[str, str],
        retry_rate_limits: bool = True,
    ) -> dict[str, Any]:
        max_retries = INFERENCE_429_MAX_RETRIES if retry_rate_limits else 0

        for retry_num in range(max_retries + 1):
            response = requests.post(
                f"{self.base_url}/inference",
                headers=headers,
                json=payload,
            )

            try:
                response.raise_for_status()

            except requests.HTTPError as exc:
                try:
                    body = response.json()

                except ValueError:
                    body = None

                is_rate_limited = (
                    isinstance(body, dict)
                    and body.get("error_code") == "rate_limited"
                    and body.get("upstream_status") == 429
                )

                if not is_rate_limited or not retry_rate_limits:
                    raise

                if retry_num == max_retries:
                    raise RuntimeError(f"Inference remained rate limited after {max_retries} retries") from exc

                logger.warning(f"Inference rate limited. Retrying {retry_num + 1}/{max_retries}")

                time.sleep(INFERENCE_429_RETRY_INTERVAL_SECONDS)

                continue

            return response.json()


class ProxyClient:
    def __init__(self, *args, **kwargs):
        self._client = APIProxyClient(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)
