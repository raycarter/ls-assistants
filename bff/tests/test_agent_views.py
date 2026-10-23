"""Bounded public body selection and causal paging over synthetic archives only."""

import base64
import json
from copy import deepcopy
from dataclasses import fields, is_dataclass
from unittest.mock import Mock

import pytest
from pydantic import ValidationError

import app.session_journal as codec
from app.agent_files import AgentFault
from app.agent_schemas import AgentError, AgentPlan, AgentRun, AgentSession, StepResult, Strict
from app.agent_views import EventV2, LegacyBody
from app.sessions import HistoricalRecords, SessionStore
from tests.test_session_journal import CREATE, NOW, SID, run
from tests.test_session_migration import legacy_records, write_legacy

HEADER = {
    "projection_version",
    "id",
    "title",
    "model",
    "reasoning_effort",
    "created_at",
    "updated_at",
    "archived",
    "archive_pending",
    "sequence",
    "run_count",
    "latest_run_id",
    "current_run",
}
CONTROL = {
    "id",
    "status",
    "operation_id",
    "created_at",
    "updated_at",
    "version_sequence",
    "plan_identity",
    "result_count",
    "completed_steps",
    "error",
}
EVENT = {
    "schema_version",
    "sequence",
    "time",
    "session_id",
    "request_id",
    "run_id",
    "operation_id",
    "type",
    "session",
    "run",
    "request",
    "plan",
    "result",
    "answer",
    "error",
    "invalidates_runs",
}


@pytest.fixture
def opened(tmp_path):
    store = SessionStore(tmp_path / "journal", clock=lambda: NOW, id_factory=lambda: SID)
    store.start()
    snapshot = store.create(CREATE)
    try:
        yield store, snapshot
    finally:
        store.close()


def forbid_full_history(monkeypatch, store):
    forbidden = Mock(side_effect=AssertionError("Public reads must not hydrate/replay history"))
    monkeypatch.setattr(store, "get", forbidden)
    monkeypatch.setattr(store, "events", forbidden)
    monkeypatch.setattr(store, "listing", forbidden)
    monkeypatch.setattr(store, "_history", forbidden)
    monkeypatch.setattr(codec, "materialize", forbidden)
    monkeypatch.setattr(HistoricalRecords, "__iter__", forbidden)
    monkeypatch.setattr(HistoricalRecords, "__getitem__", forbidden)
    return forbidden


def test_wire_keys_and_nonterminal_pin_are_independent_of_session_metadata(opened):
    store, snapshot = opened
    empty = store.public_session(SID)
    assert set(empty) == HEADER
    assert empty["run_count"] == 0 and empty["latest_run_id"] is None
    assert empty["current_run"] is None
    snapshot.runs = [run("old"), run("active")]
    snapshot.runs[-1].status = "awaiting_approval"
    snapshot = store.append(snapshot, "run.awaiting_approval", "review", run_id="active").session
    header = store.public_session(SID)
    control = header["current_run"]
    assert set(control) == CONTROL
    assert control["version_sequence"] == 2 and header["run_count"] == 2
    assert header["latest_run_id"] == control["id"] == "active"
    assert control["plan_identity"] == {
        "id": "plan-active",
        "revision": 1,
        "digest": "a" * 64,
        "model": "offline",
        "reasoning_effort": "high",
    }
    snapshot.title = "Foreign metadata change"
    store.append(snapshot, "session.updated", "other-client")
    header = store.public_session(SID)
    assert header["sequence"] == 3 and header["current_run"] == control
    events = store.public_events(SID, 1)["events"]
    assert all(set(e) == EVENT and set(e["session"]) == HEADER for e in events)
    assert events[0]["plan"] == snapshot.runs[-1].plan.model_dump()
    assert events[1]["request_id"] == "other-client"
    assert events[1]["run_id"] is events[1]["operation_id"] is events[1]["run"] is None
    assert all(events[1][name] is None for name in ("request", "plan", "result", "answer"))
    assert events[1]["session"]["current_run"]["version_sequence"] == 2
    assert events[1]["invalidates_runs"] is False
    with pytest.raises(ValidationError):
        EventV2.model_validate({k: v for k, v in events[1].items() if k != "invalidates_runs"})
    # Public DTO metadata never enters the durable private models.
    assert "projection_version" not in AgentSession.model_fields
    assert "version_sequence" not in type(snapshot.runs[-1]).model_fields
    assert "invalidates_runs" not in type(store.records[SID][-1].event).model_fields
    assert set(store.get(SID).model_dump()) == set(snapshot.model_dump())
    header["current_run"]["plan_identity"]["digest"] = "mutation"
    events[0]["plan"]["objective"] = "mutation"
    assert store.public_session(SID)["current_run"]["plan_identity"]["digest"] == "a" * 64
    assert store.public_events(SID, 1)["events"][0]["plan"]["objective"] == "objective"


