"""Direct public v2 projections, independent of private session/codec models.

Indexes retain small headers/controls and body references, never historical full
sessions, plans, or results. Legacy bodies use a single-record locator (no prefix
replay); a request may decode one old full record to select its body. The store
still retains one latest hydrated baseline per session for internal mutation and
comparison. That baseline, private current ref index, metadata verification and
commit prefix hashing remain linear in retained history; this is bounded public
body selection, not a constant-memory or constant-I/O storage claim.
"""

import base64
import binascii
import json
import os
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from types import MappingProxyType
from typing import Literal

from app.agent_files import AgentFault
from app.agent_schemas import (
    TERMINAL,
    AgentError,
    AgentPlan,
    AgentSession,
    Effort,
    JournalRecord,
    Status,
    StepResult,
    Strict,
)
from app.session_journal import EventHeader, EventKind, PayloadRef, SessionState


class PlanControl(Strict):
    id: str
    revision: int
    digest: str
    model: str
    reasoning_effort: Effort


class RunControl(Strict):
    id: str
    status: Status
    operation_id: str | None
    created_at: str
    updated_at: str
    version_sequence: int
    plan_identity: PlanControl | None
    result_count: int
    completed_steps: int
    error: AgentError | None


class SessionHeader(Strict):
    projection_version: Literal[2] = 2
    id: str
    title: str
    model: str
    reasoning_effort: Effort
    created_at: str
    updated_at: str
    archived: bool
    archive_pending: bool
    sequence: int
    run_count: int
    latest_run_id: str | None
    current_run: RunControl | None


class RunSummary(RunControl):
    request_preview: str


class EventV2(Strict):
    schema_version: Literal[2] = 2
    sequence: int
    time: str
    session_id: str
    request_id: str
    run_id: str | None
    operation_id: str | None
    type: EventKind
    session: SessionHeader
    run: RunControl | None
    request: str | None
    plan: AgentPlan | None
    result: StepResult | None
    answer: str | None
    error: AgentError | None
    invalidates_runs: bool


@dataclass(frozen=True)
class LegacyBody:
    sequence: int
    run_id: str
    field: str
    step_id: str | None = None


Body = PayloadRef | LegacyBody


@dataclass(frozen=True)
class RunView:
    control: RunControl
    ordinal: int
    request_preview: str
    request: Body
    plan: Body | None
    results: tuple[tuple[str, Body], ...]
    answer: Body | None


@dataclass(frozen=True)
class EventView:
    envelope: EventHeader
    session: SessionHeader
    run: RunControl | None
    request: Body | None
    plan: Body | None
    result: Body | None
    answer: Body | None
    error: AgentError | None
    invalidates_runs: bool


@dataclass(frozen=True)
class PublicIndex:
    session: SessionHeader
    runs: Mapping[str, RunView]
    events: tuple[EventView, ...]
    next_ordinal: int


