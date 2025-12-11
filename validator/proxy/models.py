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
    max_tokens: int = Field(default=60000)
    temperature: float = Field(default=0.2)

class InferenceResponse(BaseModel):
    content: str
    role: Role
    input_tokens: int
    cached_tokens: int
    output_tokens: int