@pytest.mark.parametrize(
    "status",
    [
        "planning",
        "awaiting_clarification",
        "awaiting_approval",
        "approved",
        "executing",
        "cancelling",
    ],
)
def test_every_nonterminal_state_is_pinned(opened, status):
    store, snapshot = opened
    active = run()
    active.status = status
    snapshot.runs = [active, run("terminal-newer")]
    store.append(snapshot, "session.updated", "add")
    header = store.public_session(SID)
    assert header["latest_run_id"] == "terminal-newer"
    assert header["current_run"]["id"] == "run"
    assert header["current_run"]["status"] == status


def test_large_history_direct_reads_select_only_one_plan_and_result_window(opened, monkeypatch):
    store, snapshot = opened
    for n in range(12):
        item = run(f"run-{n}")
        item.request = f"request-{n} " * 1000
        item.plan.objective = f"old-plan-{n} " * 50000
        item.answer = f"answer-{n} " * 1000
        item.results = [
            StepResult(
                step_id=f"step-{j}", status="completed", content=f"{n}:{j} " * 1000, time=NOW
            )
            for j in range(65)
        ]
        snapshot.runs.append(item)
        snapshot = store.append(snapshot, "run.completed", f"done-{n}", run_id=item.id).session
    for n in range(70):
        snapshot.title = f"title-{n}"
        snapshot = store.append(snapshot, "session.updated", f"title-{n}").session
    committed = store._committed[SID]
    expected = committed.views.runs["run-11"]
    read = Mock(wraps=store._read_payload)
    with monkeypatch.context() as patch:
        forbidden = forbid_full_history(patch, store)
        patch.setattr(store, "_read_payload", read)
        listing = store.public_list(False)
        assert len(listing["sessions"]) == 1 and "runs" not in listing["sessions"][0]
        summaries = store.public_runs(SID)
        assert len(summaries["runs"]) == 10 and summaries["total"] == 12
        assert set(summaries["runs"][0]) == CONTROL | {"request_preview"}
        assert summaries["runs"][0]["request_preview"] == snapshot.runs[-1].request[:200]
        metadata = store.public_events(SID, 13, limit=100)
        assert len(metadata["events"]) == 70
        read.assert_not_called()
        result_page = store.public_results(SID, "run-11")
        assert [r["step_id"] for r in result_page["results"]] == [
            f"step-{n}" for n in range(45, 65)
        ]
        assert result_page["next_before"] == 45 and result_page["completed_steps"] == 65
        assert {call.args[1].id for call in read.call_args_list} == {
            ref.id for _, ref in expected.results[45:65]
        }
        read.reset_mock()
        selected = store.public_run(SID, "run-11")
        assert selected["plan"] == snapshot.runs[-1].plan.model_dump()
        assert selected["results"] == result_page["results"]
        assert selected["results_before"] == 45
        assert selected["result_count"] == selected["completed_steps"] == 65
        assert {call.args[1].id for call in read.call_args_list} == {
            expected.request.id,
            expected.plan.id,
            expected.answer.id,
            *(ref.id for _, ref in expected.results[45:65]),
        }
        assert read.call_count == 23
        forbidden.assert_not_called()


