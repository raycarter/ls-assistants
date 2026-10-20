"""Private v2 journal codec: fixed projection changes, never public wire models.

New bodies survive in the current projection only as refs, alongside small controls.
Unchanged legacy bodies remain inline in that one projection until replaced.
Absent change fields mean unchanged; explicit null means clear. Ordered run/result
identities are independent of the event's optional run_id (archive/recovery change
multiple runs). Legacy records are read in place, without rewriting their bytes.
"""

import hashlib
import json
import secrets
from collections.abc import Callable
from typing import Literal

from pydantic import Field

from app.agent_schemas import (
    AgentError,
    AgentEvent,
    AgentPlan,
    AgentSession,
    Claim,
    Effort,
    JournalRecord,
    Key,
    Status,
    StepResult,
    Strict,
)


class PayloadRef(Strict):
    id: str = Field(pattern=r"^[a-f0-9]{32}$")
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    bytes: int = Field(ge=0)


EventKind = AgentEvent.model_fields["type"].annotation


class EventHeader(Strict):
    schema_version: Literal[1] = 1
    sequence: int
    time: str
    session_id: str
    request_id: str
    run_id: str | None
    operation_id: str | None
    type: EventKind


class ResultRef(Strict):
    step_id: Key
    payload: PayloadRef


class ResultState(Strict):
    step_id: Key
    payload: PayloadRef | StepResult


class RunState(Strict):
    id: Key
    request: PayloadRef | str
    created_at: str
    updated_at: str
    status: Status
    operation_id: Key | None
    plan: PayloadRef | AgentPlan | None
    results: list[ResultState]
    answer: PayloadRef | str | None
    error: AgentError | None
    clarification: None


class SessionState(Strict):
    id: str
    title: str
    model: str
    reasoning_effort: Effort
    created_at: str
    updated_at: str
    archived: bool
    archive_pending: bool
    sequence: int
    runs: list[RunState]


class SessionChanges(Strict):
    id: str | None = None
    title: str | None = None
    model: str | None = None
    reasoning_effort: Effort | None = None
    created_at: str | None = None
    updated_at: str | None = None
    archived: bool | None = None
    archive_pending: bool | None = None
    sequence: int | None = None


class ResultChanges(Strict):
    order: list[Key] | None = None
    changed: list[ResultRef] = Field(default_factory=list)


class RunChanges(Strict):
    id: Key
    request: PayloadRef | None = None
    created_at: str | None = None
    updated_at: str | None = None
    status: Status | None = None
    operation_id: Key | None = None
    plan: PayloadRef | None = None
    results: ResultChanges | None = None
    answer: PayloadRef | None = None
    error: AgentError | None = None
    clarification: None = None


class StorageEntry(Strict):
    storage_version: Literal[2] = 2
    event: EventHeader
    session: SessionChanges
    run_order: list[Key] | None = None
    runs: list[RunChanges] = Field(default_factory=list)
    claim: Claim | None = None
    audit: PayloadRef | None = None


def json_bytes(value: object) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def header(record: JournalRecord) -> EventHeader:
    return EventHeader.model_validate(record.event.model_dump(exclude={"session"}))


def encode(
    record: JournalRecord, previous: SessionState | None, baseline: AgentSession | None = None
) -> tuple[bytes, SessionState, list[tuple[PayloadRef, bytes]]]:
    """Compare against the one private latest baseline *before* serializing bodies."""
    pending = []

    def payload(value):
        raw = json_bytes(value.model_dump() if isinstance(value, Strict) else value)
        digest = hashlib.sha256(raw).hexdigest()
        ref = PayloadRef(id=secrets.token_hex(16), sha256=digest, bytes=len(raw))
        pending.append((ref, raw))
        return ref

    old_runs = {run.id: run for run in previous.runs} if previous else {}
    baseline_runs = {run.id: run for run in baseline.runs} if baseline else {}
    if previous is not None and baseline is None:
        raise ValueError("missing latest comparison baseline")
    states, changes = [], []
    for run in record.event.session.runs:
        old = old_runs.get(run.id)
        prior = baseline_runs.get(run.id)
        if old is not None and run == prior:
            states.append(old)
            continue
        old_results = {r.step_id: r for r in old.results} if old else {}
        prior_results = {r.step_id: r for r in prior.results} if prior else {}
        values = run.model_dump(exclude={"request", "plan", "results", "answer"})
        for name in ("request", "plan", "answer"):
            value = getattr(run, name)
            values[name] = (
                getattr(old, name)
                if old is not None and prior is not None and value == getattr(prior, name)
                else payload(value)
                if value is not None
                else None
            )
        results = [
            ResultState(
                step_id=r.step_id,
                payload=old_results[r.step_id].payload
                if r.step_id in old_results and r == prior_results.get(r.step_id)
                else payload(r),
            )
            for r in run.results
        ]
        if len({r.step_id for r in results}) != len(results):
            raise ValueError("duplicate result")
        state = RunState(**values, results=results)
        delta = {
            name: getattr(state, name)
            for name in RunState.model_fields
            if name not in {"id", "results"}
            and (old is None or getattr(old, name) != getattr(state, name))
        }
        if old is None or old.results != results:
            order = [r.step_id for r in results]
            result_delta = ResultChanges(
                changed=[
                    ResultRef.model_validate(r.model_dump())
                    for r in results
                    if old_results.get(r.step_id) != r
                ]
            )
            if old is None or [r.step_id for r in old.results] != order:
                result_delta.order = order
            delta["results"] = result_delta
        if delta or old is None:
            changes.append(RunChanges(id=run.id, **delta))
        states.append(state)
    if len({r.id for r in states}) != len(states):
        raise ValueError("duplicate run")
    session = record.event.session.model_dump(exclude={"runs"})
    state = SessionState(**session, runs=states)
    entry = StorageEntry(
        storage_version=2,
        event=header(record),
        session=SessionChanges(
            **{
                name: value
                for name, value in session.items()
                if previous is None or getattr(previous, name) != value
            }
        ),
        runs=changes,
        claim=record.claim,
        audit=payload(record.audit) if record.audit else None,
    )
    order = [r.id for r in states]
    if previous is None or [r.id for r in previous.runs] != order:
        entry.run_order = order
    return json_bytes(entry.model_dump(exclude_unset=True)) + b"\n", state, pending


