from enum import Enum
from pydantic import BaseModel, Field


class Role(str, Enum):
    system = "system"
    user = "user"
    assistant = "assistant"


class Message(BaseModel):
    role: Role
    content: str


class InferenceRequest(BaseModel):
    model: str | None
    messages: list[Message]
    max_tokens: int = Field(default=4096)
    temperature: float = Field(default=0.2)
    json_response: bool = Field(default=True)


class InferenceResponse(BaseModel):
    content: str
    role: Role
    input_tokens: int
    cached_tokens: int
    output_tokens: int
    model: str | None = None
    finish_reason: str | None = None
    chutes_id: str | None = None