def test_events_read_only_changed_bodies_and_keep_historical_identity(opened, monkeypatch):
    store, snapshot = opened
    item = run()
    item.status, item.operation_id = "planning", "planning-op"
    item.plan, item.results, item.answer = None, [], None
    snapshot.runs = [item]
    snapshot = store.append(
        snapshot, "run.planning", "plan-request", run_id="run", operation_id="planning-op"
    ).session
    snapshot = store.append(
        snapshot,
        "planner.attempt",
        "plan-request",
        run_id="run",
        operation_id="planning-op",
        audit={"private": "do not expose"},
    ).session
    snapshot.runs[0].plan = run().plan
    snapshot.runs[0].status, snapshot.runs[0].operation_id = "awaiting_approval", None
    snapshot = store.append(
        snapshot, "run.awaiting_approval", "plan-request", run_id="run", operation_id="planning-op"
    ).session
    snapshot.runs[0].status, snapshot.runs[0].operation_id = "executing", "execute-op"
    snapshot.runs[0].results = [
        StepResult(step_id="step-0", status="running", content="", time=NOW)
    ]
    snapshot = store.append(
        snapshot,
        "tool.intent",
        "execute-request",
        run_id="run",
        operation_id="execute-op",
        audit={"step_id": "step-0"},
    ).session
    snapshot.runs[0].results[0].status = "completed"
    snapshot.runs[0].results[0].content = "completed body"
    snapshot = store.append(
        snapshot,
        "tool.result",
        "execute-request",
        run_id="run",
        operation_id="execute-op",
        audit={"step_id": "step-0"},
    ).session
    snapshot.runs[0].answer = "final answer"
    snapshot.runs[0].status, snapshot.runs[0].operation_id = "completed", None
    snapshot = store.append(
        snapshot, "run.completed", "execute-request", run_id="run", operation_id="execute-op"
    ).session
    read = Mock(wraps=store._read_payload)
    with monkeypatch.context() as patch:
        forbid_full_history(patch, store)
        patch.setattr(store, "_read_payload", read)
        events = store.public_events(SID, 0)["events"]
    assert read.call_count == 5  # request, reviewed plan, intent, result, answer
    assert [e["sequence"] for e in events] == list(range(1, 8))
    assert events[1]["request"] == "request"
    assert events[2]["request"] is events[2]["plan"] is events[2]["result"] is None
    assert events[3]["plan"]["digest"] == "a" * 64
    assert events[4]["result"]["status"] == "running"
    assert events[5]["result"]["content"] == "completed body"
    assert events[6]["result"] is None and events[6]["answer"] == "final answer"
    assert events[3]["operation_id"] == "planning-op"
    assert events[3]["run"]["operation_id"] is None
    assert events[6]["operation_id"] == "execute-op"
    assert events[6]["run"]["operation_id"] is None
    assert all("audit" not in e and "payloads" not in e for e in events)
    assert all(e["schema_version"] == 2 for e in events)
    assert all(e["invalidates_runs"] is False for e in events)


def test_terminal_changed_failure_is_the_only_result_body_in_failure_event(opened):
    store, snapshot = opened
    snapshot.runs = [run()]
    snapshot.runs[0].status = "executing"
    snapshot.runs[0].results[-1].status = "running"
    snapshot = store.append(snapshot, "tool.intent", "execute", run_id="run").session
    fault = AgentError(code="outcome_unknown", message="Stopped without a known outcome")
    snapshot.runs[0].status = "interrupted"
    snapshot.runs[0].error = fault
    snapshot.runs[0].results[-1].status = "failed"
    snapshot.runs[0].results[-1].error = fault
    store.append(snapshot, "run.interrupted", "execute", run_id="run")
    event = store.public_events(SID, 2)["events"][0]
    assert event["result"]["step_id"] == "step-2"
    assert event["result"]["error"] == event["error"] == fault.model_dump()
    assert event["run"]["completed_steps"] == 2
    assert event["invalidates_runs"] is False


