"""Private LLM lifecycle index and bounded, committed-reference-only readers.

Only llm.* envelopes opt in to the versioned provider contract; historical planner
observations never become provider attempts. Optional coordinator allocations
constrain later typed intents. The index retains
identities, controls and exact locators, never requests, output bodies or usage.
Each observation has its own immutable payload; a large request is one payload.
The existing codec serializes each full JSON record in memory, not constant space.
"""

import json
import os
from bisect import bisect_right
from collections.abc import Mapping
from dataclasses import asdict, dataclass, replace
from types import MappingProxyType
from typing import Annotated, Literal

from pydantic import Field

from app.agent_files import AgentFault
from app.agent_schemas import AgentSession, JournalRecord
from app.llm_schemas import Key, Kind, LLMIdentity, LLMRecord, OutcomeStatus, Phase, Strict
from app.session_journal import PayloadRef


@dataclass(frozen=True)
class AuditLocation:
    sequence: int
    kind: Kind
    at: str
    # A tuple rather than a mutable Pydantic PayloadRef in the published index.
    payload: tuple[str, str, int] | None


@dataclass(frozen=True)
class Outcome:
    status: OutcomeStatus
    dispatched: bool | None
    reported_model: str | None
    provider_status: str | None
    error_code: str | None


@dataclass(frozen=True)
class Validation:
    status: Literal["valid", "invalid"]
    error_code: str | None


@dataclass(frozen=True)
class Attempt:
    identity: LLMIdentity
    ordinal: int
    records: tuple[AuditLocation, ...]
    dispatch_sequence: int | None = None
    output_count: int = 0
    failed_observation: bool = False
    outcome: Outcome | None = None
    validation: Validation | None = None
    # At most three exact output locators, never response bodies or usage values.
    metadata: tuple[tuple[str, AuditLocation], ...] = ()


class Allocation(Strict):
    """Optional preallocated coordinator controls, not a provider audit record."""

    plan_id: Key
    revision: Annotated[int, Field(ge=1)]
    # Approved operations bind the candidate, with individual execution/synthesis
    # stages authorized against the frozen plan rather than an operation-wide ID.
    phase: Phase | None = None
    stage_id: Key | None = None


@dataclass(frozen=True)
class LLMIndex:
    attempts: Mapping[str, Attempt]
    runs: Mapping[str, tuple[str, ...]]
    interactions: Mapping[str, str]
    candidates: Mapping[tuple[str, str], tuple[str, int]]
    stages: Mapping[str, tuple]
    slots: Mapping[tuple, str]
    pending: frozenset[str]
    unvalidated: Mapping[tuple[str, str], str]
    allocations: Mapping[tuple[str, str], Allocation | None]


def empty_index() -> LLMIndex:
    return LLMIndex(
        attempts=MappingProxyType({}),
        runs=MappingProxyType({}),
        interactions=MappingProxyType({}),
        candidates=MappingProxyType({}),
        stages=MappingProxyType({}),
        slots=MappingProxyType({}),
        pending=frozenset(),
        unvalidated=MappingProxyType({}),
        allocations=MappingProxyType({}),
    )


def _allocation(record: JournalRecord, previous: LLMIndex) -> LLMIndex:
    event = record.event
    if event.type not in {"run.planning", "run.approved"} or (
        event.run_id is None or event.operation_id is None
    ):
        return previous
    fields = Allocation.model_fields if event.type == "run.planning" else ("plan_id", "revision")
    controls = {key: record.audit[key] for key in fields if key in record.audit}
    if not controls:
        return previous
    # Unversioned historical audits remain opaque and cannot fail replay merely
    # for resembling modern coordinator controls. Check these optional bindings
    # only if an actual versioned llm.intent later opts this operation in.
    try:
        allocation = Allocation.model_validate(controls)
        if event.type == "run.planning" and (
            allocation.phase is None or allocation.stage_id is None
        ):
            allocation = None
    except ValueError:
        allocation = None
    operation = (event.run_id, event.operation_id)
    if operation in previous.allocations and previous.allocations[operation] != allocation:
        allocation = None
    return replace(
        previous, allocations=MappingProxyType({**previous.allocations, operation: allocation})
    )


def _audit_only(record: JournalRecord, baseline: AgentSession):
    """An observation may update recording timestamps, never coordinator state."""
    event, snapshot = record.event, record.event.session
    prior_runs = {run.id: run for run in baseline.runs}
    normalized = snapshot.model_copy(
        update={
            "sequence": baseline.sequence,
            "updated_at": baseline.updated_at,
            "runs": [
                run.model_copy(update={"updated_at": prior_runs[run.id].updated_at})
                if run.id == event.run_id and run.id in prior_runs
                else run
                for run in snapshot.runs
            ],
        }
    )
    if record.claim is not None or normalized != baseline:
        raise ValueError("LLM audit changed coordinator state")


