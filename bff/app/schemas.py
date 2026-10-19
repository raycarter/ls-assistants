from datetime import datetime
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field


class ChatMessage(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    role: Literal["user", "assistant"]
    content: str = Field(min_length=1)


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    session_id: str = Field(min_length=1)
    request_id: str = Field(min_length=1)
    model: str = Field(min_length=1)
    reasoning_effort: Literal["low", "medium", "high", "xhigh"]
    system_prompt: str | None
    messages: list[ChatMessage] = Field(min_length=1)
    generate_title: bool = False


class ModelsResponse(BaseModel):
    models: list[str]
    default_model: str


class CorrelatedEvent(BaseModel):
    session_id: str
    request_id: str
    message_id: str


class StartEvent(CorrelatedEvent):
    event: ClassVar[str] = "start"
    time: datetime


class SessionTitleEvent(CorrelatedEvent):
    event: ClassVar[str] = "session_title"
    title: str


class DeltaEvent(CorrelatedEvent):
    event: ClassVar[str] = "delta"
    text: str


class DoneEvent(CorrelatedEvent):
    event: ClassVar[str] = "done"
    time: datetime
    status: Literal["complete"] = "complete"


class ErrorEvent(CorrelatedEvent):
    event: ClassVar[str] = "error"
    code: str
    message: str


ChatEvent = StartEvent | SessionTitleEvent | DeltaEvent | DoneEvent | ErrorEvent


def encode_sse(event: ChatEvent, sequence: int) -> str:
    return (
        f"id: {event.message_id}:{sequence}\n"
        f"event: {event.event}\n"
        f"data: {event.model_dump_json()}\n\n"
    )