def recovery_archive(tmp_path, source, pending_archive, affected_runs, running_steps=1):
    """Persist unmatched execution evidence; opening the returned root must recover it."""
    root = tmp_path / "journal"
    legacy = legacy_records(interrupted=True, pending_archive=pending_archive)
    template = legacy[-1]["event"]["session"]["runs"][0]
    affected = []
    for n in range(affected_runs):
        item = deepcopy(template)
        item["id"] = "legacy-run" if n == 0 else f"other-run-{n}"
        if n:
            item["operation_id"] = f"other-operation-{n}"
        step = deepcopy(template["results"][0])
        prefix = {
            **step,
            "step_id": "completed-prefix",
            "status": "completed",
            "content": "Previously completed evidence",
        }
        item["results"] = [
            prefix,
            *(
                {**step, "step_id": f"running-{j}", "content": f"Unsettled run {n}, step {j}"}
                for j in range(running_steps)
            ),
        ]
        affected.append(item)
    # The newest retained run did not change during recovery and must not be
    # mistaken for the primary delta just because it is last in the snapshot.
    terminal = {
        **deepcopy(template),
        "id": "retained-newer",
        "status": "completed",
        "operation_id": None,
        "results": [],
        "answer": "Old completed answer",
    }
    retained = [*affected, terminal]
    if source == "legacy":
        legacy[-1]["event"]["session"]["runs"] = retained
        write_legacy(root, legacy)
        sid = legacy[0]["event"]["session_id"]
    else:
        if source == "mixed":
            # Open a literal v1 waiting state first, then persist v2 execution.
            # This must not recover the interrupted work before the test restart.
            waiting = legacy_records()
            write_legacy(root, waiting)
            sid = waiting[0]["event"]["session_id"]
        else:
            sid = SID
        store = SessionStore(root, clock=lambda: NOW, id_factory=lambda: sid)
        store.start()
        try:
            snapshot = store.get(sid) if source == "mixed" else store.create(CREATE)
            snapshot.runs = [AgentRun.model_validate(item) for item in retained]
            snapshot = store.append(
                snapshot,
                "tool.intent",
                "original-execution",
                run_id=affected[0]["id"],
                operation_id=affected[0]["operation_id"],
                audit={"step_id": "running-0"},
            ).session
            if pending_archive:
                snapshot.archive_pending = True
                store.append(snapshot, "session.archive_pending", "foreign-archive")
        finally:
            store.close()
    journal = root / sid / "events.jsonl"
    before = journal.read_bytes()
    return root, sid, affected, before


