import json
import time
import requests
from typing import Any
from loggers.logger import get_logger

try:
    from .models import InferenceRequest, InferenceResponse
except ImportError:
    from models import InferenceRequest, InferenceResponse


logger = get_logger()


class ChutesError(Exception):
    pass


CHUTES_API_URL = "https://llm.chutes.ai/v1/chat/completions"
DEFAULT_MODEL = "unsloth/gemma-3-12b-it"
TIMEOUT = 300
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.5

# Global session for connection reuse (one per worker process)
SESSION = requests.Session()


def _backoff_sleep(attempt: int, reason: str):
    """Exponential backoff for server-side errors (429, 502, connection)."""
    sleep_time = BACKOFF_FACTOR * (2 ** (attempt - 1))
    logger.warning(f"{reason}, retrying in {sleep_time:.1f}s...")
    time.sleep(sleep_time)


def call_chutes(
    request: InferenceRequest,
    job_id: str = "unknown",
    project_key: str = "unknown",
    api_key: str = None,
) -> InferenceResponse:
    if not request.model:
        request.model = DEFAULT_MODEL

    logger.info(f'Request from [J:{job_id}|P:{project_key}] | model="{request.model}"')

    if not api_key:
        raise ChutesError("CHUTES_API_KEY is required. Pass x-chutes-api-key header.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-Identifier": "Bitsec",
    }
    payload_dict = request.model_dump()

    # Default to JSON mode unless caller provided a valid response_format dict
    if not isinstance(payload_dict.get("response_format"), dict):
        payload_dict["response_format"] = {"type": "json_object"}

    response_format = payload_dict.get("response_format", {})
    is_json_mode = response_format.get("type") in ("json_object", "json_schema")

    last_error = None

    for attempt in range(1, MAX_RETRIES + 1):
        resp = None

        # --- HTTP request ---
        try:
            logger.info(f"Sending request to Chutes. Attempt: {attempt}")
            resp = SESSION.post(
                CHUTES_API_URL,
                headers=headers,
                json=payload_dict,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()

        except requests.RequestException as e:
            if resp is None:
                last_error = f"Connection error: {e.__class__.__name__}"
                if attempt < MAX_RETRIES:
                    _backoff_sleep(attempt, last_error)
                    continue
                raise ChutesError(f"Chutes error: no response after {MAX_RETRIES} retries") from e

            status = resp.status_code
            if status not in (502, 429):
                msg = f"Chutes error: non-retriable failure (status {status})"
                logger.error(f"{msg}: {resp.text[:300]}")
                raise ChutesError(msg) from e

            last_error = f"HTTP {status}"
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt, f"Retriable error (status {status})")
                continue
            raise ChutesError(f"Chutes error: retry limit reached (status {status})") from e

        # --- Parse JSON response ---
        try:
            resp_json = resp.json()
        except Exception as e:
            last_error = f"Invalid JSON: {resp.text[:200]}"
            logger.error(f"Chutes error: {last_error}")
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt, "Invalid JSON response")
                continue
            raise ChutesError(f"Chutes error: invalid JSON after {MAX_RETRIES} retries") from e

        served_model = resp_json.get("model", "unknown")

        if "choices" not in resp_json or not resp_json["choices"]:
            last_error = f"No choices in response (model={served_model})"
            logger.error(f"Chutes error: {last_error}")
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt, last_error)
                continue
            raise ChutesError(f"Chutes error: {last_error}")

        finish_reason = resp_json["choices"][0].get("finish_reason")
        msg_obj = resp_json["choices"][0].get("message", {})
        content = msg_obj.get("content")

        # --- Check finish_reason (server-side truncation) ---
        if is_json_mode and finish_reason in ("length", "content_filter"):
            last_error = (
                f"Unusable response (model={served_model}, finish_reason={finish_reason})"
            )
            logger.error(f"Chutes error: {last_error}")
            if attempt < MAX_RETRIES:
                _backoff_sleep(attempt, last_error)
                continue
            raise ChutesError(f"Chutes error: {last_error}")

        # --- Check null/empty content (model-level flake — no backoff) ---
        if is_json_mode and not (content or "").strip():
            last_error = (
                f"Null/empty content (model={served_model}, finish_reason={finish_reason})"
            )
            logger.warning(f"Chutes: {last_error}, retrying immediately...")
            # No sleep — this is model flakiness, not server overload.
            # Re-rolling immediately maximizes chance of hitting a working response.
            continue

        # --- All checks passed ---
        logger.info(
            f"Chutes OK (model={served_model}, finish_reason={finish_reason}, "
            f"content_len={len(content or '')})"
        )

        usage = resp_json.get("usage", {})
        cached_tokens = (usage.get("prompt_tokens_details") or {}).get("cached_tokens", 0)

        return InferenceResponse(
            **resp_json,
            content=content,
            role=msg_obj.get("role", "assistant"),
            tool_calls=msg_obj.get("tool_calls"),
            input_tokens=usage.get("prompt_tokens", 0),
            cached_tokens=cached_tokens,
            output_tokens=usage.get("completion_tokens", 0),
        )

    raise ChutesError(f"Chutes error: exhausted {MAX_RETRIES} retries ({last_error})")
