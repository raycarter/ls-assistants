"""Owned, single-dispatch application-observable LLM boundary.

No SDK/provider is configured here. A transport must implement exactly one send,
await each observation callback before consuming/advancing the stream, and return
any final observation even when it suppresses cancellation. A future SDK adapter
must demonstrate max_retries=0 (including underlying HTTP retry mechanisms).
Bodies are private opaque evidence, not secret-scrubbed text or hidden reasoning.
"""

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal, Protocol

from pydantic import JsonValue

from app.agent_files import AgentFault
from app.agent_schemas import Proposal
from app.llm_schemas import LLMIdentity, LLMRecord, OutcomeData, RequestBody

if TYPE_CHECKING:
    from app.agent import PlanningInput

Record = Callable[[str, dict], Awaitable[None]]


class AuthorizationRevoked(AgentFault):
    """An authorization write was revoked BEFORE append, not a recording failure."""

    def __init__(self):
        super().__init__("operation_stopped", "LLM authorization was revoked.")


@dataclass(frozen=True, init=False)
class CanonicalRequest:
    """Serialize once; the exact same immutable UTF-8 bytes enter the transport."""

    body_bytes: bytes
    instructions_version: str

    def __init__(self, body: dict, *, instructions_version: str):
        checked = RequestBody.model_validate({"store": False, **body}).model_dump(
            mode="json", exclude_unset=True, by_alias=True
        )
        if not isinstance(instructions_version, str) or not instructions_version:
            raise ValueError("An instructions version is required")
        object.__setattr__(
            self,
            "body_bytes",
            json.dumps(checked, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode(
                "utf-8"
            ),
        )
        object.__setattr__(self, "instructions_version", instructions_version)

    @property
    def body(self) -> dict:
        return json.loads(self.body_bytes)


@dataclass(frozen=True)
class Observation:
    """One independent observable payload, never a cumulative stream snapshot.

    Metadata is explicitly supplied by the adapter, separate from the opaque body.
    Never pass HTTP headers, exception objects/strings, clients or settings here.
    """

    kind: Literal["event", "response", "error_body"]
    body: JsonValue
    reported_model: str | None = None
    provider_status: str | None = None
    usage: JsonValue = None
    failed: bool = False


Observe = Callable[[Observation], Awaitable[None]]


class SingleDispatchTransport(Protocol):
    async def send_once(self, request: bytes, observe: Observe) -> Observation | None:
        """One send, no retries; emit every event or return the complete response body."""
        ...


class ProviderBodyFailure(Exception):
    """Explicit opt-in body-only failure; generic exception text is never evidence."""

    def __init__(self, body: JsonValue):
        super().__init__("Provider returned an explicit error body")
        self.observation = Observation("error_body", body, failed=True)


@dataclass(frozen=True)
class AttemptResult:
    identity: LLMIdentity
    outputs: tuple[Observation, ...]
    outcome: OutcomeData


class AttemptRunner:
    """Callback-bound owner for one interaction and explicit sequential attempts.

    Cancellation revokes dispatch/advancement, but does not abandon transport or
    recording ownership. Timeouts bound a send attempt, not cleanup or planning.
    """

    def __init__(
        self,
        *,
        record: Record,
        dispatch_check: Callable[[], None],
        timeout_seconds: float,
        recording_failed: Callable[[], None],
    ):
        if (
            isinstance(timeout_seconds, bool)
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("Attempt timeout must be positive and finite")
        self.record = record
        self.dispatch_check = dispatch_check
        self.timeout_seconds = timeout_seconds
        self.recording_failed = recording_failed
        self.stop_event = asyncio.Event()
        self.stop_reason = "cancelled"
        self.poisoned = False
        self.active: asyncio.Task | None = None
        self.previous: AttemptResult | None = None
        self.validated: dict[str, bool] = {}
        self.attempt_ids: set[str] = set()

    def stop(self, reason: str = "cancelled"):
        if not self.stop_event.is_set():
            self.stop_reason = reason
            self.stop_event.set()

    def check(self):
        if self.poisoned:
            raise AgentFault("storage_unavailable", "LLM recording is unavailable.", 503)
        if self.stop_event.is_set():
            raise AgentFault("operation_stopped", "Operation stopped.")

    async def _drain(self, task: asyncio.Task):
        # Repeated native waiter cancellation cannot release an owned worker.
        while not task.done():
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                self.stop()
        return task.result()

    async def _record(self, identity: LLMIdentity, kind: str, data: dict):
        async def write():
            try:
                audit = LLMRecord(
                    llm_audit_version=1,
                    kind=kind,
                    identity=identity,
                    at=datetime.now(UTC).isoformat(),
                    data=data,
                ).model_dump(mode="json")
                await self.record("llm." + kind, audit)
            except (Exception, asyncio.CancelledError) as exc:
                if isinstance(exc, AuthorizationRevoked) and kind in {"intent", "dispatched"}:
                    self.stop()
                    raise
                self.poisoned = True
                self.stop("interrupted")
                self.recording_failed()
                raise AgentFault(
                    "storage_unavailable", "LLM recording failed; operation stopped.", 503
                ) from None

        task = asyncio.create_task(write())
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            self.stop()
            await self._drain(task)
            raise

    def _lineage(self, identity: LLMIdentity):
        LLMIdentity.model_validate(identity.model_dump())
        if identity.attempt_id in self.attempt_ids:
            raise ValueError("Attempt identity was already used")
        previous = self.previous
        if previous is None:
            if identity.attempt != 1:
                raise ValueError("The first attempt must start a new interaction")
            return
        before = previous.identity
        stable = set(LLMIdentity.model_fields) - {
            "attempt_id",
            "attempt",
            "retry_of",
            "backoff_seconds",
        }
        if (
            any(getattr(before, key) != getattr(identity, key) for key in stable)
            or identity.attempt != before.attempt + 1
            or identity.retry_of != before.attempt_id
            or identity.attempt_id == before.attempt_id
            or not (
                previous.outcome.status == "failed"
                or (
                    previous.outcome.status == "completed"
                    and self.validated.get(before.attempt_id) is False
                )
            )
        ):
            raise ValueError(
                "Retry requires a failed or durably invalid attempt in this interaction"
            )

    async def run(
        self, identity: LLMIdentity, request: CanonicalRequest, transport: SingleDispatchTransport
    ) -> AttemptResult:
        self.check()
        if self.active is not None:
            raise ValueError("An attempt is already owned")
        self._lineage(identity)
        self.attempt_ids.add(identity.attempt_id)
        task = self.active = asyncio.create_task(self._attempt(identity, request, transport))
        try:
            result = await asyncio.shield(task)
            self.previous = result
            return result
        except asyncio.CancelledError:
            self.stop()
            await self._drain(task)
            raise
        except Exception:
            self.stop("interrupted")
            raise
        finally:
            self.active = None

    async def _attempt(self, identity, request, transport) -> AttemptResult:
        outputs: list[Observation] = []
        retained: set[asyncio.Task] = set()
        output_lock = asyncio.Lock()
        output_count = 0
        dispatched = False
        failure = False
        reported_model = provider_status = usage = None

        async def retain(observation: Observation):
            nonlocal output_count, reported_model, provider_status, usage, failure
            async with output_lock:
                # Detach mutable adapter objects before any recording await.
                body = json.loads(json.dumps(observation.body, ensure_ascii=False, allow_nan=False))
                item = Observation(
                    observation.kind,
                    body,
                    observation.reported_model,
                    observation.provider_status,
                    json.loads(json.dumps(observation.usage, allow_nan=False)),
                    observation.failed,
                )
                outputs.append(item)
                await self._record(
                    identity,
                    "output",
                    {
                        "index": output_count + 1,
                        "kind": item.kind,
                        "body": item.body,
                        "reported_model": item.reported_model,
                        "provider_status": item.provider_status,
                        "usage": item.usage,
                        "failed": item.failed,
                    },
                )
                output_count += 1
                if item.reported_model is not None:
                    reported_model = item.reported_model
                if item.provider_status is not None:
                    provider_status = item.provider_status
                if item.usage is not None:
                    usage = item.usage
                failure |= (
                    item.failed
                    or item.kind == "error_body"
                    or item.provider_status in {"failed", "incomplete", "refused"}
                )

        async def observe(observation: Observation):
            task = asyncio.create_task(retain(observation))
            retained.add(task)
            try:
                await asyncio.shield(task)
            except asyncio.CancelledError:
                await self._drain(task)
                raise

        async def dispatch():
            nonlocal dispatched
            # This scheduled owner is the last authorization boundary. NO await
            # between these synchronous checks and entry into send_once.
            self.check()
            self.dispatch_check()
            dispatched = True
            return await transport.send_once(request.body_bytes, observe)

        await self._record(
            identity,
            "intent",
            {"request": request.body, "instructions_version": request.instructions_version},
        )
        if identity.backoff_seconds and not self.stop_event.is_set():
            await self._backoff(identity.backoff_seconds)
        call = stop = None
        status, error_code = "not_dispatched", "operation_stopped"
        try:
            if not self.stop_event.is_set():
                await self._record(identity, "dispatched", {})
                call = asyncio.create_task(dispatch())
                stop = asyncio.create_task(self.stop_event.wait())
                done, _ = await asyncio.wait(
                    {call, stop}, timeout=self.timeout_seconds, return_when=asyncio.FIRST_COMPLETED
                )
                if self.stop_event.is_set():
                    status, error_code = self.stop_reason, "operation_stopped"
                elif call not in done:
                    status, error_code = "timed_out", "provider_timeout"
                    self.stop("timed_out")
                else:
                    status, error_code = "completed", None
        except AuthorizationRevoked:
            # Intent is durable here; the rejected dispatch append did not run.
            # Its not_dispatched outcome remains recordable after Stop.
            pass
        finally:
            if call is not None:
                if not call.done():
                    call.cancel()
                stop.cancel()
                await asyncio.gather(call, stop, return_exceptions=True)
                if status == "completed" and (call.cancelled() or call.cancelling()):
                    # Self-cancellation revokes success even if the transport
                    # suppresses it and returns a final observation to retain.
                    status, error_code = "cancelled", "provider_cancelled"
                    self.stop()
                if not call.cancelled():
                    try:
                        final = call.result()
                    except ProviderBodyFailure as exc:
                        await observe(exc.observation)
                        failure = True
                    except TimeoutError:
                        if status == "completed":
                            status, error_code = "timed_out", "provider_timeout"
                            self.stop("timed_out")
                    except Exception:
                        # Never serialize str(exc), headers or SDK error objects.
                        failure = True
                    else:
                        if final is not None:
                            await observe(final)
                if retained:
                    await asyncio.gather(*retained)
        if self.poisoned:
            self.check()
        if self.stop_event.is_set() and status == "completed":
            status, error_code = self.stop_reason, "operation_stopped"
        if not dispatched:
            status = "not_dispatched"
            error_code = "dispatch_blocked"
        elif status == "completed" and failure:
            status, error_code = "failed", "provider_failure"
        elif status == "completed" and output_count == 0:
            status, error_code = "failed", "provider_no_output"
        outcome = OutcomeData(
            status=status,
            dispatched=dispatched,
            output_count=output_count,
            reported_model=reported_model,
            provider_status=provider_status,
            usage=usage,
            error_code=error_code,
        )
        await self._record(identity, "outcome", outcome.model_dump(mode="json"))
        return AttemptResult(identity, tuple(outputs), outcome)

    async def _backoff(self, seconds: float):
        try:
            await asyncio.wait_for(self.stop_event.wait(), timeout=seconds)
        except TimeoutError:
            pass

    async def validation(self, result: AttemptResult, *, valid: bool):
        self.check()
        if (
            result is not self.previous
            or result.outcome.status != "completed"
            or result.outcome.output_count == 0
            or result.identity.attempt_id in self.validated
        ):
            raise ValueError("Validation must refer to the current unvalidated attempt")
        await self._record(
            result.identity,
            "validation",
            {
                "status": "valid" if valid else "invalid",
                "error_code": None if valid else "invalid_plan",
            },
        )
        self.validated[result.identity.attempt_id] = valid
        self.check()


class RecordedPlanner:
    """Injected request builder/one-send transport/decoder, with coordinator validation.

    This is an audit boundary, not a real planner or automatic retry policy.
    Legacy Planner.plan fixtures remain a separate, logically observed path.
    """

    def __init__(
        self,
        *,
        build_request: Callable[[PlanningInput], CanonicalRequest],
        transport: SingleDispatchTransport,
        decode: Callable[[AttemptResult], Proposal],
    ):
        self.build_request, self.transport, self.decode = build_request, transport, decode

    async def plan_recorded(
        self,
        context: PlanningInput,
        *,
        runner: AttemptRunner,
        identity: LLMIdentity,
        validate: Callable[[Proposal], Proposal],
    ) -> Proposal:
        request = self.build_request(context)
        body = request.body
        reasoning = body.get("reasoning")
        if (
            body["model"] != context.model
            or reasoning is None
            or reasoning.get("effort") != context.reasoning_effort
        ):
            raise AgentFault(
                "planner_request_mismatch",
                "Planner request does not match the frozen model or reasoning selection.",
            )
        result = await runner.run(identity, request, self.transport)
        if result.outcome.status == "timed_out":
            raise AgentFault("planner_timeout", "The planner request timed out; no tools ran.")
        runner.check()
        if result.outcome.status != "completed":
            raise AgentFault("planner_unavailable", "Planner failed; no tools ran.")
        try:
            proposal = validate(self.decode(result))
        except Exception:
            await runner.validation(result, valid=False)
            raise AgentFault(
                "invalid_plan", "Planner returned an invalid structured plan."
            ) from None
        await runner.validation(result, valid=True)
        return proposal