def index_record(
    record: JournalRecord,
    state: SessionState,
    previous: PublicIndex | None,
    baseline: AgentSession | None = None,
) -> PublicIndex:
    """Build before publication; never mutate an already published causal view."""
    event, snapshot = record.event, record.event.session
    old_runs = previous.runs if previous else {}
    prior_runs = {r.id: r for r in baseline.runs} if baseline else {}
    states = {r.id: r for r in state.runs}
    ordinal = previous.next_ordinal if previous else 0
    runs = {}
    recovery = (
        event.type == "run.interrupted"
        and event.request_id == "recovery"
        and event.run_id is None
        and event.operation_id is None
    )
    # Session-wide recovery keeps its original uncorrelated envelope. A primary
    # changed run is a review projection, not a reconstructed work/command ID.
    primary = None
    invalidates_runs = recovery or any(rid not in states for rid in old_runs)
    changed_result = None
    for run in snapshot.runs:
        old, prior = old_runs.get(run.id), prior_runs.get(run.id)
        if old is not None and run == prior:
            runs[run.id] = old
            continue
        if run.id != event.run_id:
            invalidates_runs = True
        stored = states[run.id]

        def pointer(value, field, old_pointer=None, step_id=None, prior=prior, rid=run.id):
            if value is None:
                return None
            if isinstance(value, PayloadRef):
                return value
            # Inline legacy bodies are replaced by locators in the public index.
            if prior is not None and old_pointer is not None:
                prior_value = getattr(prior, field)
                if step_id is not None:
                    prior_value = next((r for r in prior_value if r.step_id == step_id), None)
                if value == prior_value:
                    return old_pointer
            return LegacyBody(event.sequence, rid, field, step_id)

        old_results = dict(old.results) if old else {}
        results = tuple(
            (r.step_id, pointer(r.payload, "results", old_results.get(r.step_id), r.step_id))
            for r in stored.results
        )
        completed = 0
        for result in run.results:
            if result.status != "completed":
                break
            completed += 1
        control = RunControl(
            id=run.id,
            status=run.status,
            operation_id=run.operation_id,
            created_at=run.created_at,
            updated_at=run.updated_at,
            version_sequence=event.sequence,
            plan_identity=PlanControl(
                **{name: getattr(run.plan, name) for name in PlanControl.model_fields}
            )
            if run.plan
            else None,
            result_count=len(run.results),
            completed_steps=completed,
            error=run.error.model_copy(deep=True) if run.error else None,
        )
        runs[run.id] = RunView(
            control=control,
            ordinal=old.ordinal if old else ordinal,
            request_preview=run.request[:200],
            request=pointer(stored.request, "request", old.request if old else None),
            plan=pointer(stored.plan, "plan", old.plan if old else None),
            results=results,
            answer=pointer(stored.answer, "answer", old.answer if old else None),
        )
        view = runs[run.id]
        if (
            recovery
            and (primary is None or view.ordinal > primary.ordinal)
            or not recovery
            and event.run_id == run.id
        ):
            primary = view
            prior_results = {r.step_id: r for r in prior.results} if prior else {}
            candidates = [r for r in run.results if r != prior_results.get(r.step_id)]
            if event.type not in {"tool.intent", "tool.result"}:
                candidates = [
                    r for r in candidates if run.status in TERMINAL and r.status == "failed"
                ]
            changed_result = None
            if candidates and (not recovery or len(candidates) == 1):
                selected = next(
                    (r for r in candidates if r.step_id == record.audit.get("step_id")),
                    candidates[-1],
                )
                changed_result = dict(results)[selected.step_id]
            # One body cannot describe multiple changed cells. Recovery always
            # invalidates; ordinary single-run events also refresh in this case.
            if len(candidates) > 1:
                invalidates_runs = True
        if old is None:
            ordinal += 1
    active = next(
        (runs[r.id].control for r in reversed(snapshot.runs) if r.status not in TERMINAL), None
    )
    session = SessionHeader(
        **{
            name: getattr(snapshot, name)
            for name in (
                "id",
                "title",
                "model",
                "reasoning_effort",
                "created_at",
                "updated_at",
                "archived",
                "archive_pending",
                "sequence",
            )
        },
        run_count=len(runs),
        latest_run_id=snapshot.runs[-1].id if snapshot.runs else None,
        current_run=active,
    )
    run = primary if recovery else runs.get(event.run_id)
    view = EventView(
        envelope=EventHeader.model_validate(event.model_dump(exclude={"session"})),
        session=session,
        run=run.control if run else None,
        request=run.request if run and event.type == "run.planning" else None,
        plan=run.plan if run and event.type == "run.awaiting_approval" else None,
        result=changed_result,
        answer=run.answer if run and event.type == "run.completed" else None,
        error=run.control.error if run else None,
        invalidates_runs=invalidates_runs,
    )
    return PublicIndex(
        session, MappingProxyType(runs), (*previous.events, view) if previous else (view,), ordinal
    )