@pytest.mark.parametrize("source", ["v2", "legacy", "mixed"])
@pytest.mark.parametrize("pending_archive", [False, True])
@pytest.mark.parametrize("affected_runs,running_steps", [(1, 1), (2, 1), (2, 2)])
def test_actual_startup_recovery_projects_primary_delta_and_invalidates_all_changed_runs(
    tmp_path,
    monkeypatch,
    source,
    pending_archive,
    affected_runs,
    running_steps,
):
    root, sid, affected, before = recovery_archive(
        tmp_path,
        source,
        pending_archive,
        affected_runs,
        running_steps,
    )
    before_sequence = before.count(b"\n")
    primary_id = affected[-1]["id"]
    journal = root / sid / "events.jsonl"
    expected_event = committed_bytes = None
    for _ in range(2):
        store = SessionStore(root, clock=lambda: NOW)
        store.start()
        try:
            recovered_sequence = before_sequence + 1
            assert len(store.records[sid]) == recovered_sequence
            assert journal.read_bytes().startswith(before)
            physical = json.loads(journal.read_bytes().splitlines()[-1])
            assert physical["storage_version"] == 2
            assert "invalidates_runs" not in physical["event"]
            primary = store._committed[sid].views.runs[primary_id]
            body_read = Mock(wraps=store._read_payload)
            record_read = Mock(wraps=store._record_bytes)
            with monkeypatch.context() as patch:
                forbidden = forbid_full_history(patch, store)
                patch.setattr(AgentSession, "model_copy", forbidden)
                patch.setattr(store, "_read_payload", body_read)
                patch.setattr(store, "_record_bytes", record_read)
                page = store.public_events(sid, before_sequence, limit=1)
                assert page["through_sequence"] == page["next_after"] == recovered_sequence
                assert page["has_more"] is False and len(page["events"]) == 1
                event = page["events"][0]
                assert set(event) == EVENT
                assert event["type"] == "run.interrupted"
                assert event["request_id"] == "recovery"
                assert event["run_id"] is event["operation_id"] is None
                for name in (
                    "sequence",
                    "time",
                    "session_id",
                    "request_id",
                    "run_id",
                    "operation_id",
                    "type",
                ):
                    assert event[name] == physical["event"][name]
                assert event["invalidates_runs"] is True
                assert event["run"]["id"] == primary_id
                assert event["run"]["status"] == "interrupted"
                assert event["run"]["operation_id"] is None
                assert event["run"]["version_sequence"] == recovered_sequence
                assert event["run"]["result_count"] == running_steps + 1
                assert event["run"]["completed_steps"] == 1
                assert event["error"] == event["run"]["error"]
                assert event["error"]["code"] == "outcome_unknown"
                assert event["request"] is event["plan"] is event["answer"] is None
                assert event["session"]["current_run"] is None
                assert event["session"]["latest_run_id"] == "retained-newer"
                assert event["session"]["archived"] is pending_archive
                assert event["session"]["archive_pending"] is False
                if running_steps == 1:
                    assert event["result"]["step_id"] == "running-0"
                    assert event["result"]["status"] == "failed"
                    assert event["result"]["error"] == event["error"]
                    assert event["result"]["content"] == affected[-1]["results"][-1]["content"]
                    assert body_read.call_count == 1
                    assert body_read.call_args.args[1] == primary.results[-1][1]
                else:
                    # Multiple failed cells have no unique primary step. The
                    # invalidation refreshes them without inventing correlation.
                    assert event["result"] is None
                    body_read.assert_not_called()
                record_read.assert_not_called()
                body_read.reset_mock()
                assert store.public_session(sid) == event["session"]
                assert store.public_list(True)["sessions"] == [event["session"]]
                summaries = store.public_runs(sid)
                assert summaries["total"] == affected_runs + 1
                body_read.assert_not_called()
                for item in affected:
                    detail = store.public_run(sid, item["id"], limit=1)
                    assert detail["status"] == "interrupted" and detail["operation_id"] is None
                    assert detail["error"] == event["error"]
                    assert detail["version_sequence"] == recovered_sequence
                    assert detail["completed_steps"] == 1
                    assert detail["result_count"] == running_steps + 1
                    assert detail["plan"] == item["plan"]
                    assert len(detail["results"]) == 1
                    assert detail["results"][0]["status"] == "failed"
                    assert detail["results"][0]["error"] == event["error"]
                    results = store.public_results(sid, item["id"], limit=running_steps)
                    assert len(results["results"]) == running_steps
                    assert all(r["status"] == "failed" for r in results["results"])
                unchanged = next(r for r in summaries["runs"] if r["id"] == "retained-newer")
                assert unchanged["status"] == "completed" and unchanged["error"] is None
                assert unchanged["version_sequence"] <= before_sequence
                forbidden.assert_not_called()
            if expected_event is None:
                expected_event, committed_bytes = event, journal.read_bytes()
            else:
                assert event == expected_event
                assert journal.read_bytes() == committed_bytes
        finally:
            store.close()


