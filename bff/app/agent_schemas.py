"""Agent commands and durable models, including legacy approval material."""

from datetime import datetime, timedelta
from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1)]
Key = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")]
Effort = Literal["low", "medium", "high", "xhigh"]
Status = Literal[
    "planning",
    "awaiting_clarification",
    "awaiting_approval",
    "approved",
    "executing",
    "cancelling",
    "completed",
    "rejected",
    "failed",
    "cancelled",
    "interrupted",
]
TERMINAL = {"completed", "rejected", "failed", "cancelled", "interrupted"}


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class Command(Strict):
    request_id: Key


class CreateSession(Command):
    model: Annotated[str, Field(min_length=1, max_length=128)]
    reasoning_effort: Effort


class PatchSession(Command):
    model: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    reasoning_effort: Effort | None = None
    title: Annotated[str, Field(min_length=1, max_length=200)] | None = None


class StartRun(Command):
    content: Text


class PlanIdentity(Command):
    plan_id: Key
    revision: Annotated[int, Field(ge=1)]
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]


class RejectPlan(PlanIdentity):
    kind: Literal["plan"]


class RejectClarification(Command):
    kind: Literal["clarification"]
    clarification_id: Key


class Feedback(PlanIdentity):
    kind: Literal["feedback"]
    content: Text


class ClarificationReply(RejectClarification):
    content: Text


Reject = Annotated[RejectPlan | RejectClarification, Field(discriminator="kind")]
Revise = Annotated[Feedback | ClarificationReply, Field(discriminator="kind")]


class Cancel(Command):
    operation_id: Key


class ReadArguments(Strict):
    workspace_id: Key
    path: Annotated[str, Field(min_length=1, max_length=500)]
    offset: Annotated[int, Field(ge=1)] = 1
    limit: Annotated[int, Field(ge=1, le=200)] = 200


class FetchArguments(Strict):
    url: str
    offset: Annotated[int, Field(ge=0)] = 0
    limit: Annotated[int, Field(ge=1, le=16000)] = 12000


class LegacyPlanLimits(Strict):
    """Historical signed material only; these values no longer govern execution."""

    max_steps: Annotated[int, Field(ge=1)] = 3
    max_tool_calls: Annotated[int, Field(ge=1)] = 3
    timeout_seconds: Annotated[int, Field(ge=1)] = 30


PlanLimits = LegacyPlanLimits


class ProposedStep(Strict):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Text
    tool: Literal["read_file", "web_fetch"]
    arguments: ReadArguments | FetchArguments

    @model_validator(mode="after")
    def matching_arguments(self) -> Self:
        expected = ReadArguments if self.tool == "read_file" else FetchArguments
        if not isinstance(self.arguments, expected):
            raise ValueError("Tool and arguments do not match")
        return self


class Proposal(Strict):
    objective: Text
    assumptions: list[Text]
    limits: LegacyPlanLimits | None = Field(default=None, exclude_if=lambda value: value is None)
    steps: Annotated[list[ProposedStep], Field(min_length=1)]


class PlanStep(ProposedStep):
    id: Key


class AgentPlan(Strict):
    id: Key
    revision: Annotated[int, Field(ge=1)]
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    objective: Text
    assumptions: list[Text]
    model: str
    reasoning_effort: Effort
    limits: LegacyPlanLimits | None = Field(default=None, exclude_if=lambda value: value is None)
    steps: Annotated[list[PlanStep], Field(min_length=1)]
    access_policy: dict[str, str]


class AgentError(Strict):
    code: str
    message: str


ArtifactId = Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]
Nonnegative = Annotated[int, Field(ge=0)]


class ArtifactPage(Strict):
    artifact_id: ArtifactId
    content: str
    offset: Nonnegative
    returned_chars: Nonnegative
    total_chars: Nonnegative
    next_offset: Nonnegative | None


class FetchMetadata(Strict):
    artifact_id: ArtifactId
    requested_url: str
    final_url: str
    title: str | None
    status_code: int
    content_type: str
    fetched_at: str
    redirects: list[str]
    total_chars: Nonnegative
    offset: Nonnegative
    returned_chars: Nonnegative
    next_offset: Nonnegative | None

    @model_validator(mode="after")
    def utc_timestamp(self) -> Self:
        try:
            stamp = datetime.fromisoformat(self.fetched_at)
        except ValueError:
            raise ValueError("Expected a UTC timestamp") from None
        if stamp.utcoffset() != timedelta(0):
            raise ValueError("Expected a UTC timestamp")
        return self


class StepResult(Strict):
    step_id: Key
    status: Literal["running", "completed", "failed"]
    content: str
    time: str
    error: AgentError | None = None
    path: str | None = None
    offset: int | None = None
    returned_lines: int | None = None
    truncated: bool = False
    fetch: FetchMetadata | None = None


class AgentRun(Strict):
    id: Key
    request: Text
    created_at: str
    updated_at: str
    status: Status
    operation_id: Key | None = None
    plan: AgentPlan | None = None
    results: list[StepResult] = Field(default_factory=list)
    answer: str | None = None
    error: AgentError | None = None
    clarification: None = None


class AgentSession(Strict):
    id: str
    title: str
    model: str
    reasoning_effort: Effort
    created_at: str
    updated_at: str
    archived: bool = False
    archive_pending: bool = False
    sequence: int = 0
    runs: list[AgentRun] = Field(default_factory=list)


class AgentEvent(Strict):
    schema_version: Literal[1] = 1
    sequence: int
    time: str
    session_id: str
    request_id: str
    run_id: str | None
    operation_id: str | None
    type: Literal[
        "session.created",
        "session.updated",
        "session.archive_pending",
        "session.archived",
        "run.planning",
        "planner.attempt",
        "planner.result",
        "run.awaiting_approval",
        "run.approved",
        "run.executing",
        "tool.intent",
        "tool.result",
        "run.completed",
        "run.rejected",
        "run.cancelling",
        "run.cancelled",
        "run.interrupted",
        "run.failed",
    ]
    session: AgentSession


class Claim(Strict):
    request_id: str
    digest: str


class JournalRecord(Strict):
    event: AgentEvent
    claim: Claim | None = None
    audit: dict = Field(default_factory=dict)