def _authorized(record: JournalRecord, identity: LLMIdentity):
    snapshot = record.event.session
    run = next(r for r in snapshot.runs if r.id == identity.run_id)
    if (
        snapshot.archived
        or snapshot.archive_pending
        or run.operation_id != identity.operation_id
        or run.status not in {"planning", "approved", "executing"}
    ):
        raise ValueError("LLM dispatch is not authorized by an active operation")
    plan = run.plan
    if identity.phase == "planning":
        valid = run.status == "planning" and plan is None and identity.revision == 1
    elif identity.phase == "revision":
        valid = (
            run.status == "planning"
            and plan is not None
            and (identity.plan_id, identity.revision) == (plan.id, plan.revision + 1)
        )
    else:
        valid = (
            run.status in {"approved", "executing"}
            and plan is not None
            and (identity.plan_id, identity.revision) == (plan.id, plan.revision)
            and (identity.step_id is None or identity.step_id in {s.id for s in plan.steps})
        )
    if not valid:
        raise ValueError("LLM candidate does not match its operation")


def index_record(
    record: JournalRecord,
    previous: LLMIndex,
    baseline: AgentSession | None,
    operations: frozenset[tuple[str, str]],
    audit_ref: PayloadRef | None,
) -> LLMIndex:
    """One transition using compact committed controls; no historical replay/I/O."""
    event = record.event
    if not event.type.startswith("llm."):
        return _allocation(record, previous)
    audit = LLMRecord.model_validate(record.audit)
    identity, kind, data = audit.identity, audit.kind, audit.data
    operation = (identity.run_id, identity.operation_id)
    if (
        event.type != "llm." + kind
        or identity.session_id != event.session_id
        or identity.run_id != event.run_id
        or identity.operation_id != event.operation_id
        or operation not in operations
        or baseline is None
        or not any(run.id == identity.run_id for run in baseline.runs)
    ):
        raise ValueError("Unbound LLM audit")
    _audit_only(record, baseline)
    location = AuditLocation(
        event.sequence,
        kind,
        audit.at,
        (audit_ref.id, audit_ref.sha256, audit_ref.bytes) if audit_ref else None,
    )
    old = previous.attempts.get(identity.attempt_id)
    runs, interactions = previous.runs, previous.interactions
    candidates, stages, slots = previous.candidates, previous.stages, previous.slots
    pending, unvalidated = previous.pending, previous.unvalidated
    if kind == "intent":
        _authorized(record, identity)
        if operation in previous.allocations:
            allocated = previous.allocations[operation]
            fields = (
                Allocation.model_fields
                if allocated is not None and allocated.phase is not None
                else ("plan_id", "revision")
            )
            if allocated is None or any(
                getattr(identity, key) != getattr(allocated, key) for key in fields
            ):
                raise ValueError("LLM identity does not match its preallocated operation")
        if old is not None:
            raise ValueError("Duplicate LLM attempt")
        last_id = interactions.get(identity.interaction_id)
        if last_id is None:
            if identity.attempt != 1:
                raise ValueError("Retry has no committed predecessor")
        else:
            last = previous.attempts[last_id]
            stable = set(LLMIdentity.model_fields) - {
                "attempt_id",
                "attempt",
                "retry_of",
                "backoff_seconds",
            }
            if (
                identity.retry_of != last_id
                or identity.attempt != last.identity.attempt + 1
                or any(getattr(identity, key) != getattr(last.identity, key) for key in stable)
                or last.outcome is None
                or not (
                    last.outcome.status in {"failed", "timed_out"}
                    or (
                        last.outcome.status == "completed"
                        and last.validation is not None
                        and last.validation.status == "invalid"
                    )
                )
            ):
                raise ValueError("Invalid LLM retry lineage")
        candidate = (identity.plan_id, identity.revision)
        stage = (*operation, *candidate, identity.phase, identity.step_id)
        slot = (*operation, identity.phase, identity.step_id)
        if (
            candidates.get(operation, candidate) != candidate
            or stages.get(identity.stage_id, stage) != stage
            or slots.get(slot, identity.stage_id) != identity.stage_id
        ):
            raise ValueError("LLM candidate or stage changed identity")
        candidates = MappingProxyType({**candidates, operation: candidate})
        stages = MappingProxyType({**stages, identity.stage_id: stage})
        slots = MappingProxyType({**slots, slot: identity.stage_id})
        ordered = runs.get(identity.run_id, ())
        attempt = Attempt(identity, len(ordered), (location,))
        runs = MappingProxyType({**runs, identity.run_id: (*ordered, identity.attempt_id)})
        interactions = MappingProxyType(
            {**interactions, identity.interaction_id: identity.attempt_id}
        )
        pending = pending | {identity.attempt_id}
    else:
        if old is None or old.identity != identity:
            raise ValueError("LLM record has no matching intent identity")
        if kind == "dispatched":
            _authorized(record, identity)
            if (
                old.dispatch_sequence is not None
                or old.outcome is not None
                or pending != {identity.attempt_id}
                or operation in unvalidated
            ):
                raise ValueError("LLM dispatch before prior recording settled")
            attempt = replace(old, dispatch_sequence=event.sequence)
        elif kind == "output":
            if (
                old.dispatch_sequence is None
                or old.outcome is not None
                or data["index"] != old.output_count + 1
            ):
                raise ValueError("Out-of-order LLM output")
            metadata = dict(old.metadata)
            for name in ("reported_model", "provider_status", "usage"):
                if data.get(name) is not None:
                    metadata[name] = location
            attempt = replace(
                old,
                output_count=old.output_count + 1,
                # Match AttemptRunner's explicit observation classification;
                # later successful chunks cannot erase earlier failed evidence.
                failed_observation=(
                    old.failed_observation
                    or data.get("failed", False)
                    or data["kind"] == "error_body"
                    or data.get("provider_status") in {"failed", "incomplete", "refused"}
                ),
                metadata=tuple(metadata.items()),
            )
        elif kind == "outcome":
            marker = old.dispatch_sequence is not None
            dispatched, status = data["dispatched"], data["status"]
            if (
                old.outcome is not None
                or data["output_count"] != old.output_count
                or (dispatched is True and not marker)
                or (dispatched is False and old.output_count != 0)
                or (status in {"completed", "failed", "timed_out"} and dispatched is not True)
                or (status == "completed" and old.failed_observation)
                or (status == "not_dispatched" and dispatched is not False)
                or (dispatched is None and status != "interrupted")
            ):
                raise ValueError("Inconsistent LLM outcome")
            attempt = replace(
                old,
                outcome=Outcome(
                    status,
                    dispatched,
                    data["reported_model"],
                    data["provider_status"],
                    data["error_code"],
                ),
            )
            pending = pending - {identity.attempt_id}
            if status == "completed":
                # This operation cannot advance another send without validation.
                # A later, distinct operation need not invent a validation for an
                # abandoned candidate from an interrupted operation.
                unvalidated = MappingProxyType({**unvalidated, operation: identity.attempt_id})
        else:  # LLMRecord already restricts the remaining kind to validation.
            if (
                old.outcome is None
                or old.outcome.status != "completed"
                or old.output_count == 0
                or old.validation is not None
            ):
                raise ValueError("LLM validation requires unvalidated completed output")
            attempt = replace(old, validation=Validation(data["status"], data["error_code"]))
            remaining = dict(unvalidated)
            del remaining[operation]
            unvalidated = MappingProxyType(remaining)
        attempt = replace(attempt, records=(*old.records, location))
    return LLMIndex(
        attempts=MappingProxyType({**previous.attempts, identity.attempt_id: attempt}),
        runs=runs,
        interactions=interactions,
        candidates=candidates,
        stages=stages,
        slots=slots,
        pending=frozenset(pending),
        unvalidated=unvalidated,
        allocations=previous.allocations,
    )


