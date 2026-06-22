import asyncio

from fastapi import FastAPI, Header, HTTPException
from fastapi.responses import JSONResponse

from base_client import ProxyProviderError, get_provider_client
from metrics import get_metrics_recorder, monotonic_ms
from models import InferenceProvider, InferenceRequest, InferenceResponse, ProxyMetricsContext


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
    x_agent_id: str = Header(default="unknown"),
    x_job_run_id: str | None = Header(default=None),
    x_request_phase: str = Header(default="unknown"),
):
    metrics = get_metrics_recorder()
    started_ms = monotonic_ms()
    ctx = None
    try:
        if not x_inference_api_key:
            raise HTTPException(status_code=422, detail="x-inference-api-key is required")
        provider = InferenceProvider(api_key=x_inference_api_key)
        client = get_provider_client(provider.name)
        if not request.model:
            request.model = client.default_model

        log_ctx = ProxyMetricsContext(
            agent_id=x_agent_id,
            job_run_id=x_job_run_id,
            phase=x_request_phase,
            req_model=request.model,
            model=request.model,
            provider=provider.name,
        )
        if x_job_run_id and x_job_run_id.strip():
            ctx = log_ctx

        async with _sem:
            response = await asyncio.to_thread(client.call, request, provider, ctx, log_ctx)
            if ctx is not None:
                metrics.record_proxy_complete(ctx, success=True, duration_ms=monotonic_ms() - started_ms)
            return response
    except ProxyProviderError as e:
        if ctx is not None:
            metrics.record_proxy_complete(ctx, success=False, duration_ms=monotonic_ms() - started_ms)
        raise HTTPException(status_code=502, detail=str(e))
    except Exception:
        if ctx is not None:
            metrics.record_proxy_complete(ctx, success=False, duration_ms=monotonic_ms() - started_ms)
        raise


@app.post("/metrics/job-runs/{job_run_id}/summary/reset")
async def reset_job_run_summary(job_run_id: str):
    get_metrics_recorder().reset_summary(job_run_id)
    return {"status": "ok", "job_run_id": job_run_id}


@app.get("/metrics/job-runs/{job_run_id}/summary")
async def get_job_run_summary(job_run_id: str):
    return get_metrics_recorder().get_summary(job_run_id)
