from fastapi import FastAPI, Header, HTTPException
from models import InferenceRequest, InferenceResponse
from chutes_client import call_chutes, ChutesError


app = FastAPI(title="Chutes Proxy")


@app.post("/inference", response_model=InferenceResponse)
async def inference(
    request: InferenceRequest,
    x_job_id: str = Header(default="unknown"),
    x_project_id: str = Header(default="unknown"),
):
    try:
        return call_chutes(request, x_job_id, x_project_id)
    except ChutesError as e:
        raise HTTPException(status_code=502, detail=str(e))