def _page(limit, before=None):
    if type(limit) is not int or not 1 <= limit <= 100:
        raise AgentFault("invalid_page", "Invalid Agent page size.", 422)
    if before is not None and (type(before) is not int or before < 0):
        raise AgentFault("invalid_cursor", "Invalid Agent page cursor.", 422)


def _window(total, limit, before):
    end = min(total, before) if before is not None else total
    start = max(0, end - limit)
    return start, end, start if start else None


class PublicViews:
    """Selection facade over the store's existing integrity/publication guard."""

    def __init__(self, store):
        self.store = store

    @contextmanager
    def _selected(self, sid):
        store, directory = self.store, None
        with store.lock:
            store._opened()
            # Validate identity/existence without copying the hydrated baseline.
            if sid not in store._committed:
                # Reuse the exact validation/error contract of the private reader.
                store.get(sid)
            try:
                directory = store._directory(sid)
                committed = store._verify_current(sid, directory)
                store._verify_public_artifacts(sid, directory)
                yield committed, directory
                store._healthy()
            except OSError, ValueError, TypeError, KeyError:
                raise store._recording_failure() from None
            finally:
                if directory is not None:
                    os.close(directory)

    def _reader(self, committed, directory):
        legacy = {}

        def read(ref):
            if ref is None:
                return None
            if isinstance(ref, PayloadRef):
                return self.store._read_payload(directory, ref)
            if ref.sequence not in legacy:
                fd = os.open(
                    "events.jsonl", os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory
                )
                try:
                    raw = self.store._record_bytes(fd, committed.locations[ref.sequence - 1])
                    # This locator always refers to a legacy *full* record.
                    legacy.clear()  # at most one full legacy record per request
                    legacy[ref.sequence] = JournalRecord.model_validate_json(raw)
                finally:
                    os.close(fd)
            run = next(r for r in legacy[ref.sequence].event.session.runs if r.id == ref.run_id)
            value = getattr(run, ref.field)
            if ref.step_id is not None:
                value = next(r for r in value if r.step_id == ref.step_id)
            return value.model_dump() if isinstance(value, Strict) else value

        return read

    def session(self, sid):
        with self._selected(sid) as (committed, _):
            return committed.views.session.model_dump()

    def listing(self, include_archived: bool, limit=25, cursor=None):
        _page(limit)
        if type(include_archived) is not bool:
            raise AgentFault("invalid_cursor", "Invalid Agent archive filter.", 422)
        key = None
        if cursor is not None:
            try:
                if not isinstance(cursor, str) or len(cursor) > 2048:
                    raise ValueError("invalid cursor")
                value = json.loads(base64.b64decode(cursor, altchars=b"-_", validate=True))
                if (
                    not isinstance(value, list)
                    or len(value) != 4
                    or type(value[0]) is not int
                    or value[0] != 2
                    or type(value[1]) is not bool
                    or value[1] != include_archived
                    or not all(isinstance(v, str) for v in value[2:])
                ):
                    raise ValueError("invalid cursor")
                if datetime.fromisoformat(value[2]).utcoffset() is None:
                    raise ValueError("invalid cursor timestamp")
                # Cursor content selects a position only, never a filesystem path.
                from app.sessions import SESSION_ID

                if not SESSION_ID.fullmatch(value[3]):
                    raise ValueError("invalid cursor identity")
                key = tuple(value[2:])
            except ValueError, TypeError, binascii.Error:
                raise AgentFault("invalid_cursor", "Invalid Agent list cursor.", 422) from None
        store = self.store
        with store.lock:
            store._opened()
            headers = [
                c.views.session
                for c in store._committed.values()
                if include_archived or not c.views.session.archived
            ]
            headers.sort(key=lambda h: (h.created_at, h.id), reverse=True)
            selected = [h for h in headers if key is None or (h.created_at, h.id) < key]
            page = selected[:limit]
            sessions = [self.session(h.id) for h in page]
            next_cursor = None
            if len(selected) > limit:
                last = page[-1]
                next_cursor = base64.urlsafe_b64encode(
                    json.dumps(
                        [2, include_archived, last.created_at, last.id], separators=(",", ":")
                    ).encode()
                ).decode()
            return {
                "projection_version": 2,
                "sessions": sessions,
                "next_cursor": next_cursor,
                "total": len(headers),
            }

    @staticmethod
    def _run(committed, rid):
        run = committed.views.runs.get(rid)
        if run is None:
            raise AgentFault("run_not_found", "Agent run not found.", 404)
        return run

    def runs(self, sid, limit=10, before=None):
        _page(limit, before)
        with self._selected(sid) as (committed, _):
            runs = sorted(committed.views.runs.values(), key=lambda r: r.ordinal, reverse=True)
            selected = [r for r in runs if before is None or r.ordinal < before]
            page = selected[:limit]
            return {
                "projection_version": 2,
                "session_id": sid,
                "at_sequence": committed.views.session.sequence,
                "total": len(runs),
                "runs": [
                    RunSummary(
                        **r.control.model_dump(), request_preview=r.request_preview
                    ).model_dump()
                    for r in page
                ],
                "next_before": page[-1].ordinal if len(selected) > limit else None,
            }

    def run(self, sid, rid, limit=20, before=None):
        _page(limit, before)
        with self._selected(sid) as (committed, directory):
            run = self._run(committed, rid)
            read = self._reader(committed, directory)
            start, end, next_before = _window(len(run.results), limit, before)
            control = run.control
            return {
                "projection_version": 2,
                "id": rid,
                "request": read(run.request),
                "created_at": control.created_at,
                "updated_at": control.updated_at,
                "status": control.status,
                "operation_id": control.operation_id,
                "plan": read(run.plan),
                "results": [read(ref) for _, ref in run.results[start:end]],
                "answer": read(run.answer),
                "error": control.error.model_dump() if control.error else None,
                "clarification": None,
                "version_sequence": control.version_sequence,
                "result_count": control.result_count,
                "completed_steps": control.completed_steps,
                "results_before": next_before,
            }

    def results(self, sid, rid, limit=20, before=None):
        _page(limit, before)
        with self._selected(sid) as (committed, directory):
            run = self._run(committed, rid)
            read = self._reader(committed, directory)
            start, end, next_before = _window(len(run.results), limit, before)
            return {
                "projection_version": 2,
                "session_id": sid,
                "run_id": rid,
                "at_sequence": committed.views.session.sequence,
                "total": len(run.results),
                "completed_steps": run.control.completed_steps,
                "results": [read(ref) for _, ref in run.results[start:end]],
                "next_before": next_before,
            }

    def events(self, sid, after, limit=50, through=None):
        _page(limit, after)
        if type(after) is not int or (
            through is not None and (type(through) is not int or through < after)
        ):
            raise AgentFault("invalid_cursor", "Invalid Agent event cursor.", 422)
        with self._selected(sid) as (committed, directory):
            tail = committed.views.session.sequence
            if through is not None and through > tail:
                raise AgentFault("invalid_cursor", "Invalid Agent event frontier.", 422)
            frontier = tail if through is None else through
            end = min(frontier, after + limit)
            read = self._reader(committed, directory)
            events = []
            for event in committed.views.events[after:end]:
                envelope = event.envelope.model_dump(exclude={"schema_version"})
                events.append(
                    EventV2(
                        **envelope,
                        session=event.session,
                        run=event.run,
                        request=read(event.request),
                        plan=read(event.plan),
                        result=read(event.result),
                        answer=read(event.answer),
                        error=event.error,
                        invalidates_runs=event.invalidates_runs,
                    ).model_dump()
                )
            next_after = events[-1]["sequence"] if events else after
            return {
                "projection_version": 2,
                "events": events,
                "next_after": next_after,
                "through_sequence": frontier,
                "has_more": next_after < frontier,
            }
