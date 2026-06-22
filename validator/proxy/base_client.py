import json
import time
from abc import ABC, abstractmethod

import requests
from loggers.logger import get_logger

from metrics import get_metrics_recorder, monotonic_ms
from models import LLMAttemptMetrics, ProxyMetricsContext, InferenceProvider, InferenceRequest, InferenceResponse


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

    def _upstream_provider_from_response(self, resp_json: dict) -> str:
        provider = resp_json.get("provider")
        if isinstance(provider, dict):
            return str(provider.get("name") or provider.get("slug") or self.provider_name)
        if provider:
            return str(provider)
        return self.provider_name

    def _error_type_for_status(self, status_code: int) -> str:
        if status_code == 429:
            return "rate_limited"
        if status_code == 0:
            return "connection_error"
        return "provider_error"

    def _build_llm_attempt(
        self,
        *,
        status_code: int,
        duration_ms: int,
        retry_num: int,
        resp_json: dict | None = None,
        error_type: str = "",
    ) -> LLMAttemptMetrics:
        resp_json = resp_json or {}
        usage = resp_json.get("usage") or {}
        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)
        choices = resp_json.get("choices") or []
        finish_reason = choices[0].get("finish_reason") if choices else ""

        return LLMAttemptMetrics(
            upstream_provider=self._upstream_provider_from_response(resp_json),
            status_code=status_code,
            duration_ms=duration_ms,
            input_tokens=usage.get("prompt_tokens", 0),
            output_tokens=usage.get("completion_tokens", 0),
            cached_tokens=cached_tokens,
            retry_num=retry_num,
            finish_reason=finish_reason or "",
            error_type=error_type,
        )

    def _flush_llm_attempts(self, ctx: ProxyMetricsContext | None, attempts: list[LLMAttemptMetrics]) -> None:
        if ctx is None:
            return
        recorder = get_metrics_recorder()
        for attempt in attempts:
            recorder.record_llm_attempt(ctx, attempt)

    def call(
        self,
        request: InferenceRequest,
        provider: InferenceProvider,
        metrics_ctx: ProxyMetricsContext | None = None,
        log_ctx: ProxyMetricsContext | None = None,
    ) -> InferenceResponse:
        if not request.model:
            request.model = self.default_model
        log_ctx = log_ctx or metrics_ctx
        if metrics_ctx is not None:
            metrics_ctx.req_model = request.model
            metrics_ctx.model = request.model
        if log_ctx is not None:
            log_ctx.req_model = request.model
            log_ctx.model = request.model
        log_agent_id = log_ctx.agent_id if log_ctx is not None else "unknown"
        log_job_run_id = log_ctx.job_run_id if log_ctx is not None else "unknown"
        log_phase = log_ctx.phase if log_ctx is not None else "unknown"

        logger.info(
            f"Request from [A:{log_agent_id}|JR:{log_job_run_id}|Phase:{log_phase}] | "
            f'provider="{self.provider_name}" | model="{request.model}"'
        )

        headers = self.build_headers(provider.api_key)
        payload_dict = request.model_dump(exclude_none=True)

        if not isinstance(payload_dict.get("response_format"), dict):
            payload_dict["response_format"] = {"type": "json_object"}

        resp = None
        final_attempt_duration_ms = 0
        final_retry_num = 0
        llm_attempts = []
        for attempt in range(1, MAX_RETRIES + 1):
            attempt_started_ms = monotonic_ms()
            try:
                logger.info(f"Sending request to {self.provider_name}. Attempt: {attempt}")
                resp = None
                resp = SESSION.post(
                    self.api_url,
                    headers=headers,
                    json=payload_dict,
                    timeout=TIMEOUT,
                )
                resp.raise_for_status()
                final_attempt_duration_ms = monotonic_ms() - attempt_started_ms
                final_retry_num = attempt - 1
                break
            except requests.RequestException as e:
                duration_ms = monotonic_ms() - attempt_started_ms
                if resp is None:
                    llm_attempts.append(
                        self._build_llm_attempt(
                            status_code=0,
                            duration_ms=duration_ms,
                            retry_num=attempt - 1,
                            error_type=e.__class__.__name__,
                        )
                    )
                    if attempt == MAX_RETRIES:
                        self._flush_llm_attempts(metrics_ctx, llm_attempts)
                        msg = f"{self.provider_name} error: no response received after retries"
                        logger.exception(msg)
                        raise ProxyProviderError(msg) from e
                    sleep_time = BACKOFF_FACTOR * (2 ** (attempt - 1))
                    logger.warning(f"Connection error, retrying in {sleep_time:.1f}s... ({e.__class__.__name__})")
                    time.sleep(sleep_time)
                    continue

                status = resp.status_code
                llm_attempts.append(
                    self._build_llm_attempt(
                        status_code=status,
                        duration_ms=duration_ms,
                        retry_num=attempt - 1,
                        error_type=self._error_type_for_status(status),
                    )
                )
                if status not in (429, 502, 503, 504):
                    self._flush_llm_attempts(metrics_ctx, llm_attempts)
                    msg = f"{self.provider_name} error: non-retriable failure (status {status})"
                    logger.exception(f"{msg}: {resp.text}")
                    raise ProxyProviderError(msg) from e
                if attempt == MAX_RETRIES:
                    self._flush_llm_attempts(metrics_ctx, llm_attempts)
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
            llm_attempts.append(
                self._build_llm_attempt(
                    status_code=resp.status_code,
                    duration_ms=final_attempt_duration_ms,
                    retry_num=final_retry_num,
                    error_type="invalid_json",
                )
            )
            self._flush_llm_attempts(metrics_ctx, llm_attempts)
            msg = f"{self.provider_name} error: invalid JSON in response"
            logger.exception(f"{msg}: {resp.text}")
            raise ProxyProviderError(msg) from e

        logger.info(f"Received response from {self.provider_name}: {json.dumps(resp_json, indent=2)}")

        if metrics_ctx is not None and resp_json.get("model"):
            metrics_ctx.model = str(resp_json["model"])

        if "choices" not in resp_json or not resp_json["choices"]:
            llm_attempts.append(
                self._build_llm_attempt(
                    status_code=resp.status_code,
                    duration_ms=final_attempt_duration_ms,
                    retry_num=final_retry_num,
                    resp_json=resp_json,
                    error_type="unexpected_response",
                )
            )
            self._flush_llm_attempts(metrics_ctx, llm_attempts)
            msg = f"{self.provider_name} error: unexpected response format"
            logger.exception(f"{msg}: {resp_json}")
            raise ProxyProviderError(msg)

        response_format = payload_dict.get("response_format", {})
        is_json_mode = response_format.get("type") in ("json_object", "json_schema")
        finish_reason = resp_json["choices"][0].get("finish_reason")
        if is_json_mode and finish_reason in ("length", "content_filter"):
            llm_attempts.append(
                self._build_llm_attempt(
                    status_code=resp.status_code,
                    duration_ms=final_attempt_duration_ms,
                    retry_num=final_retry_num,
                    resp_json=resp_json,
                    error_type=f"finish_reason_{finish_reason}",
                )
            )
            self._flush_llm_attempts(metrics_ctx, llm_attempts)
            err = (
                f"{self.provider_name} error: response unusable (finish_reason={finish_reason}); "
                "increase max_tokens or review content policy"
            )
            logger.error(err)
            raise ProxyProviderError(err)

        msg = resp_json["choices"][0].get("message", {})
        usage = resp_json.get("usage", {})
        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

        llm_attempts.append(
            self._build_llm_attempt(
                status_code=resp.status_code,
                duration_ms=final_attempt_duration_ms,
                retry_num=final_retry_num,
                resp_json=resp_json,
            )
        )
        self._flush_llm_attempts(metrics_ctx, llm_attempts)

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
        from chutes_client import ChutesClient

        return ChutesClient()

    if provider_name == "openrouter":
        from openrouter_client import OpenRouterClient

        return OpenRouterClient()

    raise ProxyProviderError(f"Unsupported provider: {provider_name}")
