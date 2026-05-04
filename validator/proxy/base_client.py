import json
import time
from abc import ABC, abstractmethod

import requests
from loggers.logger import get_logger

from models import InferenceProvider, InferenceRequest, InferenceResponse


logger = get_logger()


class ProxyProviderError(Exception):
    pass


TIMEOUT = 300
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.5
SESSION = requests.Session()


class BaseProviderClient(ABC):
    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def api_url(self) -> str:
        raise NotImplementedError

    @property
    @abstractmethod
    def default_model(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def build_headers(self, api_key: str) -> dict[str, str]:
        raise NotImplementedError

    def call(
        self,
        request: InferenceRequest,
        provider: InferenceProvider,
        job_id: str = "unknown",
        project_key: str = "unknown",
    ) -> InferenceResponse:
        if not request.model:
            request.model = self.default_model

        logger.info(
            f'Request from [J:{job_id}|P:{project_key}] | provider="{self.provider_name}" | model="{request.model}"'
        )

        headers = self.build_headers(provider.api_key)
        payload_dict = request.model_dump(exclude_none=True)

        if not isinstance(payload_dict.get("response_format"), dict):
            payload_dict["response_format"] = {"type": "json_object"}

        resp = None
        for attempt in range(1, MAX_RETRIES + 1):
            try:
                logger.info(f"Sending request to {self.provider_name}. Attempt: {attempt}")
                resp = SESSION.post(
                    self.api_url,
                    headers=headers,
                    json=payload_dict,
                    timeout=TIMEOUT,
                )
                resp.raise_for_status()
                break
            except requests.RequestException as e:
                if resp is None:
                    if attempt == MAX_RETRIES:
                        msg = f"{self.provider_name} error: no response received after retries"
                        logger.exception(msg)
                        raise ProxyProviderError(msg) from e
                    sleep_time = BACKOFF_FACTOR * (2 ** (attempt - 1))
                    logger.warning(f"Connection error, retrying in {sleep_time:.1f}s... ({e.__class__.__name__})")
                    time.sleep(sleep_time)
                    continue

                status = resp.status_code
                if status not in (429, 502, 503, 504):
                    msg = f"{self.provider_name} error: non-retriable failure (status {status})"
                    logger.exception(f"{msg}: {resp.text}")
                    raise ProxyProviderError(msg) from e
                if attempt == MAX_RETRIES:
                    msg = f"{self.provider_name} error: retry limit reached (status {status})"
                    logger.exception(f"{msg}: {resp.text}")
                    raise ProxyProviderError(msg) from e

                sleep_time = BACKOFF_FACTOR * (2 ** (attempt - 1))
                logger.warning(
                    f"Retryable {self.provider_name} error (status {status}), retrying in {sleep_time:.1f}s..."
                )
                time.sleep(sleep_time)

        try:
            resp_json = resp.json()
        except Exception as e:
            msg = f"{self.provider_name} error: invalid JSON in response"
            logger.exception(f"{msg}: {resp.text}")
            raise ProxyProviderError(msg) from e

        logger.info(f"Received response from {self.provider_name}: {json.dumps(resp_json, indent=2)}")

        if "choices" not in resp_json or not resp_json["choices"]:
            msg = f"{self.provider_name} error: unexpected response format"
            logger.exception(f"{msg}: {resp_json}")
            raise ProxyProviderError(msg)

        response_format = payload_dict.get("response_format", {})
        is_json_mode = response_format.get("type") in ("json_object", "json_schema")
        finish_reason = resp_json["choices"][0].get("finish_reason")
        if is_json_mode and finish_reason in ("length", "content_filter"):
            err = (
                f"{self.provider_name} error: response unusable (finish_reason={finish_reason}); "
                "increase max_tokens or review content policy"
            )
            logger.error(err)
            raise ProxyProviderError(err)

        msg = resp_json["choices"][0].get("message", {})
        usage = resp_json.get("usage", {})
        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

        return InferenceResponse(
            **resp_json,
            content=msg.get("content"),
            role=msg.get("role", "assistant"),
            tool_calls=msg.get("tool_calls"),
            input_tokens=usage.get("prompt_tokens", 0),
            cached_tokens=cached_tokens,
            output_tokens=usage.get("completion_tokens", 0),
        )


def get_provider_client(provider_name: str) -> BaseProviderClient:
    if provider_name == "chutes":
        try:
            from .chutes_client import ChutesClient
        except ImportError:
            from chutes_client import ChutesClient

        return ChutesClient()

    if provider_name == "openrouter":
        try:
            from .openrouter_client import OpenRouterClient
        except ImportError:
            from openrouter_client import OpenRouterClient

        return OpenRouterClient()

    raise ProxyProviderError(f"Unsupported provider: {provider_name}")
