"""Bounded public contracts for the mocked Agent vertical slice."""

from typing import Annotated, Literal, Self

from pydantic import BaseModel, ConfigDict, Field, model_validator

Text = Annotated[str, Field(min_length=1, max_length=8000)]
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
    revision: Annotated[int, Field(ge=1, le=8)]
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
    offset: Annotated[int, Field(ge=1, le=10000)] = 1
    limit: Annotated[int, Field(ge=1, le=200)] = 200


class PlanLimits(Strict):
    max_steps: Annotated[int, Field(ge=1, le=3)] = 3
    max_tool_calls: Annotated[int, Field(ge=1, le=3)] = 3
    timeout_seconds: Annotated[int, Field(ge=1, le=60)] = 30


class ProposedStep(Strict):
    title: Annotated[str, Field(min_length=1, max_length=200)]
    description: Text
    tool: Literal["read_file"]
    arguments: ReadArguments


class Proposal(Strict):
    objective: Text
    assumptions: Annotated[list[Text], Field(max_length=8)]
    limits: PlanLimits = Field(default_factory=PlanLimits)
    steps: Annotated[list[ProposedStep], Field(min_length=1, max_length=3)]

    @model_validator(mode="after")
    def within_limits(self) -> Self:
        if len(self.steps) > min(self.limits.max_steps, self.limits.max_tool_calls):
            raise ValueError("Steps exceed approved limits")
        return self


class PlanStep(ProposedStep):
    id: Key


class AgentPlan(Strict):
    id: Key
    revision: Annotated[int, Field(ge=1, le=8)]
    digest: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
    objective: Text
    assumptions: Annotated[list[Text], Field(max_length=9)]
    model: str
    reasoning_effort: Effort
    limits: PlanLimits
    steps: Annotated[list[PlanStep], Field(min_length=1, max_length=3)]
    access_policy: dict[str, str]


class AgentError(Strict):
    code: str
    message: str


class StepResult(Strict):
    step_id: Key
    status: Literal["running", "completed", "failed"]
    content: Annotated[str, Field(max_length=24000)]
    time: str
    error: AgentError | None = None
    path: str | None = None
    offset: int | None = None
    returned_lines: int | None = None
    truncated: bool = False


class AgentRun(Strict):
    id: Key
    request: Text
    created_at: str
    updated_at: str
    status: Status
    operation_id: Key | None = None
    plan: AgentPlan | None = None
    results: Annotated[list[StepResult], Field(max_length=3)] = Field(default_factory=list)
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
    runs: Annotated[list[AgentRun], Field(max_length=8)] = Field(default_factory=list)


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
