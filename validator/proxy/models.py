from enum import Enum
from typing import Any
from pydantic import BaseModel, Field


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"
    tool = "tool"


class Message(BaseModel):
    role: Role
    content: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None


class InferenceRequest(BaseModel):
    model: str | None
    messages: list[Message]
    max_tokens: int = Field(default=4096)
    temperature: float = Field(default=0.2)
    tools: list[dict[str, Any]] | None = None
    tool_choice: str | None = None


class InferenceResponse(BaseModel):
    content: str | None = None
    role: Role
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    tool_calls: list[dict[str, Any]] | None = None