def _ordered(current: dict, order: list | None) -> list:
    if order is None:
        return list(current.values())
    if len(set(order)) != len(order) or any(key not in current for key in order):
        raise ValueError("invalid change order")
    return [current[key] for key in order]


def apply(entry: StorageEntry, previous: SessionState | None) -> SessionState:
    values = previous.model_dump() if previous else {"runs": []}
    values.update(entry.session.model_dump(exclude_unset=True))
    runs = {r["id"]: r for r in values["runs"]}
    changed = set()
    for change in entry.runs:
        if change.id in changed:
            raise ValueError("duplicate run change")
        changed.add(change.id)
        run = runs.get(change.id, {"id": change.id, "results": []})
        run.update(change.model_dump(exclude_unset=True, exclude={"results"}))
        if "results" in change.model_fields_set:
            if change.results is None:
                raise ValueError("null results")
            results = {r["step_id"]: r for r in run["results"]}
            seen = set()
            for result in change.results.changed:
                if result.step_id in seen:
                    raise ValueError("duplicate result change")
                seen.add(result.step_id)
                results[result.step_id] = result.model_dump()
            if change.results.order is not None and not seen.issubset(change.results.order):
                raise ValueError("unreachable result change")
            run["results"] = _ordered(results, change.results.order)
        runs[change.id] = run
    if entry.run_order is not None and not changed.issubset(entry.run_order):
        raise ValueError("unreachable run change")
    values["runs"] = _ordered(runs, entry.run_order)
    return SessionState.model_validate(values)


def materialize(state: SessionState, read: Callable[[PayloadRef], object]) -> AgentSession:
    def body(value):
        if isinstance(value, PayloadRef):
            return read(value)
        return value.model_dump() if isinstance(value, Strict) else value

    values = state.model_dump(exclude={"runs"})
    runs = []
    for run in state.runs:
        value = run.model_dump(exclude={"request", "plan", "results", "answer"})
        value["request"] = body(run.request)
        value["plan"] = body(run.plan)
        value["answer"] = body(run.answer)
        value["results"] = [body(r.payload) for r in run.results]
        if [r["step_id"] for r in value["results"]] != [r.step_id for r in run.results]:
            raise ValueError("mismatched result identity")
        runs.append(value)
    return AgentSession.model_validate({**values, "runs": runs})


def decode(
    raw: bytes,
    previous: SessionState | None,
    read: Callable[[PayloadRef], object],
) -> tuple[JournalRecord, SessionState]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("invalid journal object")
    if "storage_version" not in value:
        record = JournalRecord.model_validate(value)
        # Only the current legacy projection retains inline bodies. Unchanged
        # legacy fields stay inline until changed, even across mixed v2 records.
        return record, legacy_state(record.event.session)
    if type(value["storage_version"]) is not int or value["storage_version"] != 2:
        raise ValueError("unknown storage version")
    entry = StorageEntry.model_validate(value)
    state = apply(entry, previous)
    audit = read(entry.audit) if entry.audit else {}
    if not isinstance(audit, dict):
        raise ValueError("invalid audit payload")
    record = JournalRecord(
        event=AgentEvent(**entry.event.model_dump(), session=materialize(state, read)),
        claim=entry.claim,
        audit=audit,
    )
    return record, state


def legacy_state(snapshot: AgentSession) -> SessionState:
    """Legacy projection bodies are held only for its latest snapshot, by the store."""
    return SessionState(
        **snapshot.model_dump(exclude={"runs"}),
        runs=[
            RunState(
                **run.model_dump(exclude={"results"}),
                results=[
                    ResultState(step_id=r.step_id, payload=r.model_copy(deep=True))
                    for r in run.results
                ],
            )
            for run in snapshot.runs
        ],
    )