def test_archive_only_startup_recovery_invalidates_without_inventing_a_run(opened, monkeypatch):
    store, snapshot = opened
    snapshot.runs = [run()]
    snapshot.archive_pending = True
    snapshot = store.append(snapshot, "session.archive_pending", "archive").session
    store.close()
    recovered = SessionStore(store.root)
    recovered.start()
    try:
        with monkeypatch.context() as patch:
            forbidden = forbid_full_history(patch, recovered)
            patch.setattr(AgentSession, "model_copy", forbidden)
            event = recovered.public_events(SID, snapshot.sequence)["events"][0]
            assert event["invalidates_runs"] is True and event["request_id"] == "recovery"
            assert all(
                event[name] is None
                for name in (
                    "run_id",
                    "operation_id",
                    "run",
                    "result",
                    "error",
                    "request",
                    "plan",
                    "answer",
                )
            )
            assert event["session"]["archived"] is True
            assert event["session"]["archive_pending"] is False
            forbidden.assert_not_called()
    finally:
        recovered.close()


@pytest.mark.parametrize("correlated", [False, True])
def test_multi_run_mutation_invalidates_without_changing_command_correlation(opened, correlated):
    store, snapshot = opened
    snapshot.runs = [run("older"), run("newer")]
    for item in snapshot.runs:
        item.status = "awaiting_approval"
    snapshot = store.append(snapshot, "session.updated", "initial").session
    for item in snapshot.runs:
        item.status = "rejected"
    snapshot.archived = True
    store.append(
        snapshot, "session.archived", "foreign-archive", run_id="older" if correlated else None
    )
    event = store.public_events(SID, snapshot.sequence)["events"][0]
    assert event["invalidates_runs"] is True and event["request_id"] == "foreign-archive"
    assert event["run_id"] == ("older" if correlated else None)
    assert event["operation_id"] is None
    assert event["run"]["id"] == "older" if correlated else event["run"] is None
    assert event["result"] is event["error"] is None
    for rid in ("older", "newer"):
        detail = store.public_run(SID, rid, limit=1)
        assert detail["status"] == "rejected"
        assert detail["version_sequence"] == event["sequence"]


def test_event_frontier_is_finite_under_append_and_backward_timestamps(opened):
    store, snapshot = opened
    for n in range(6):
        store.clock = lambda n=n: f"2026-10-{19 - n:02}T12:00:00Z"
        snapshot.title = str(n)
        snapshot = store.append(snapshot, "session.updated", f"foreign-{n}").session
    first = store.public_events(SID, 0, limit=3)
    assert first["through_sequence"] == 7 and first["next_after"] == 3 and first["has_more"]
    snapshot.title = "appended during paging"
    store.append(snapshot, "session.updated", "late-append")
    second = store.public_events(SID, first["next_after"], 3, first["through_sequence"])
    last = store.public_events(SID, second["next_after"], 3, first["through_sequence"])
    events = first["events"] + second["events"] + last["events"]
    assert [e["sequence"] for e in events] == list(range(1, 8))
    assert events[-1]["time"] < events[1]["time"]
    assert last["through_sequence"] == last["next_after"] == 7 and not last["has_more"]
    assert store.public_events(SID, 7)["events"][0]["request_id"] == "late-append"
    beyond = store.public_events(SID, 10**30)
    assert beyond == {
        "projection_version": 2,
        "events": [],
        "next_after": 10**30,
        "through_sequence": 8,
        "has_more": False,
    }
    for after, through in ((4, 3), (0, 99), (-1, None), (True, None), (0, True)):
        with pytest.raises(AgentFault) as failure:
            store.public_events(SID, after, through=through)
        assert failure.value.status == 422