def audit_reference(raw: bytes) -> PayloadRef | None:
    """The codec's exact audit pointer, or a single legacy full-record locator."""
    entry = json.loads(raw)
    return PayloadRef.model_validate(entry["audit"]) if "storage_version" in entry else None


def interrupted(attempt: Attempt, at: str, metadata: dict) -> LLMRecord:
    return LLMRecord(
        llm_audit_version=1,
        kind="outcome",
        identity=attempt.identity,
        at=at,
        data={
            "status": "interrupted",
            # A durable dispatch authorization does not prove remote execution.
            "dispatched": None if attempt.dispatch_sequence is not None else False,
            "output_count": attempt.output_count,
            "reported_model": metadata.get("reported_model"),
            "provider_status": metadata.get("provider_status"),
            "usage": metadata.get("usage"),
            "error_code": "outcome_unknown",
        },
    )


def _page(limit, cursor):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AgentFault("invalid_page", "Invalid LLM page size.", 422)
    if cursor is not None and (type(cursor) is not int or cursor < 0):
        raise AgentFault("invalid_cursor", "Invalid LLM page cursor.", 422)


def _key(value):
    # Identity selectors never become filesystem paths.
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or any(
            c not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_-"
            for c in value
        )
    ):
        raise AgentFault("invalid_llm_identity", "Invalid LLM identity.", 422)


