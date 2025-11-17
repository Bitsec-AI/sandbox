import base64
import os
import json
import secrets
import time
from datetime import datetime, timedelta
from typing import Any, Literal

import requests
from bittensor_wallet import Wallet

from config import settings
from validator.models.platform import JobRun, AgentExecution, AgentEvaluation, AgentCode, User, MockJobRun


class PlatformError(Exception):
    def __init__(self, message: str, status_code: int | None = None, details: Any | None = None):
        super().__init__(message)
        self.status_code = status_code
        self.details = details


class APIPlatformClient:
    def __init__(self, base_url: str | None = None, timeout: int = 10, wallet_name: str | None = None):
        self.base_url = (base_url or settings.platform_url).rstrip("/")
        self.timeout = timeout
        self.set_wallet(wallet_name)

    def set_wallet(self, wallet_name: str | None = None):
        wallet_name = wallet_name or settings.wallet_name
        wallet = Wallet(wallet_name)
        self.hotkey = wallet.hotkey

    def _create_wallet_token(self, hotkey: str, expiry_minutes: int = 1) -> str:
        iat = int(datetime.utcnow().timestamp())
        exp = int((datetime.utcnow() + timedelta(minutes=expiry_minutes)).timestamp())
        payload = {
            "address": self.hotkey.ss58_address,
            "nonce": secrets.token_hex(16),
            "domain": settings.app_url,
            "iat": iat,
            "exp": exp
        }

        payload_json = json.dumps(payload, separators=(',', ':'), sort_keys=True)
        signature_bytes = hotkey.sign(payload_json.encode())
        payload_b64 = base64.urlsafe_b64encode(payload_json.encode()).decode()
        sig_b64 = base64.urlsafe_b64encode(signature_bytes).decode()
        return f"{payload_b64}.{sig_b64}"

    def _call_api(
        self,
        method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"],
        endpoint: str,
        *,
        authenticate: bool = False,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any] | list[Any] | None:
        url = f"{self.base_url}/api/{endpoint.lstrip('/')}"

        headers: dict[str, str] = {}
        if authenticate:
            if not self.hotkey:
                raise ValueError("Wallet name must be provided via argument or WALLET_NAME environment variable.")

            token = self._create_wallet_token(self.hotkey)
            headers["Authorization"] = f"Bearer {token}"

        try:
            response = requests.request(
                method=method,
                url=url,
                params=params,
                json=json,
                headers=headers,
                timeout=self.timeout,
            )
            response.raise_for_status()

        except requests.HTTPError as exc:
            try:
                details = exc.response.json()
            except Exception:
                details = exc.response.text

            raise PlatformError(
                f"Platform API request failed ({exc.response.status_code}): {details}",
                status_code=exc.response.status_code,
                details=details,
            ) from exc

        except requests.RequestException as exc:
            raise PlatformError(f"Request failed: {exc}") from exc

        if not response.text.strip():
            return None

        try:
            return response.json()

        except json.JSONDecodeError:
            raise PlatformError(f"Expected JSON response from {url}, got invalid JSON.")

    def get_next_job_run(self, validator_id: int):
        endpoint = f"jobs/runs/validator/{validator_id}"
        resp = self._call_api('get', endpoint)
        if not resp:
            return

        job_run = JobRun.model_validate(resp)
        return job_run

    def get_job_run_code(self, job_run_id: int):
        endpoint = f"jobs/runs/{job_run_id}/code"
        resp = self._call_api('get', endpoint)
        return resp['code']

    def submit_agent_execution(self, agent_execution: AgentExecution) -> dict:
        endpoint = f"agents/execution/"
        payload = agent_execution.model_dump(mode="json")
        resp = self._call_api("post", endpoint, json=payload, authenticate=True)
        return resp

    def submit_agent_evaluation(self, agent_evaluation: AgentEvaluation) -> dict:
        endpoint = f"agents/evaluation/"
        payload = agent_evaluation.model_dump(mode="json")
        resp = self._call_api("post", endpoint, json=payload, authenticate=True)
        return resp

    def start_job_run(self, job_run_id: int) -> dict:
        endpoint = f"jobs/runs/{job_run_id}/start"
        resp = self._call_api("post", endpoint, authenticate=True)
        return resp

    def complete_job_run(self, job_run_id: int, status='success') -> dict:
        endpoint = f"jobs/runs/{job_run_id}/complete"
        payload = {
            "status": status,
        }
        resp = self._call_api("post", endpoint, json=payload, authenticate=True)
        return resp

    def submit_agent(self, agent_code: AgentCode) -> dict:
        endpoint = f"agents/submit/"
        payload = agent_code.model_dump(mode="json")
        resp = self._call_api("post", endpoint, json=payload, authenticate=True)
        return resp

    def create_user(self, user: User) -> dict:
        endpoint = f"users/"
        payload = user.model_dump(mode="json")
        resp = self._call_api("post", endpoint, json=payload, authenticate=True)
        return resp

    def get_current_validator(self) -> User:
        endpoint = f"users/validators/me"
        resp = self._call_api("get", endpoint, authenticate=True)
        return User.model_validate(resp)


class MockPlatformClient:
    def __getattr__(self, name):
        def _method(*args, **kwargs):
            return {'id': 1}
        return _method

    def get_next_job_run(self, validator_id: int):
        job_run = MockJobRun(
            id=int(time.time()),
            job_id=1,
            validator_id=1,
        )
        return job_run


class PlatformClient:
    """
    Public interface for consumers.
    Delegates all calls to either APIPlatformClient or MockPlatformClient.
    Forwards all args/kwargs transparently to the underlying client,
    while reserving `is_local` as a keyword-only argument.
    """
    def __init__(self, *args, is_local=False, **kwargs):
        if is_local:
            self._client = MockPlatformClient(*args, **kwargs)
        else:
            self._client = APIPlatformClient(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self._client, name)