def test_result_and_run_continuations_are_stable_append_ordinals(opened):
    store, snapshot = opened
    snapshot.runs = [run(f"run-{n}") for n in range(12)]
    snapshot.runs[-1].results = [
        StepResult(step_id=f"step-{n}", status="completed", content=str(n), time=NOW)
        for n in range(45)
    ]
    snapshot = store.append(snapshot, "session.updated", "initial-runs").session
    runs = store.public_runs(SID, 5)
    results = store.public_results(SID, "run-11", 20)
    snapshot.runs.append(run("new-run"))
    snapshot.runs[-2].results.append(
        StepResult(step_id="step-45", status="completed", content="45", time=NOW)
    )
    snapshot = store.append(snapshot, "session.updated", "concurrent-append").session
    continuation = store.public_runs(SID, 100, runs["next_before"])
    assert [r["id"] for r in runs["runs"] + continuation["runs"]] == [
        f"run-{n}" for n in reversed(range(12))
    ]
    next_results = store.public_results(SID, "run-11", 100, results["next_before"])
    assert [r["content"] for r in next_results["results"] + results["results"]] == [
        str(n) for n in range(45)
    ]
    assert next_results["next_before"] is None
    assert next_results["total"] == 46 and next_results["at_sequence"] == snapshot.sequence
    assert store.public_results(SID, "run-11", before=0)["results"] == []


def cursor(value):
    return base64.urlsafe_b64encode(json.dumps(value).encode()).decode()


def test_session_keyset_filter_and_untrusted_cursor_validation(tmp_path, monkeypatch):
    ids = iter(f"20261020T123456123456Z-{n:04d}" for n in range(8))
    store = SessionStore(tmp_path / "journal", clock=lambda: NOW, id_factory=lambda: next(ids))
    store.start()
    try:
        created = [
            store.create(CREATE.model_copy(update={"request_id": f"create-{n}"})) for n in range(6)
        ]
        created[1].archived = True
        store.append(created[1], "session.archived", "archive")
        page = store.public_list(False, 2)
        assert [s["id"] for s in page["sessions"]] == [created[5].id, created[4].id]
        assert page["total"] == 5 and page["next_cursor"]
        # Mutable updates and a newly appended session cannot shift the old keyset page.
        changed = store.get(created[0].id)
        changed.title = "updated after first page"
        store.append(changed, "session.updated", "update")
        store.create(CREATE.model_copy(update={"request_id": "new-session"}))
        with monkeypatch.context() as patch:
            forbid_full_history(patch, store)
            patch.setattr(store, "_read_payload", Mock(side_effect=AssertionError("list body")))
            rest = store.public_list(False, 100, page["next_cursor"])
            assert [s["id"] for s in rest["sessions"]] == [
                created[3].id,
                created[2].id,
                created[0].id,
            ]
            assert rest["next_cursor"] is None and rest["total"] == 6
        for invalid in (
            "not base64",
            cursor({}),
            cursor([2, False, NOW]),
            cursor([2, True, NOW, SID]),
            cursor([1, False, NOW, SID]),
            cursor([2, False, 3, SID]),
            cursor([2, False, "not-a-time", SID]),
            cursor([2, False, NOW, "../private"]),
            "x" * 2049,
        ):
            with pytest.raises(AgentFault) as failure:
                store.public_list(False, cursor=invalid)
            assert failure.value.status == 422 and not store.poisoned
        assert store.public_list(True)["total"] == 7
    finally:
        store.close()


@pytest.mark.parametrize("limit", [0, 101, -1, True, 1.5])
def test_page_bounds_are_operational_and_consistent(opened, limit):
    store, _ = opened
    for call in (
        lambda: store.public_list(False, limit),
        lambda: store.public_runs(SID, limit),
        lambda: store.public_run(SID, "missing", limit),
        lambda: store.public_results(SID, "missing", limit),
        lambda: store.public_events(SID, 0, limit),
    ):
        with pytest.raises(AgentFault) as failure:
            call()
        assert failure.value.status == 422
    assert not store.poisoned


