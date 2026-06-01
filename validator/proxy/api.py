import asyncio
import sys
from pathlib import Path

# Ensure project root is on path when running from validator/proxy (e.g. uvicorn api:app)
try:
    _root = Path(__file__).resolve().parents[2]
    if str(_root) not in sys.path:
        sys.path.insert(0, str(_root))
except IndexError:
    pass

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from models import InferenceProvider, InferenceRequest, InferenceResponse
from base_client import ProxyProviderError, get_provider_client


app = FastAPI(title="Inference Proxy")


@app.get("/")
async def root():
    """Root endpoint providing proxy information."""
    return JSONResponse(
        {
            "service": "Inference Proxy",
            "description": "Inference proxy for LLM requests",
            "endpoints": {
                "POST /inference": "Submit inference requests",
                "GET /docs": "Interactive API documentation",
                "GET /openapi.json": "OpenAPI schema",
            },
            "status": "running",
        }
    )


INFER_CONCURRENCY = 8  # per worker
_sem = asyncio.Semaphore(INFER_CONCURRENCY)


@app.post("/inference", response_model=InferenceResponse)
async def inference(
    request: InferenceRequest,
    x_inference_api_key: str | None = Header(default=None),
    x_job_id: str = Header(default="unknown"),
    x_project_id: str = Header(default="unknown"),
):
    try:
        if not x_inference_api_key:
            raise HTTPException(status_code=422, detail="x-inference-api-key is required")
        provider = InferenceProvider(api_key=x_inference_api_key)
        async with _sem:
            client = get_provider_client(provider.name)
            return await asyncio.to_thread(client.call, request, provider, x_job_id, x_project_id)
    except ProxyProviderError as e:
        raise HTTPException(status_code=502, detail=str(e))
