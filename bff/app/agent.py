"""Backend-owned request/approval/settlement coordinator; no autonomous LLM loop."""

import asyncio
import threading
import time
from collections.abc import AsyncIterator
from dataclasses import asdict, dataclass, field
from functools import partial
from typing import Protocol
from uuid import uuid4

import anyio

from app.agent_files import AgentFault, FilePolicy, canonical_digest
from app.agent_schemas import (
    TERMINAL,
    AgentError,
    AgentPlan,
    AgentRun,
    AgentSession,
    Cancel,
    Claim,
    Command,
    CreateSession,
    PatchSession,
    PlanIdentity,
    PlanStep,
    Proposal,
    RejectClarification,
    StepResult,
)
from app.sessions import SessionStore

INSTRUCTIONS = (
    "agent-read-slice-v1: Propose only bounded read_file steps; no tools during planning."
)


@dataclass(frozen=True)
class PlanningInput:
    request: str
    model: str
    reasoning_effort: str
    workspaces: tuple[str, ...]
    feedback: str | None = None
    instructions: str = INSTRUCTIONS
    attempt: int = 1
    max_attempts: int = 8


class Planner(Protocol):
    async def plan(self, context: PlanningInput) -> Proposal: ...


@dataclass
class Operation:
    id: str
    sid: str
    rid: str
    request_id: str
    after: int
    deadline: float
    context: PlanningInput | None
    gate: asyncio.Event = field(default_factory=asyncio.Event)
    stop: threading.Event = field(default_factory=threading.Event)
    signal: asyncio.Event = field(default_factory=asyncio.Event)
    reason: str = "cancelled"
    task: asyncio.Task | None = None
    timer: asyncio.Task | None = None
    stopping: asyncio.Task | None = None