def walk_index(value):
    yield value
    if is_dataclass(value):
        for field in fields(value):
            yield from walk_index(getattr(value, field.name))
    elif isinstance(value, Strict):
        for name in type(value).model_fields:
            yield from walk_index(getattr(value, name))
    elif hasattr(value, "values"):
        for item in value.values():
            yield from walk_index(item)
    elif isinstance(value, (tuple, list)):
        for item in value:
            yield from walk_index(item)


def test_legacy_index_retains_only_locators_and_reads_one_old_record(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    legacy = legacy_records()
    raw = write_legacy(root, legacy)
    sid = legacy[0]["event"]["session_id"]
    for restart in range(2):
        store = SessionStore(root, clock=lambda: NOW)
        store.start()
        try:
            if restart == 0:
                snapshot = store.get(sid)
                for n in range(70):
                    snapshot.title = f"v2-control-{n}"
                    snapshot = store.append(snapshot, "session.updated", f"title-{n}").session
            index = store._committed[sid].views
            values = list(walk_index(index))
            assert not any(isinstance(v, (AgentSession, AgentPlan, StepResult)) for v in values)
            assert any(isinstance(v, LegacyBody) for v in values)
            read_record = Mock(wraps=store._record_bytes)
            with monkeypatch.context() as patch:
                forbid_full_history(patch, store)
                patch.setattr(store, "_record_bytes", read_record)
                assert store.public_list(False)["sessions"][0]["run_count"] == 1
                assert store.public_runs(sid)["runs"][0]["request_preview"] == "Read the note"
                read_record.assert_not_called()
                # Read just the reviewed plan's old full record, never its prefix.
                event = store.public_events(sid, 4, limit=1)["events"][0]
                assert event["plan"] == legacy[-1]["event"]["session"]["runs"][0]["plan"]
                assert read_record.call_count == 1
                assert read_record.call_args.args[1] == store._committed[sid].locations[4]
                read_record.reset_mock()
                assert store.public_events(sid, 5, limit=100)["events"]
                read_record.assert_not_called()
            assert (root / sid / "events.jsonl").read_bytes().startswith(raw)
        finally:
            store.close()


@pytest.mark.parametrize("damage", ["journal", "request", "superseded_plan", "audit"])
@pytest.mark.parametrize("access", ["list", "session", "runs", "run", "results", "events"])
def test_public_reads_verify_committed_evidence_without_using_cached_bodies(
    opened,
    monkeypatch,
    damage,
    access,
):
    store, snapshot = opened
    snapshot.runs = [run()]
    snapshot = store.append(
        snapshot, "run.completed", "done", run_id="run", audit={"private": "audit"}
    ).session
    old = store._committed[SID]
    plan = old.views.runs["run"].plan
    snapshot.runs[0].plan.revision += 1
    store.append(snapshot, "session.updated", "revision")
    folder = store.root / SID
    if damage == "journal":
        path = folder / "events.jsonl"
        raw = path.read_bytes().replace(b"New Agent session", b"Bad Agent session", 1)
    else:
        ref = old.views.runs["run"].request if damage == "request" else plan
        if damage == "audit":
            entry = json.loads((folder / "events.jsonl").read_bytes().splitlines()[1])
            ref = codec.PayloadRef.model_validate(entry["audit"])
        path = folder / "payloads" / (ref.id + ".json")
        raw = b"X" * ref.bytes
    path.write_bytes(raw)
    with monkeypatch.context() as patch:
        forbid_full_history(patch, store)
        calls = {
            "list": lambda: store.public_list(False),
            "session": lambda: store.public_session(SID),
            "runs": lambda: store.public_runs(SID),
            "run": lambda: store.public_run(SID, "run"),
            "results": lambda: store.public_results(SID, "run"),
            "events": lambda: store.public_events(SID, 2),
        }
        with pytest.raises(AgentFault) as failure:
            calls[access]()
    assert failure.value.code == "storage_unavailable" and store.poisoned
