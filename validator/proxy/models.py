from typing import Any

from pydantic import BaseModel, ConfigDict, Field


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
