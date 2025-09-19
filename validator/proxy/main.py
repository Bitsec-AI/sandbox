from fastapi import FastAPI, Request, Header, HTTPException
import os
import requests
import logging
from models import InferenceRequest, InferenceResponse, Message


logging.basicConfig(level=logging.INFO)
app = FastAPI(title="Chutes Proxy")

CHUTES_API_KEY = os.getenv("CHUTES_API_KEY")
if not CHUTES_API_KEY:
    raise RuntimeError("CHUTES_API_KEY environment variable is required!")

CHUTES_API_URL = "https://llm.chutes.ai/v1/chat/completions"
DEFAULT_MODEL = "unsloth/gemma-3-12b-it"
# DEFAULT_MODEL = "deepseek-ai/DeepSeek-V3.1"


@app.post("/inference", response_model=InferenceResponse)
async def inference(
    request: InferenceRequest,
    x_job_id: str = Header(default="unknown"),
    x_project_id: str = Header(default="unknown"),
):
    logging.info(f"Request from [J:{x_job_id}|P:{x_project_id}]")

    if not request.model:
        request.model = DEFAULT_MODEL

    headers = {
        "Authorization": f"Bearer {CHUTES_API_KEY}"
    }
    payload_dict = request.model_dump()

    try:
        resp = requests.post(CHUTES_API_URL, headers=headers, json=payload_dict)
        resp.raise_for_status()

    except requests.RequestException as e:
        logging.error(f"Chutes API error: {e}")
        raise HTTPException(status_code=502, detail=f"Chutes API error: {e}")

    resp_json = resp.json()
    if "choices" in resp_json and len(resp_json["choices"]):
        choice_message = resp_json["choices"][0]["message"]
        response = {
            "content": choice_message["content"],
            "role": choice_message["role"],
        }
        return response