class AgentService:
    def __init__(
        self,
        store: SessionStore | None = None,
        *,
        planner: Planner | None = None,
        files: FilePolicy | None = None,
        models: tuple[str, ...] = (),
        eager: bool = True,
    ):
        self.store = store or SessionStore()
        self.planner, self.files, self.models = planner, files or FilePolicy(), models
        self.files.bind_storage(self.store.root)
        self.eager = eager
        self.lock = asyncio.Lock()
        self.active: dict[str, Operation] = {}
        self.admissions: dict[asyncio.Task, asyncio.Event] = {}
        self.io_tasks: set[asyncio.Task] = set()
        self.control_tasks: set[asyncio.Task] = set()
        self.closing = False

    async def io(self, fn, *args, **kwargs):
        task = self.track(asyncio.create_task(self._io(fn, *args, **kwargs)), self.io_tasks)
        return await asyncio.shield(task)

    @staticmethod
    def track(task: asyncio.Task, tasks: set[asyncio.Task]) -> asyncio.Task:
        tasks.add(task)

        def settled(done):
            tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settled)
        return task

    async def _io(self, fn, *args, **kwargs):
        try:
            return await anyio.to_thread.run_sync(partial(fn, *args, **kwargs))
        except OSError:
            self.store.poisoned = True
            self.stop_for_recording_failure()
            raise AgentFault("storage_unavailable", "Agent storage is unavailable.", 503) from None
        except AgentFault as exc:
            if exc.code == "storage_unavailable":
                self.stop_for_recording_failure()
            raise

    def stop_for_recording_failure(self):
        for op in self.active.values():
            self.signal_stop(op, "interrupted")

    async def start(self):
        if self.eager:
            async with self.lock:
                await self.ready()

    def open_store(self) -> tuple[int, int]:
        self.store.start()
        return self.store.identity()

    async def ready(self):
        if self.closing:
            raise AgentFault("service_closing", "Agent service is shutting down.", 503)
        identity = await self.io(self.open_store)
        if self.closing:
            raise AgentFault("service_closing", "Agent service is shutting down.", 503)
        self.files.bind_storage(self.store.root, identity=identity)

    def configured(self):
        if self.planner is None:
            raise AgentFault(
                "agent_not_configured",
                "Agent planning is unavailable. Configure a planner and allowed workspaces.",
                503,
            )

    def model_allowed(self, model: str):
        if model not in self.models:
            raise AgentFault("invalid_model", "Model must be included in AVAILABLE_MODELS.", 422)

    async def create(self, body: CreateSession):
        self.model_allowed(body.model)
        async with self.lock:
            await self.ready()
            return await self.io(self.store.create, body)

    async def get(self, sid: str):
        async with self.lock:
            await self.ready()
            return await self.io(self.store.get, sid)

    async def listing(self, include_archived: bool):
        async with self.lock:
            await self.ready()
            return await self.io(self.store.listing, include_archived)

    async def history(self, sid: str, after: int):
        async with self.lock:
            await self.ready()
            events = await self.io(self.store.events, sid, after)
            snapshot = await self.io(self.store.get, sid)
            return {"events": events, "has_more": after + len(events) < snapshot.sequence}

    async def check_claim(self, sid: str, action: str, body: Command) -> tuple[Claim, bool]:
        digest = canonical_digest({"action": action, "body": body.model_dump()})
        fresh = await self.io(self.store.claim, sid, body.request_id, digest)
        return Claim(request_id=body.request_id, digest=digest), fresh

    async def commit(self, snapshot, kind, request_id, **kwargs):
        return await self.io(self.store.append, snapshot, kind, request_id, **kwargs)

    @staticmethod
    def waiting(snapshot: AgentSession):
        return any(run.status not in TERMINAL for run in snapshot.runs)

    @staticmethod
    def run(snapshot: AgentSession, rid: str) -> AgentRun:
        for run in snapshot.runs:
            if run.id == rid:
                return run
        raise AgentFault("run_not_found", "Agent run not found.", 404)

    @staticmethod
    def identity(run: AgentRun, body: PlanIdentity):
        plan = run.plan
        if (
            run.status != "awaiting_approval"
            or plan is None
            or (plan.id, plan.revision, plan.digest) != (body.plan_id, body.revision, body.digest)
        ):
            raise AgentFault(
                "stale_plan", "Only the exact current awaiting-approval plan can be used."
            )

    async def patch(self, sid: str, body: PatchSession):
        async with self.lock:
            await self.ready()
            snapshot = await self.io(self.store.get, sid)
            claim, fresh = await self.check_claim(sid, "patch", body)
            if not fresh:
                return snapshot
            if snapshot.archived or snapshot.archive_pending:
                raise AgentFault("session_archived", "Archived sessions are read-only.")
            if (body.model is not None or body.reasoning_effort is not None) and self.waiting(
                snapshot
            ):
                raise AgentFault(
                    "preferences_locked", "Reject the waiting run or wait for settlement first."
                )
            for name in ("title", "model", "reasoning_effort"):
                value = getattr(body, name)
                if value is not None:
                    if name == "model":
                        self.model_allowed(value)
                    setattr(snapshot, name, value)
            return (
                await self.commit(snapshot, "session.updated", body.request_id, claim=claim)
            ).session

    async def prepare(self, sid: str, body, *, rid: str | None = None, action="plan") -> Operation:
        if self.closing:
            raise AgentFault("service_closing", "Agent service is shutting down.", 503)
        if len(self.admissions) >= 4:
            raise AgentFault("capacity_exceeded", "Agent admission capacity is occupied.", 429)
        abandoned = asyncio.Event()

        async def admitted():
            op = await self._admit(sid, body, rid=rid, action=action)
            if abandoned.is_set() or self.closing:
                self.request_stop(op, "interrupted")
            return op

        # Native Task.cancel() is independent of AnyIO cancel scopes. This owned
        # task retains commit + registration, even when its HTTP waiter vanishes.
        task = asyncio.create_task(admitted())
        self.admissions[task] = abandoned

        def settled(done):
            self.admissions.pop(done, None)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(settled)
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            abandoned.set()
            if task.done() and not task.cancelled() and task.exception() is None:
                self.request_stop(task.result(), "interrupted")
            raise

    async def _admit(self, sid: str, body, *, rid: str | None, action: str) -> Operation:
        with anyio.CancelScope(shield=True):
            async with self.lock:
                self.configured()
                await self.ready()
                snapshot = await self.io(self.store.get, sid)
                claim, fresh = await self.check_claim(sid, f"{action}:{rid}", body)
                if not fresh:
                    raise AgentFault(
                        "duplicate_command", "Command was already accepted; fetch the session."
                    )
                if snapshot.archived or snapshot.archive_pending:
                    raise AgentFault("session_archived", "Archived sessions are read-only.")
                if len(self.active) >= 4:
                    raise AgentFault(
                        "capacity_exceeded", "Agent operation capacity is occupied.", 429
                    )
                if any(op.sid == sid for op in self.active.values()):
                    raise AgentFault(
                        "operation_active", "Wait for the current operation to settle."
                    )
                context = None
                if action == "plan":
                    if self.waiting(snapshot) or len(snapshot.runs) >= 8:
                        raise AgentFault(
                            "run_conflict", "The session is waiting or has reached its run limit."
                        )
                    now = self.store.clock()
                    run = AgentRun(
                        id=uuid4().hex,
                        request=body.content,
                        created_at=now,
                        updated_at=now,
                        status="planning",
                    )
                    snapshot.runs.append(run)
                else:
                    run = self.run(snapshot, rid)
                    self.identity(run, body)
                if action == "execute":
                    plan = run.plan
                    material = plan.model_dump(exclude={"digest"})
                    if (
                        plan.access_policy != self.files.projection()
                        or plan.model != snapshot.model
                        or plan.reasoning_effort != snapshot.reasoning_effort
                        or plan.digest
                        != canonical_digest({"session": sid, "run": run.id, "plan": material})
                    ):
                        raise AgentFault(
                            "policy_changed", "The effective policy changed; create a new run."
                        )
                    run.status = "approved"
                else:
                    attempts = sum(
                        record.event.type == "planner.attempt" and record.event.run_id == run.id
                        for record in self.store.records[sid]
                    )
                    if attempts >= 8:
                        raise AgentFault(
                            "attempt_limit", "Planning attempt allowance is exhausted."
                        )
                    context = PlanningInput(
                        request=run.request,
                        model=snapshot.model,
                        reasoning_effort=snapshot.reasoning_effort,
                        workspaces=tuple(sorted(self.files.roots)),
                        feedback=body.content if action == "revise" else None,
                        attempt=attempts + 1,
                    )
                    run.status = "planning"
                op = Operation(
                    id=uuid4().hex,
                    sid=sid,
                    rid=run.id,
                    request_id=body.request_id,
                    after=snapshot.sequence,
                    deadline=time.monotonic()
                    + (run.plan.limits.timeout_seconds if action == "execute" else 30),
                    context=context,
                )
                run.operation_id = op.id
                await self.io(self.store.reserve, snapshot, op.id, executing=action == "execute")
                try:
                    await self.commit(
                        snapshot,
                        "run.approved" if action == "execute" else "run.planning",
                        body.request_id,
                        run_id=run.id,
                        operation_id=op.id,
                        claim=claim,
                        reservation=op.id,
                        audit={"feedback": context.feedback}
                        if context and context.feedback
                        else {},
                    )
                except BaseException:
                    await self.io(self.store.release, op.id)
                    raise
                self.active[op.id] = op
                op.task = asyncio.create_task(self.drive(op))
                op.timer = asyncio.create_task(self.deadline(op))
                return op

    async def transition(self, op: Operation, kind: str, mutate=None, audit=None):
        async with self.lock:
            if op.stop.is_set() and kind not in {"run.cancelled", "run.interrupted", "run.failed"}:
                raise AgentFault("operation_stopped", "Operation stopped.")
            snapshot = await self.io(self.store.get, op.sid)
            run = self.run(snapshot, op.rid)
            if mutate:
                mutate(snapshot, run)
            return await self.commit(
                snapshot,
                kind,
                op.request_id,
                run_id=op.rid,
                operation_id=op.id,
                reservation=op.id,
                audit=audit,
            )

    def check_stop(self, op: Operation):
        if op.stop.is_set():
            if op.reason == "failed":
                raise AgentFault("timeout", "Agent operation exceeded its deadline.")
            raise AgentFault("operation_stopped", "Operation stopped.")
        if time.monotonic() >= op.deadline:
            op.stop.set()
            op.reason = "failed"
            raise AgentFault("timeout", "Agent operation exceeded its deadline.")

    async def planning(self, op: Operation):
        self.check_stop(op)
        await self.transition(
            op,
            "planner.attempt",
            audit={
                "input": asdict(op.context),
                "usage": "unknown",
                "retries": 0,
            },
        )
        self.check_stop(op)
        call = asyncio.create_task(self.planner.plan(op.context))
        stop = asyncio.create_task(op.signal.wait())
        try:
            done, _ = await asyncio.wait(
                {call, stop},
                timeout=max(0, op.deadline - time.monotonic()),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if call not in done or op.stop.is_set():
                call.cancel()
                await asyncio.gather(call, return_exceptions=True)
                self.check_stop(op)
                raise AgentFault("timeout", "Agent planning exceeded its deadline.")
            try:
                proposal = call.result()
            except Exception:
                raise AgentFault(
                    "planner_unavailable", "Planner failed to return a valid plan; no tools ran."
                ) from None
            if not isinstance(proposal, Proposal):
                raise AgentFault("invalid_plan", "Planner returned an invalid structured plan.")
            proposal = Proposal.model_validate(proposal.model_dump())
            for step in proposal.steps:
                self.files.validate(step.arguments)
            snapshot = await self.get(op.sid)
            prior = self.run(snapshot, op.rid).plan
            material = {
                **proposal.model_dump(),
                "id": prior.id if prior else uuid4().hex,
                "revision": prior.revision + 1 if prior else 1,
                "model": snapshot.model,
                "reasoning_effort": snapshot.reasoning_effort,
                "assumptions": [
                    "Workspace assumptions are unverified; planning performs no reads.",
                    *proposal.assumptions,
                ],
                "access_policy": self.files.projection(),
                "steps": [
                    PlanStep(**step.model_dump(), id=uuid4().hex).model_dump()
                    for step in proposal.steps
                ],
            }
            plan = AgentPlan(
                **material,
                digest=canonical_digest(
                    {
                        "session": op.sid,
                        "run": op.rid,
                        "plan": material,
                    }
                ),
            )
            self.check_stop(op)
            await self.transition(
                op,
                "planner.result",
                audit={"proposal": proposal.model_dump(), "status": "completed"},
            )
            self.check_stop(op)

            def reviewed(snapshot, run):
                run.plan, run.status, run.operation_id = plan, "awaiting_approval", None

            await self.transition(op, "run.awaiting_approval", reviewed)
        finally:
            stop.cancel()
            await asyncio.gather(stop, return_exceptions=True)

    async def execution(self, op: Operation):
        self.check_stop(op)
        await self.transition(op, "run.executing", lambda s, r: setattr(r, "status", "executing"))
        plan = self.run(await self.get(op.sid), op.rid).plan
        for step in plan.steps:
            self.check_stop(op)
            # Exact frozen arguments, one bounded call per step, no model/tool re-interpretation.
            self.files.validate(step.arguments)

            def running(snapshot, run, step=step):
                run.results.append(
                    StepResult(
                        step_id=step.id,
                        status="running",
                        content="",
                        time=self.store.clock(),
                    )
                )

            await self.transition(
                op,
                "tool.intent",
                running,
                audit={
                    "step_id": step.id,
                    "tool": step.tool,
                    "arguments": step.arguments.model_dump(),
                    "decision": "allowed_exact_approved_arguments",
                },
            )
            self.check_stop(op)
            # This await is never cancelled on Stop/disconnect. Ownership lasts until
            # the actual thread returns, not merely until its waiter is cancelled.
            result = await self.io(self.files.read, step.arguments, op.stop, op.deadline)
            self.check_stop(op)

            def completed(snapshot, run, step=step, result=result):
                run.results[-1] = StepResult(
                    step_id=step.id,
                    status="completed",
                    time=self.store.clock(),
                    **result,
                )

            await self.transition(
                op, "tool.result", completed, audit={"step_id": step.id, "status": "completed"}
            )
        self.check_stop(op)

        def finished(snapshot, run):
            run.status, run.operation_id = "completed", None
            run.answer = (
                f"Completed {len(run.results)} approved file read(s). See the recorded excerpts."
            )

        await self.transition(op, "run.completed", finished)

    async def drive(self, op: Operation):
        try:
            await op.gate.wait()
            self.check_stop(op)
            await (self.planning(op) if op.context else self.execution(op))
        except Exception as exc:
            fault = (
                exc
                if isinstance(exc, AgentFault)
                else AgentFault(
                    "agent_operation_failed",
                    "Agent operation failed; no additional work was dispatched.",
                )
            )
            if op.stop.is_set() and op.reason == "failed":
                fault = AgentFault("timeout", "Agent operation exceeded its deadline.")
            status = op.reason if op.stop.is_set() else "failed"

            def failed(snapshot, run):
                run.status, run.operation_id = status, None
                run.error = AgentError(code=fault.code, message=fault.message)
                for result in run.results:
                    if result.status == "running":
                        result.status, result.error = "failed", run.error
                if snapshot.archive_pending:
                    snapshot.archived, snapshot.archive_pending = True, False

            try:
                await self.transition(
                    op, "run." + status, failed, audit={"status": status, "error": fault.code}
                )
            except AgentFault:
                # No uncommitted outcome is published. Durable unmatched intent is
                # recovered as interrupted/unknown on the next successful startup.
                pass
        finally:
            async with self.lock:
                try:
                    snapshot = await self.io(self.store.get, op.sid)
                    if snapshot.archive_pending:
                        snapshot.archived, snapshot.archive_pending = True, False
                        await self.commit(
                            snapshot, "session.archived", op.request_id, reservation=op.id
                        )
                except AgentFault:
                    pass
                self.active.pop(op.id, None)
                if op.timer is not None:
                    op.timer.cancel()
                await self.io(self.store.release, op.id)

    def signal_stop(self, op: Operation, reason: str):
        # No service/store lock or filesystem operation may delay the worker's
        # cooperative stop signal. Durable acknowledgement remains serialized.
        if op.stop.is_set():
            return
        op.reason = reason
        op.stop.set()
        op.signal.set()
        op.gate.set()

    def request_stop(self, op: Operation, reason: str):
        if op.id not in self.active:
            return None
        self.signal_stop(op, reason)
        if op.stopping is None:
            op.stopping = self.track(asyncio.create_task(self.record_stop(op)), self.control_tasks)
        return op.stopping

    async def record_stop(self, op: Operation):
        async with self.lock:
            if op.id in self.active:
                try:
                    await self.stop_locked(op, op.reason)
                except AgentFault:
                    pass

    async def deadline(self, op: Operation):
        await asyncio.sleep(max(0, op.deadline - time.monotonic()))
        self.request_stop(op, "failed")

    async def stop_locked(self, op: Operation, reason: str):
        self.signal_stop(op, reason)
        snapshot = await self.io(self.store.get, op.sid)
        run = self.run(snapshot, op.rid)
        if run.operation_id == op.id and run.status != "cancelling":
            run.status = "cancelling"
            await self.commit(
                snapshot,
                "run.cancelling",
                op.request_id,
                run_id=op.rid,
                operation_id=op.id,
                reservation=op.id,
            )

    async def disconnect(self, op: Operation):
        reason = "failed" if time.monotonic() >= op.deadline else "interrupted"
        task = self.request_stop(op, reason)
        if task is not None:
            # Wait briefly for the ordinary fast commit. If storage/locks stall,
            # the retained control task owns that commit beyond HTTP cleanup.
            await asyncio.wait({task}, timeout=0.1)

    async def cancel(self, sid: str, rid: str, body: Cancel):
        candidate = self.active.get(body.operation_id)
        if candidate is not None and candidate.sid == sid and candidate.rid == rid:
            # Revocation intent must reach the worker even if an unrelated
            # durable mutation is blocking the command lock. Published claims
            # are immutable; reject a known reused key before signaling Stop.
            digest = canonical_digest({"action": f"cancel:{rid}", "body": body.model_dump()})
            for record in self.store.records[sid]:
                if record.claim is not None and record.claim.request_id == body.request_id:
                    if record.claim.digest != digest:
                        raise AgentFault("request_conflict", "Request ID has different content.")
            self.request_stop(candidate, "cancelled")
        async with self.lock:
            await self.ready()
            snapshot = await self.io(self.store.get, sid)
            self.run(snapshot, rid)
            claim, fresh = await self.check_claim(sid, f"cancel:{rid}", body)
            known = any(
                r.event.operation_id == body.operation_id and r.event.run_id == rid
                for r in self.store.records[sid]
            )
            if not known:
                raise AgentFault(
                    "operation_not_found", "Operation does not belong to this run.", 404
                )
            op = self.active.get(body.operation_id)
            pending = op is not None and self.run(snapshot, rid).operation_id == body.operation_id
            if pending:
                await self.stop_locked(op, "cancelled")
                snapshot = await self.io(self.store.get, sid)
            if fresh:
                snapshot = (
                    await self.commit(snapshot, "session.updated", body.request_id, claim=claim)
                ).session
            return snapshot, pending

    async def reject(self, sid: str, rid: str, body):
        if isinstance(body, RejectClarification):
            raise AgentFault(
                "clarification_unavailable", "Clarification is unavailable in this slice.", 501
            )
        async with self.lock:
            await self.ready()
            snapshot = await self.io(self.store.get, sid)
            run = self.run(snapshot, rid)
            claim, fresh = await self.check_claim(sid, f"reject:{rid}", body)
            if not fresh:
                return snapshot
            self.identity(run, body)
            run.status = "rejected"
            return (
                await self.commit(
                    snapshot, "run.rejected", body.request_id, run_id=rid, claim=claim
                )
            ).session

    async def archive(self, sid: str, body: Command):
        async with self.lock:
            await self.ready()
            snapshot = await self.io(self.store.get, sid)
            claim, fresh = await self.check_claim(sid, "archive", body)
            op = next((op for op in self.active.values() if op.sid == sid), None)
            if op:
                await self.stop_locked(op, "cancelled")
                snapshot = await self.io(self.store.get, sid)
            if fresh:
                snapshot.archive_pending = op is not None
                snapshot.archived = op is None
                for run in snapshot.runs:
                    if run.status in {"awaiting_approval", "awaiting_clarification"}:
                        run.status = "rejected"
                snapshot = (
                    await self.commit(
                        snapshot,
                        "session.archive_pending" if op else "session.archived",
                        body.request_id,
                        claim=claim,
                    )
                ).session
            return snapshot, snapshot.archive_pending

    async def frames(self, op: Operation) -> AsyncIterator[str]:
        op.gate.set()
        after = op.after
        try:
            while True:
                events = await self.io(self.store.events, op.sid, after)
                for event in events:
                    after = event.sequence
                    yield f"id: {event.sequence}\nevent: agent\ndata: {event.model_dump_json()}\n\n"
                    if event.operation_id == op.id and event.type in {
                        "run.awaiting_approval",
                        "run.completed",
                        "run.failed",
                        "run.cancelled",
                        "run.interrupted",
                    }:
                        return
                if op.task.done():
                    if not events:
                        break
                else:
                    await asyncio.sleep(0.01)
        except AgentFault:
            return
        finally:
            with anyio.CancelScope(shield=True):
                await self.disconnect(op)

    async def close(self):
        self.closing = True
        for abandoned in self.admissions.values():
            abandoned.set()
        await asyncio.gather(*list(self.admissions), return_exceptions=True)
        operations = list(self.active.values())
        for op in operations:
            self.request_stop(op, "interrupted")
        await asyncio.gather(*(op.task for op in operations), return_exceptions=True)
        # Control tasks need the command lock, so settle them before taking the
        # final barrier. No new admission can appear after closing was set.
        while self.control_tasks:
            await asyncio.gather(*list(self.control_tasks), return_exceptions=True)
        async with self.lock:
            # A command may have passed ready() before shutdown and not yet
            # queued its write when the earlier I/O set appeared empty. The lock
            # closes that scheduling gap; drain native-cancelled command workers
            # under it too, before invalidating the store descriptor.
            while self.io_tasks:
                await asyncio.gather(*list(self.io_tasks), return_exceptions=True)
            await self.io(self.store.close)
