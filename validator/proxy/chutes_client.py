import os
import json
import time
import requests
from loggers.logger import get_logger
from models import InferenceRequest, InferenceResponse


logger = get_logger()


class ChutesError(Exception):
    pass


CHUTES_API_KEY = os.getenv("CHUTES_API_KEY")

CHUTES_API_URL = "https://llm.chutes.ai/v1/chat/completions"
DEFAULT_MODEL = "unsloth/gemma-3-12b-it"
TIMEOUT = 300
MAX_RETRIES = 5
BACKOFF_FACTOR = 1.5


def call_chutes(
    request: InferenceRequest,
    job_id: str = "unknown",
    project_key: str = "unknown",
    api_key: str = None
) -> InferenceResponse:

    if not request.model:
        request.model = DEFAULT_MODEL

    logger.info(f"Request from [J:{job_id}|P:{project_key}]")

    if not api_key:
        api_key = CHUTES_API_KEY

    headers = {"Authorization": f"Bearer {CHUTES_API_KEY}"}
    payload_dict = request.model_dump()
    resp = None

    payload_dict['response_format'] = {
        'type': 'json_object'
    }

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            logger.info(f"Sending request to Chutes. Attempt: {attempt}")
            resp = requests.post(
                CHUTES_API_URL,
                headers=headers,
                json=payload_dict,
                timeout=TIMEOUT,
            )
            resp.raise_for_status()
            break

        except requests.RequestException as e:
            # No response object
            if resp is None:
                msg = "Chutes error: no response received"
                logger.exception(msg)
                raise ChutesError(msg) from e

            status = resp.status_code

            # Non-retriable HTTP status
            if status not in (502, 429):
                msg = f"Chutes error: non-retriable failure (status {status})"
                logger.exception(f"{msg}: {resp.text}")
                raise ChutesError(msg) from e

            # Retryable HTTP status but out of retries
            if attempt == MAX_RETRIES:
                msg = f"Chutes error: retry limit reached (status {status})"
                logger.exception(f"{msg}: {resp.text}")
                raise ChutesError(msg) from e

            sleep_time = BACKOFF_FACTOR * (2 ** (attempt - 1))
            logger.warning(f"Retryable Chutes error (status {status}), retrying in {sleep_time:.1f}s...")
            time.sleep(sleep_time)

    try:
        resp_json = resp.json()

    except Exception as e:
        msg = "Chutes error: invalid JSON in response"
        logger.exception(f"{msg}: {resp.text}")
        raise ChutesError(msg) from e

    logger.info(f"Received response from Chutes: {json.dumps(resp_json, indent=2)}")

    if "choices" not in resp_json or not resp_json["choices"]:
        msg = "Chutes error: unexpected response format"
        logger.exception(f"{msg}: {resp_json}")
        raise ChutesError(msg)

    msg = resp_json["choices"][0]["message"]

    return InferenceResponse(
        content=msg["content"],
        role=msg["role"],
        input_tokens=resp_json["usage"]["prompt_tokens"],
        output_tokens=resp_json["usage"]["completion_tokens"],
    )
