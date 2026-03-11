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

try:
    from .models import InferenceRequest, InferenceResponse
    from .chutes_client import call_chutes, ChutesError
except ImportError:
    from models import InferenceRequest, InferenceResponse
    from chutes_client import call_chutes, ChutesError


app = FastAPI(title="Chutes Proxy")


@app.get("/")
async def root():
    """Root endpoint providing proxy information."""
    return JSONResponse(
        {
            "service": "Chutes Proxy",
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
    x_job_id: str = Header(default="unknown"),
    x_project_id: str = Header(default="unknown"),
):
    try:
        async with _sem:
            # Run the blocking call_chutes function in a thread pool to allow parallel requests
            return await asyncio.to_thread(call_chutes, request, x_job_id, x_project_id)
    except ChutesError as e:
        raise HTTPException(status_code=502, detail=str(e))