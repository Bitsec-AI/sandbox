from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def derive_provider_from_api_key(api_key: str) -> str:
    if api_key.startswith("cpk_"):
        return "chutes"
    if api_key.startswith("sk-or-"):
        return "openrouter"
    raise ValueError("Unsupported inference API key prefix. Expected 'cpk_' or 'sk-or-'.")


class InferenceProvider(BaseModel):
    model_config = ConfigDict(extra="forbid")

    api_key: str
    name: str | None = None

    @model_validator(mode="after")
    def set_or_validate_name(self):
        derived = derive_provider_from_api_key(self.api_key)
        if self.name and self.name != derived:
            raise ValueError(f"Provider name {self.name!r} does not match the API key prefix.")
        self.name = derived
        return self


class InferenceRequest(BaseModel):
    """OpenAI-compatible chat completions request.

    Any extra fields (tools, tool_choice, response_format, reasoning_effort,
    max_completion_tokens, etc.) are passed through to the upstream API.
    """

    model_config = ConfigDict(extra="allow")

    model: str | None = None
    messages: list[dict[str, Any]]
    max_tokens: int = Field(default=4096)
    temperature: float = Field(default=0.2)


# --- OpenAI-compatible response models ---


class Usage(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt_tokens: int = 0
    completion_tokens: int = 0
    prompt_tokens_details: dict[str, Any] | None = None


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")

    id: str
    type: str = "function"
    function: dict[str, Any]


class MessageContent(BaseModel):
    model_config = ConfigDict(extra="allow")

    role: str = "assistant"
    content: str | None = None
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None


class Choice(BaseModel):
    model_config = ConfigDict(extra="allow")

    index: int = 0
    message: MessageContent
    finish_reason: str | None = None


class InferenceResponse(BaseModel):
    """OpenAI-compatible chat completions response with flattened snapshot fields.

    Extra fields (system_fingerprint, created, object, etc.) are preserved
    via extra="allow" for full pass-through fidelity.
    """

    model_config = ConfigDict(extra="allow")

    id: str | None = None
    model: str | None = None
    choices: list[Choice]
    usage: Usage | None = None

    # Flattened snapshot fields
    content: str | None = None
    role: str = "assistant"
    tool_calls: list[ToolCall] | None = None
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0


class ProxyMetricsContext(BaseModel):
    agent_id: str = "unknown"
    job_run_id: str = "unknown"
    phase: str = "unknown"
    req_model: str = "unknown"
    model: str = "unknown"
    provider: str = "unknown"

    @field_validator("agent_id", "job_run_id", "req_model", "model", "provider", mode="before")
    @classmethod
    def default_unknown(cls, value: Any) -> str:
        if value is None:
            return "unknown"
        value = str(value).strip()
        return value or "unknown"

    @field_validator("provider", mode="after")
    @classmethod
    def default_provider(cls, value: str) -> str:
        return value.lower()

    @field_validator("phase", mode="before")
    @classmethod
    def default_phase(cls, value: Any) -> str:
        value = cls.default_unknown(value)
        if value in {"execution", "evaluation"}:
            return value
        return "unknown"


class LLMAttemptMetrics(BaseModel):
    upstream_provider: str = "unknown"
    status_code: int = 0
    duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    retry_num: int = 0
    finish_reason: str = ""
    error_type: str = ""


class ProxySummaryRow(BaseModel):
    phase: str = "unknown"
    req_model: str = "unknown"
    model: str = "unknown"
    provider: str = "unknown"
    requests: int = 0
    success: int = 0
    error: int = 0
    llm_requests: int = 0
    llm_success: int = 0
    llm_error: int = 0
    retries: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cached_tokens: int = 0
    duration_ms_total: int = 0
    duration_ms_max: int = 0
    status_codes: dict[str, int] = Field(default_factory=dict)

    def to_summary_dict(self) -> dict[str, Any]:
        payload = self.model_dump()
        payload["duration_ms_avg"] = int(self.duration_ms_total / self.requests) if self.requests else 0
        return payload
