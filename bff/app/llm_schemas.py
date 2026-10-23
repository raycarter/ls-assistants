"""Version-one private LLM journal contract; independent of coordinator/storage models."""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Code = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-z0-9_]+$")]
Phase = Literal["planning", "revision", "execution", "synthesis"]
Kind = Literal["intent", "dispatched", "output", "outcome", "validation"]
OutcomeStatus = Literal[
    "completed", "failed", "timed_out", "cancelled", "interrupted", "not_dispatched"
]


class Strict(BaseModel):
    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, hide_input_in_errors=True, allow_inf_nan=False
    )


class FunctionTool(Strict):
    """Reviewed Responses function declaration; never a built-in/MCP/auth config."""

    type: Literal["function"]
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    description: str | None = None
    parameters: dict[str, JsonValue]
    strict: bool | None = None


class FunctionToolChoice(Strict):
    type: Literal["function"]
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]


class ReasoningOptions(Strict):
    effort: Literal["none", "minimal", "low", "medium", "high", "xhigh"] | None = None
    summary: Literal["auto", "concise", "detailed"] | None = None


class TextFormat(Strict):
    type: Literal["text"]


class JSONObjectFormat(Strict):
    type: Literal["json_object"]


class JSONSchemaFormat(Strict):
    type: Literal["json_schema"]
    name: Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")]
    schema_: dict[str, JsonValue] = Field(alias="schema")
    description: str | None = None
    strict: bool | None = None


class TextOptions(Strict):
    format: (
        Annotated[TextFormat | JSONObjectFormat | JSONSchemaFormat, Field(discriminator="type")]
        | None
    ) = None
    verbosity: Literal["low", "medium", "high"] | None = None


class RequestBody(Strict):
    """Reviewed application body, shared by construction and durable intent validation.

    Transport/client/settings fields are structurally excluded. Opaque input,
    continuation, source text and JSON schema property names are not redacted.
    Provider body expansion requires an explicit reviewed schema change.
    """

    model: str = Field(min_length=1)
    input: JsonValue
    instructions: str | None = None
    reasoning: ReasoningOptions | None = None
    text: TextOptions | None = None
    tools: list[FunctionTool] | None = None
    tool_choice: Literal["none", "auto", "required"] | FunctionToolChoice | None = None
    parallel_tool_calls: bool | None = None
    stream: bool | None = None
    store: Literal[False]
    include: list[str] | None = None
    previous_response_id: str | None = None
    max_output_tokens: int | None = None
    temperature: float | None = None
    top_p: float | None = None
    truncation: str | None = None

    @field_validator("store", mode="before")
    @classmethod
    def no_provider_storage(cls, value):
        if value is not False:
            raise ValueError("Provider storage must be false")
        return value


class LLMIdentity(Strict):
    session_id: Key
    run_id: Key
    operation_id: Key
    plan_id: Key
    revision: Annotated[int, Field(ge=1)]
    phase: Phase
    stage_id: Key
    step_id: Key | None
    interaction_id: Key
    attempt_id: Key
    attempt: Annotated[int, Field(ge=1)]
    retry_of: Key | None
    backoff_seconds: Annotated[float, Field(ge=0)]

    @model_validator(mode="after")
    def correlation(self) -> Self:
        if (self.attempt == 1) != (self.retry_of is None):
            raise ValueError("Retry identity does not match attempt ordinal")
        if self.retry_of == self.attempt_id or (self.attempt == 1 and self.backoff_seconds):
            raise ValueError("Invalid retry lineage")
        if self.phase in {"planning", "revision"} and self.step_id is not None:
            raise ValueError("Planning has no execution step")
        if self.phase == "execution" and self.step_id is None:
            raise ValueError("Execution requires its known step identity")
        return self


class IntentData(Strict):
    request: dict[str, JsonValue]
    instructions_version: Annotated[str, Field(min_length=1)]

    @field_validator("request")
    @classmethod
    def reviewed_body(cls, value: dict[str, JsonValue]) -> dict[str, JsonValue]:
        # Validate without reconstructing/defaulting the canonical opaque body.
        # RequestBody requires an explicit store:false in every durable intent.
        RequestBody.model_validate(value)
        return value


class DispatchedData(Strict):
    """Authorization marker, not evidence that the remote provider executed anything."""


class OutputData(Strict):
    index: Annotated[int, Field(ge=1)]
    kind: Literal["event", "response", "error_body"]
    body: JsonValue
    reported_model: str | None = None
    provider_status: str | None = None
    usage: JsonValue = None
    failed: bool = False


class OutcomeData(Strict):
    status: OutcomeStatus
    dispatched: bool | None
    output_count: Annotated[int, Field(ge=0)]
    reported_model: str | None
    provider_status: str | None
    usage: JsonValue
    error_code: Code | None


class ValidationData(Strict):
    status: Literal["valid", "invalid"]
    error_code: Code | None

    @model_validator(mode="after")
    def valid_has_no_error(self) -> Self:
        if (self.status == "valid") != (self.error_code is None):
            raise ValueError("Validation status and error code disagree")
        return self


DATA_MODELS = {
    "intent": IntentData,
    "dispatched": DispatchedData,
    "output": OutputData,
    "outcome": OutcomeData,
    "validation": ValidationData,
}


class LLMRecord(Strict):
    llm_audit_version: Literal[1]
    kind: Kind
    identity: LLMIdentity
    at: str
    data: dict[str, JsonValue]

    @field_validator("llm_audit_version", mode="before")
    @classmethod
    def integer_version(cls, value):
        if type(value) is not int:
            raise ValueError("Expected an integer audit version")
        return value

    @field_validator("at")
    @classmethod
    def utc_timestamp(cls, value: str) -> str:
        try:
            stamp = datetime.fromisoformat(value)
        except ValueError:
            raise ValueError("Expected a UTC timestamp") from None
        if stamp.utcoffset() != timedelta(0):
            raise ValueError("Expected a UTC timestamp")
        return value

    @model_validator(mode="after")
    def typed_data(self) -> Self:
        DATA_MODELS[self.kind].model_validate(self.data)
        return self