def _summary(attempt: Attempt) -> dict:
    return {
        "identity": attempt.identity.model_dump(mode="json"),
        "ordinal": attempt.ordinal,
        "intent_sequence": attempt.records[0].sequence,
        "last_sequence": attempt.records[-1].sequence,
        "intent_at": attempt.records[0].at,
        "last_at": attempt.records[-1].at,
        "dispatch_sequence": attempt.dispatch_sequence,
        "output_count": attempt.output_count,
        "record_count": len(attempt.records),
        "outcome": asdict(attempt.outcome) if attempt.outcome else None,
        "validation": asdict(attempt.validation) if attempt.validation else None,
    }


class LLMReaders:
    def __init__(self, store):
        self.store = store

    def attempts(self, sid, rid, limit=25, before=None):
        """Newest first, with an exclusive zero-based per-run ordinal cursor."""
        _key(sid)
        _key(rid)
        _page(limit, before)
        with self.store._public._selected(sid) as (committed, _):
            if rid not in committed.views.runs:
                raise AgentFault("run_not_found", "Agent run not found.", 404)
            index = committed.llm
            ordered = index.runs.get(rid, ())
            end = len(ordered) if before is None else before
            if end > len(ordered):
                raise AgentFault("invalid_cursor", "Invalid LLM attempt frontier.", 422)
            start = max(0, end - limit)
            return {
                "session_id": sid,
                "run_id": rid,
                "at_sequence": committed.baseline.sequence,
                "total": len(ordered),
                "attempts": [_summary(index.attempts[aid]) for aid in reversed(ordered[start:end])],
                "next_before": start if start else None,
            }

    def _read(self, committed, directory, attempt: Attempt, location: AuditLocation) -> dict:
        if location.payload is not None:
            pid, digest, size = location.payload
            ref = committed.payloads[pid]
            if (ref.id, ref.sha256, ref.bytes) != (pid, digest, size):
                raise ValueError("Uncommitted LLM payload reference")
            value = self.store._read_payload(directory, ref)
        else:
            fd = os.open(
                "events.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
            )
            try:
                raw = self.store._record_bytes(fd, committed.locations[location.sequence - 1])
                legacy = JournalRecord.model_validate_json(raw)
            finally:
                os.close(fd)
            if (
                legacy.event.session_id != attempt.identity.session_id
                or legacy.event.session.id != attempt.identity.session_id
                or legacy.event.run_id != attempt.identity.run_id
                or legacy.event.operation_id != attempt.identity.operation_id
                or legacy.event.sequence != location.sequence
                or legacy.event.type != "llm." + location.kind
            ):
                raise ValueError("Unbound legacy LLM locator")
            value = legacy.audit
        record = LLMRecord.model_validate(value)
        if (
            record.identity != attempt.identity
            or record.kind != location.kind
            or record.at != location.at
        ):
            raise ValueError("LLM record changed identity")
        return {"sequence": location.sequence, "record": record.model_dump(mode="json")}

    def recovery_record(self, sid, attempt_id, at) -> LLMRecord:
        """Latest known metadata from exact committed outputs, not opaque body inference."""
        with self.store._public._selected(sid) as (committed, directory):
            attempt = committed.llm.attempts[attempt_id]
            if attempt.outcome is not None:
                raise ValueError("LLM attempt is already settled")
            sources = {}
            for name, location in attempt.metadata:
                sources.setdefault(location, []).append(name)
            metadata = {}
            for location, names in sources.items():
                # Read a shared source once. Keep only the requested metadata;
                # never cache complete response bodies across observations.
                data = self._read(committed, directory, attempt, location)["record"]["data"]
                for name in names:
                    metadata[name] = data[name]
                del data
            return interrupted(attempt, at, metadata)

    def records(self, sid, attempt_id, after=0, limit=50):
        """Ascending journal sequence, exclusive after; a finite detached snapshot.

        Items contain {sequence, record}, where record is a validated LLMRecord
        dictionary with the entire canonical body. No prefix is decoded to select
        a body. A legacy pointer decodes only its exact original full record.
        """
        _key(sid)
        _key(attempt_id)
        _page(limit, after)
        if after is None:
            raise AgentFault("invalid_cursor", "Invalid LLM record cursor.", 422)
        with self.store._public._selected(sid) as (committed, directory):
            attempt = committed.llm.attempts.get(attempt_id)
            if attempt is None:
                raise AgentFault("llm_attempt_not_found", "LLM attempt not found.", 404)
            tail = committed.baseline.sequence
            if after > tail:
                raise AgentFault("invalid_cursor", "Invalid LLM record frontier.", 422)
            start = bisect_right(attempt.records, after, key=lambda ref: ref.sequence)
            selected = attempt.records[start : start + limit]
            return {
                "session_id": sid,
                "attempt_id": attempt_id,
                "at_sequence": tail,
                "records": [self._read(committed, directory, attempt, loc) for loc in selected],
                "next_after": selected[-1].sequence if selected else after,
                "has_more": start + len(selected) < len(attempt.records),
            }
