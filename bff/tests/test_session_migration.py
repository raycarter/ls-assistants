"""Black-box migration contracts; every archive and workspace lives in tmp_path.

Legacy input is assembled as literal public-v1 JSON, independently of SessionStore
and JournalRecord serialization. Private v2 field names/encodings are intentionally
opaque: only the version, external payloads, and public replay contract are checked.
"""

import hashlib
import json
import threading
from collections.abc import MutableSequence, Sequence
from contextlib import asynccontextmanager, contextmanager
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent import AgentService
from app.agent_files import AgentFault, FilePolicy
from app.agent_schemas import (
    AgentRun,
    Claim,
    CreateSession,
    Feedback,
    JournalRecord,
    PlanIdentity,
    Proposal,
    StartRun,
)
from app.sessions import SessionStore

SID = "20261016T123456123456Z-ab12"
NEW_SID = "20261016T123456123456Z-cd34"
STAMP = "2026-10-16T12:34:56Z"
CREATE = {"request_id": "legacy-create", "model": "deployment-a", "reasoning_effort": "high"}
PUBLIC_SESSION_FIELDS = {
    "id",
    "title",
    "model",
    "reasoning_effort",
    "created_at",
    "updated_at",
    "archived",
    "archive_pending",
    "sequence",
    "runs",
}


def digest(value):
    """An independent oracle for the existing canonical public/command digests."""
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def proposal_json():
    return {
        "objective": "Read the fixture",
        "assumptions": ["The fixture may exist."],
        "limits": {"max_steps": 3, "max_tool_calls": 3, "timeout_seconds": 30},
        "steps": [
            {
                "title": "Read",
                "description": "Inspect the approved text only.",
                "tool": "read_file",
                "arguments": {
                    "workspace_id": "fixture",
                    "path": "note.txt",
                    "offset": 1,
                    "limit": 2,
                },
            }
        ],
    }


def legacy_records(*, policy=None, interrupted=False, pending_archive=False):
    """Literal unversioned {event, claim, audit} records, never writer output."""
    session = {
        "id": SID,
        "title": "Legacy reviewed session",
        "model": "deployment-a",
        "reasoning_effort": "high",
        "created_at": STAMP,
        "updated_at": STAMP,
        "archived": False,
        "archive_pending": False,
        "sequence": 0,
        "runs": [],
    }
    records = []

    def record(kind, request_id, *, run_id=None, operation_id=None, claim=None, audit=None):
        session["sequence"] += 1
        records.append(
            {
                "event": {
                    "schema_version": 1,
                    "sequence": session["sequence"],
                    "time": STAMP,
                    "session_id": SID,
                    "request_id": request_id,
                    "run_id": run_id,
                    "operation_id": operation_id,
                    "type": kind,
                    "session": deepcopy(session),
                },
                "claim": deepcopy(claim),
                "audit": deepcopy(audit) if audit is not None else {},
            }
        )

    record(
        "session.created",
        CREATE["request_id"],
        claim={
            "request_id": CREATE["request_id"],
            "digest": digest(CREATE),
        },
    )
    run = {
        "id": "legacy-run",
        "request": "Read the note",
        "created_at": STAMP,
        "updated_at": STAMP,
        "status": "planning",
        "operation_id": "legacy-planning",
        "plan": None,
        "results": [],
        "answer": None,
        "error": None,
        "clarification": None,
    }
    session["runs"].append(run)
    record(
        "run.planning",
        "legacy-plan",
        run_id=run["id"],
        operation_id="legacy-planning",
        claim={
            "request_id": "legacy-plan",
            "digest": digest(
                {
                    "action": "plan:None",
                    "body": {"request_id": "legacy-plan", "content": "Read the note"},
                }
            ),
        },
    )
    record(
        "planner.attempt",
        "legacy-plan",
        run_id=run["id"],
        operation_id="legacy-planning",
        audit={
            "input": {"request": "Read the note", "attempt": 1, "feedback": None},
            "usage": "unknown",
            "retries": 0,
            "opaque_legacy": {"keep": [1, "é", None]},
        },
    )
    record(
        "planner.result",
        "legacy-plan",
        run_id=run["id"],
        operation_id="legacy-planning",
        audit={"proposal": proposal_json(), "status": "completed"},
    )
    material = {
        **proposal_json(),
        "id": "legacy-plan-id",
        "revision": 1,
        "model": "deployment-a",
        "reasoning_effort": "high",
        "access_policy": policy if policy is not None else {"version": "literal-legacy-policy"},
        "steps": [{**proposal_json()["steps"][0], "id": "legacy-step"}],
    }
    run["plan"] = {
        **material,
        "digest": digest({"session": SID, "run": run["id"], "plan": material}),
    }
    run["status"], run["operation_id"] = "awaiting_approval", None
    record("run.awaiting_approval", "legacy-plan", run_id=run["id"], operation_id="legacy-planning")
    if interrupted or pending_archive:
        approval = identity(run["plan"], "legacy-execute")
        run["status"], run["operation_id"] = "approved", "legacy-execution"
        record(
            "run.approved",
            "legacy-execute",
            run_id=run["id"],
            operation_id="legacy-execution",
            claim={
                "request_id": "legacy-execute",
                "digest": digest(
                    {
                        "action": f"execute:{run['id']}",
                        "body": approval,
                    }
                ),
            },
        )
        run["status"] = "executing"
        record("run.executing", "legacy-execute", run_id=run["id"], operation_id="legacy-execution")
        run["results"].append(
            {
                "step_id": "legacy-step",
                "status": "running",
                "content": "",
                "time": STAMP,
                "error": None,
                "path": None,
                "offset": None,
                "returned_lines": None,
                "truncated": False,
                "fetch": None,
            }
        )
        record(
            "tool.intent",
            "legacy-execute",
            run_id=run["id"],
            operation_id="legacy-execution",
            audit={
                "step_id": "legacy-step",
                "tool": "read_file",
                "arguments": proposal_json()["steps"][0]["arguments"],
                "decision": "allowed_exact_approved_arguments",
            },
        )
        if pending_archive:
            run["status"] = "cancelling"
            record(
                "run.cancelling",
                "legacy-execute",
                run_id=run["id"],
                operation_id="legacy-execution",
            )
            session["archive_pending"] = True
            record(
                "session.archive_pending",
                "legacy-archive",
                claim={
                    "request_id": "legacy-archive",
                    "digest": digest(
                        {"action": "archive", "body": {"request_id": "legacy-archive"}}
                    ),
                },
            )
    return records


def write_legacy(root, records):
    directory = root / SID
    directory.mkdir(parents=True)
    raw = b"".join((json.dumps(record, ensure_ascii=False) + "\n").encode() for record in records)
    (directory / "events.jsonl").write_bytes(raw)
    return raw


@contextmanager
def opened_store(root, **kwargs):
    store = SessionStore(root, **kwargs)
    try:
        store.start()
        yield store
    finally:
        store.close()


def dumps(records):
    return [record.model_dump(mode="json") for record in records]


def assert_claims(store, sid, expected):
    for record in expected:
        if claim := record["claim"]:
            assert store.claim(sid, claim["request_id"], claim["digest"]) is False
            with pytest.raises(AgentFault) as failure:
                store.claim(sid, claim["request_id"], "different-content")
            assert failure.value.code == "request_conflict"
    assert store.claim(sid, "not-yet-claimed", "new-digest") is True


def assert_v2(raw):
    """Do not depend on the author's delta/reference field layout."""
    records = [json.loads(line) for line in raw.splitlines()]
    assert records, "Expected at least one newly committed v2 journal record"

    def no_full_session(value):
        if isinstance(value, dict):
            assert not PUBLIC_SESSION_FIELDS.issubset(value), "v2 embeds a full public session"
            for item in value.values():
                no_full_session(item)
        elif isinstance(value, list):
            for item in value:
                no_full_session(item)

    for record in records:
        assert record.get("storage_version") == 2
        no_full_session(record)


def payload_images(root, sid=SID):
    directory = root / sid / "payloads"
    assert directory.is_dir(), "New run/plan data must be persisted as immutable JSON payloads"
    images = {path.name: path.read_bytes() for path in directory.glob("*.json")}
    assert images, "Expected external JSON payloads for newly recorded run/plan data"
    for raw in images.values():
        json.loads(raw)
    return images


def assert_immutable(root, images, sid=SID):
    for name, raw in images.items():
        assert (root / sid / "payloads" / name).read_bytes() == raw


def identity(plan, request_id):
    return {
        "request_id": request_id,
        "plan_id": plan["id"],
        "revision": plan["revision"],
        "digest": plan["digest"],
    }


def append_rejection(store, sid=SID, request_id="new-reject"):
    snapshot = store.get(sid)
    snapshot.runs[0].status = "rejected"
    claim = Claim(request_id=request_id, digest=digest({"command": request_id}))
    event = store.append(
        snapshot,
        "run.rejected",
        request_id,
        run_id="legacy-run",
        claim=claim,
        audit={"reason": "new durable decision", "nested": {"values": [1, 2]}},
    )
    return {
        "event": event.model_dump(mode="json"),
        "claim": claim.model_dump(),
        "audit": {"reason": "new durable decision", "nested": {"values": [1, 2]}},
    }


@pytest.fixture
def migration_archive(tmp_path):
    root, workspace = tmp_path / "journal", tmp_path / "workspace"
    root.mkdir()
    workspace.mkdir()
    (workspace / "note.txt").write_text("alpha\nbeta\ngamma\n")
    files = FilePolicy({"fixture": workspace})
    info = root.stat()
    files.bind_storage(root, identity=(info.st_dev, info.st_ino))
    legacy = legacy_records(policy=files.projection())
    raw = write_legacy(root, legacy)
    return SimpleNamespace(root=root, workspace=workspace, legacy=legacy, raw=raw)


@asynccontextmanager
async def opened_service(archive):
    files = FilePolicy({"fixture": archive.workspace})
    files.read = Mock(wraps=files.read)
    planner = SimpleNamespace(plan=AsyncMock(return_value=Proposal.model_validate(proposal_json())))
    fetcher = SimpleNamespace(
        policy_version="migration-no-network",
        fetch=AsyncMock(side_effect=AssertionError("Migration must not dispatch network work")),
    )
    owner = AgentService(
        SessionStore(archive.root, clock=lambda: STAMP),
        planner=planner,
        files=files,
        fetcher=fetcher,
        models=("deployment-a",),
    )
    try:
        await owner.start()
        yield owner
    finally:
        await owner.close()


async def finish(owner, operation):
    frames = [frame async for frame in owner.frames(operation)]
    await operation.task
    events = [json.loads(frame.split("data: ", 1)[1]) for frame in frames]
    assert all(event["schema_version"] == 2 for event in events)
    return events


def test_literal_v1_projection_plan_claims_and_audits_are_unchanged(migration_archive):
    archive = migration_archive
    log = archive.root / SID / "events.jsonl"
    with opened_store(archive.root) as store:
        assert store.get(SID).model_dump(mode="json") == archive.legacy[-1]["event"]["session"]
        assert dumps(store.records[SID]) == archive.legacy
        assert dumps(store.events(SID, 0)) == [record["event"] for record in archive.legacy]
        assert_claims(store, SID, archive.legacy)
        assert store.create(CreateSession(**CREATE)).id == SID  # A lost creation acknowledgement.
        with pytest.raises(AgentFault) as failure:
            store.create(CreateSession(**{**CREATE, "reasoning_effort": "low"}))
        assert failure.value.code == "request_conflict"
        assert log.read_bytes() == archive.raw  # No rewrite or synthetic migration event.


def test_mixed_v2_append_and_neighbor_creation_survive_two_restarts(migration_archive):
    archive = migration_archive
    log = archive.root / SID / "events.jsonl"
    create = {**CREATE, "request_id": "new-create"}
    with opened_store(archive.root, clock=lambda: STAMP, id_factory=lambda: NEW_SID) as store:
        appended = append_rejection(store)
        expected = [*archive.legacy, appended]
        images = payload_images(archive.root)
        neighbor = store.create(CreateSession(**create))
        # Give the neighboring v2-only session actual plan/run payload material.
        neighbor_run = deepcopy(archive.legacy[-1]["event"]["session"]["runs"][0])
        neighbor_run["status"] = "rejected"
        neighbor_plan = neighbor_run["plan"]
        neighbor_plan["digest"] = digest(
            {
                "session": NEW_SID,
                "run": neighbor_run["id"],
                "plan": {key: value for key, value in neighbor_plan.items() if key != "digest"},
            }
        )
        neighbor.runs = [AgentRun.model_validate(neighbor_run)]
        neighbor_event = store.append(neighbor, "session.updated", "neighbor-data")
        neighbor_records = dumps(store.records[NEW_SID])
        neighbor_images = payload_images(archive.root, NEW_SID)
        assert neighbor_records[-1]["event"] == neighbor_event.model_dump(mode="json")
        assert log.read_bytes().startswith(archive.raw)
        assert_v2(log.read_bytes()[len(archive.raw) :])
        assert_v2((archive.root / NEW_SID / "events.jsonl").read_bytes())
        assert dumps(store.records[SID]) == expected
        used = store.used
        committed = log.read_bytes()
    for _ in range(2):
        with opened_store(archive.root, clock=lambda: STAMP) as store:
            assert dumps(store.records[SID]) == expected
            assert dumps(store.records[NEW_SID]) == neighbor_records
            assert store.get(SID).model_dump(mode="json") == appended["event"]["session"]
            assert (
                store.get(NEW_SID).model_dump(mode="json")
                == neighbor_records[-1]["event"]["session"]
            )
            assert store.create(CreateSession(**CREATE)).id == SID
            assert store.create(CreateSession(**create)).id == NEW_SID
            assert_claims(store, SID, expected)
            assert_claims(store, NEW_SID, neighbor_records)
            assert len(store.listing(False)) == 2
            assert store.used == used  # Includes JSON payload charges, not just journal bytes.
            assert log.read_bytes() == committed
            assert_immutable(archive.root, images)
            assert_immutable(archive.root, neighbor_images, NEW_SID)


def test_records_are_a_read_only_sequence_of_exact_historical_records(migration_archive):
    archive = migration_archive
    with opened_store(archive.root) as store:
        expected = [*archive.legacy, append_rejection(store)]
        records = store.records[SID]
        assert isinstance(records, Sequence)
        assert not isinstance(records, MutableSequence)
        assert len(records) == len(expected)
        assert all(isinstance(record, JournalRecord) for record in records)
        assert dumps(iter(records)) == expected
        for index in (0, 2, -1, -len(expected)):
            assert records[index].model_dump(mode="json") == expected[index]
        for selection in (
            slice(None),
            slice(1, 4),
            slice(-3, None),
            slice(None, None, -1),
            slice(0, len(expected), 2),
            slice(99, 100),
        ):
            assert dumps(records[selection]) == expected[selection]
        for index in (len(expected), -len(expected) - 1):
            with pytest.raises(IndexError):
                _ = records[index]
        with pytest.raises(TypeError):
            records[0] = records[-1]
        assert not hasattr(records, "append")


@pytest.mark.parametrize("action", ["get", "append", "close"])
def test_paused_history_iterator_does_not_own_store_lock(migration_archive, action):
    archive = migration_archive
    with opened_store(archive.root) as store:
        append_rejection(store)
        snapshot = store.get(SID)
        snapshot.title = "Updated while history consumer is paused"
        iterator = iter(store.records[SID])
        entered, completed = threading.Event(), threading.Event()
        failures, results = [], []

        def work():
            entered.set()
            try:
                if action == "get":
                    results.append(store.get(SID).sequence)
                elif action == "append":
                    event = store.append(snapshot, "session.updated", "paused-iterator-update")
                    results.append(event.sequence)
                else:
                    store.close()
                    results.append("closed")
            except BaseException as exc:
                failures.append(exc)
            finally:
                completed.set()

        worker = threading.Thread(target=work, daemon=True)
        completed_while_paused = False
        try:
            assert next(iterator).model_dump(mode="json") == archive.legacy[0]
            worker.start()
            assert entered.wait(3), "Worker did not enter the requested store operation"
            completed_while_paused = completed.wait(3)
        finally:
            # Release a buggy generator's lock before joining or store teardown.
            # Sequence iterators need not implement close(), so do not require it.
            if close := getattr(iterator, "close", None):
                close()
            if worker.ident is not None:
                worker.join(timeout=3)
        assert not worker.is_alive(), "Worker failed to settle after iterator cleanup"
        assert not failures, failures
        assert completed_while_paused, f"Paused history iterator blocked store.{action}()"
        expected = {"get": snapshot.sequence, "append": snapshot.sequence + 1, "close": "closed"}
        assert results == [expected[action]]


@pytest.mark.parametrize("source", ["get", "events", "index", "slice", "iterator"])
def test_returned_objects_cannot_mutate_cached_history(migration_archive, source):
    archive = migration_archive
    with opened_store(archive.root) as store:
        expected = [*archive.legacy, append_rejection(store)]
        if source == "get":
            session = store.get(SID)
        elif source == "events":
            session = store.events(SID, len(expected) - 1)[0].session
        else:
            records = store.records[SID]
            record = {
                "index": lambda: records[-1],
                "slice": lambda: records[-1:][0],
                "iterator": lambda: list(records)[-1],
            }[source]()
            record.claim.digest = "mutated-claim"
            record.audit["nested"]["values"].append("mutated-audit")
            session = record.event.session
        session.title = "mutated-title"
        session.runs[0].plan.steps[0].arguments.path = "mutated.txt"
        session.runs[0].plan.assumptions.append("mutated-assumption")
        session.runs[0].status = "failed"
        assert store.get(SID).model_dump(mode="json") == expected[-1]["event"]["session"]
        assert dumps(store.records[SID]) == expected
        assert dumps(store.events(SID, 0)) == [record["event"] for record in expected]
        assert_claims(store, SID, expected)


def test_mutating_legacy_result_before_advancing_same_iterator_cannot_change_v2_view(tmp_path):
    root = tmp_path / "journal"
    legacy = legacy_records(interrupted=True)
    # Complete the literal legacy intent independently of the current writer.
    result_record = deepcopy(legacy[-1])
    result_event = result_record["event"]
    result_event.update(type="tool.result", sequence=result_event["sequence"] + 1)
    result_event["session"]["sequence"] = result_event["sequence"]
    original_content = "1: literal alpha\n2: literal beta"
    result_event["session"]["runs"][0]["results"][0] = {
        "step_id": "legacy-step",
        "status": "completed",
        "content": original_content,
        "time": STAMP,
        "error": None,
        "path": "note.txt",
        "offset": 1,
        "returned_lines": 2,
        "truncated": True,
        "fetch": None,
    }
    result_record["audit"] = {"step_id": "legacy-step", "status": "completed"}
    legacy.append(result_record)
    completed_record = deepcopy(result_record)
    completed_event = completed_record["event"]
    completed_event.update(type="run.completed", sequence=completed_event["sequence"] + 1)
    completed_event["session"]["sequence"] = completed_event["sequence"]
    completed_run = completed_event["session"]["runs"][0]
    completed_run.update(status="completed", operation_id=None, answer="Literal legacy answer")
    completed_record["audit"] = {}
    legacy.append(completed_record)
    raw = write_legacy(root, legacy)
    with opened_store(root, clock=lambda: STAMP) as store:
        snapshot = store.get(SID)
        snapshot.title = "Control-only update after legacy completion"
        event = store.append(snapshot, "session.updated", "control-only-update")
        expected = event.model_dump(mode="json")
        assert_v2((root / SID / "events.jsonl").read_bytes()[len(raw) :])
    for _ in range(2):
        with opened_store(root) as store:
            iterator = iter(store.records[SID])
            try:
                for original in legacy:
                    record = next(iterator)
                    assert record.model_dump(mode="json") == original
                # Mutate the yielded v1 object while replay is still paused at
                # that exact boundary, before the same iterator applies v2.
                record.event.session.runs[0].results[0].content = "Caller changed the old result"
                subsequent = next(iterator)
                assert subsequent.event.session.runs[0].results[0].content == original_content
                assert subsequent.event.model_dump(mode="json") == expected
                assert store.get(SID).model_dump(mode="json") == expected["session"]
            finally:
                if close := getattr(iterator, "close", None):
                    close()
            assert dumps(store.records[SID]) == [
                *legacy,
                {"event": expected, "claim": None, "audit": {}},
            ]


async def test_large_mixed_history_paginates_by_sequence_even_when_time_moves_backwards(
    migration_archive,
):
    archive = migration_archive
    expected = deepcopy(archive.legacy)
    now = [STAMP]
    with opened_store(archive.root, clock=lambda: now[0]) as store:
        for sequence in range(len(expected) + 1, 154):
            now[0] = (datetime(2026, 10, 16, tzinfo=UTC) - timedelta(seconds=sequence)).isoformat()
            snapshot = store.get(SID)
            snapshot.title = f"Historical title {sequence} — é"
            claim = Claim(request_id=f"title-{sequence}", digest=digest({"title": snapshot.title}))
            audit = {"ordinal": sequence, "nested": {"values": [sequence, "é"]}}
            event = store.append(
                snapshot,
                "session.updated",
                claim.request_id,
                claim=claim,
                audit=audit,
            )
            # Build the expected public snapshot independently of the returned event.
            view = deepcopy(expected[-1]["event"]["session"])
            view.update(title=snapshot.title, sequence=sequence, updated_at=now[0])
            public = {
                "schema_version": 1,
                "sequence": sequence,
                "time": now[0],
                "session_id": SID,
                "request_id": claim.request_id,
                "run_id": None,
                "operation_id": None,
                "type": "session.updated",
                "session": view,
            }
            assert event.model_dump(mode="json") == public
            expected.append({"event": public, "claim": claim.model_dump(), "audit": audit})
        images = payload_images(archive.root)
        assert_v2((archive.root / SID / "events.jsonl").read_bytes()[len(archive.raw) :])
    for _ in range(2):
        async with opened_service(archive) as owner:
            cursors = (
                0,
                1,
                4,
                5,
                6,
                23,
                49,
                50,
                51,
                99,
                100,
                101,
                102,
                103,
                149,
                150,
                152,
                153,
                200,
            )
            for cursor in cursors:
                page = await owner.history(SID, cursor)
                assert dumps(page["events"]) == [
                    record["event"] for record in expected[cursor : cursor + 50]
                ]
                assert page["has_more"] is (cursor + 50 < len(expected))
            replay, cursor = [], 0
            while True:
                page = await owner.history(SID, cursor)
                replay.extend(dumps(page["events"]))
                if not page["has_more"]:
                    break
                cursor = page["events"][-1].sequence
            assert replay == [record["event"] for record in expected]
            assert dumps(owner.store.records[SID][47:104:3]) == expected[47:104:3]
            assert owner.store.records[SID][4].event.session.title == "Legacy reviewed session"
            assert owner.store.records[SID][5].event.session.title == "Historical title 6 — é"
            assert owner.store.get(SID).title == "Historical title 153 — é"
            assert expected[-1]["event"]["time"] < expected[5]["event"]["time"]
            assert_claims(owner.store, SID, expected)
            assert_immutable(archive.root, images)
            owner.planner.plan.assert_not_awaited()
            owner.files.read.assert_not_called()
            owner.fetcher.fetch.assert_not_awaited()


async def test_legacy_approval_is_consumed_once_and_creation_lost_ack_retries_are_stable(
    migration_archive,
):
    archive = migration_archive
    plan = archive.legacy[-1]["event"]["session"]["runs"][0]["plan"]
    body = PlanIdentity(**identity(plan, "new-execute"))
    async with opened_service(archive) as owner:
        assert (await owner.create(CreateSession(**CREATE))).id == SID
        operation = await owner.prepare(SID, body, rid="legacy-run", action="execute")
        events = await finish(owner, operation)
        assert [event["type"] for event in events] == [
            "run.approved",
            "run.executing",
            "tool.intent",
            "tool.result",
            "run.completed",
        ]
        snapshot = (await owner.get(SID)).model_dump(mode="json")
        assert snapshot["runs"][0]["status"] == "completed"
        assert snapshot["runs"][0]["results"][0]["content"] == "1: alpha\n2: beta"
        assert snapshot["runs"][0]["plan"] == plan
        owner.files.read.assert_called_once()
        owner.planner.plan.assert_not_awaited()
        records = dumps(owner.store.records[SID])
        assert records[: len(archive.legacy)] == archive.legacy
        assert records[len(archive.legacy)]["claim"] == {
            "request_id": body.request_id,
            "digest": digest({"action": "execute:legacy-run", "body": body.model_dump()}),
        }
        images = payload_images(archive.root)
    for _ in range(2):
        async with opened_service(archive) as owner:
            before = (archive.root / SID / "events.jsonl").read_bytes()
            assert_v2(before[len(archive.raw) :])
            assert (await owner.create(CreateSession(**CREATE))).model_dump(mode="json") == snapshot
            for command, code in (
                (body, "duplicate_command"),
                (body.model_copy(update={"request_id": "retry"}), "stale_plan"),
                (body.model_copy(update={"digest": "0" * 64}), "request_conflict"),
            ):
                with pytest.raises(AgentFault) as failure:
                    await owner.prepare(SID, command, rid="legacy-run", action="execute")
                assert failure.value.code == code
            assert dumps(owner.store.records[SID]) == records
            assert_claims(owner.store, SID, records)
            assert (archive.root / SID / "events.jsonl").read_bytes() == before
            assert_immutable(archive.root, images)
            owner.files.read.assert_not_called()
            owner.planner.plan.assert_not_awaited()
            owner.fetcher.fetch.assert_not_awaited()


async def test_new_feedback_plan_digest_and_claim_survive_mixed_restarts(migration_archive):
    archive = migration_archive
    old_plan = archive.legacy[-1]["event"]["session"]["runs"][0]["plan"]
    feedback = Feedback(
        **identity(old_plan, "new-feedback"),
        kind="feedback",
        content="Keep it short",
    )
    async with opened_service(archive) as owner:
        operation = await owner.prepare(SID, feedback, rid="legacy-run", action="revise")
        events = await finish(owner, operation)
        assert events[-1]["type"] == "run.awaiting_approval"
        plan = (await owner.get(SID)).runs[0].plan.model_dump(mode="json")
        assert plan["id"] == old_plan["id"] and plan["revision"] == 2
        assert plan["digest"] != old_plan["digest"]
        assert plan["digest"] == digest(
            {
                "session": SID,
                "run": "legacy-run",
                "plan": {key: value for key, value in plan.items() if key != "digest"},
            }
        )
        owner.planner.plan.assert_awaited_once()
        assert owner.planner.plan.call_args.args[0].feedback == "Keep it short"
        owner.files.read.assert_not_called()
        records = dumps(owner.store.records[SID])
        assert records[: len(archive.legacy)] == archive.legacy
        assert records[len(archive.legacy)]["audit"] == {
            "feedback": "Keep it short",
            "plan_id": operation.plan_id,
            "revision": operation.revision,
            "phase": operation.phase,
            "stage_id": operation.stage_id,
        }
        assert (archive.root / SID / "events.jsonl").read_bytes().startswith(archive.raw)
        assert records[len(archive.legacy)]["claim"] == {
            "request_id": feedback.request_id,
            "digest": digest({"action": "revise:legacy-run", "body": feedback.model_dump()}),
        }
        images = payload_images(archive.root)
    for restart in range(2):
        async with opened_service(archive) as owner:
            assert (await owner.get(SID)).runs[0].plan.model_dump(mode="json") == plan
            assert dumps(owner.store.records[SID]) == records
            assert_claims(owner.store, SID, records)
            with pytest.raises(AgentFault) as failure:
                await owner.prepare(SID, feedback, rid="legacy-run", action="revise")
            assert failure.value.code == "duplicate_command"
            with pytest.raises(AgentFault) as failure:
                await owner.prepare(
                    SID,
                    PlanIdentity(**identity(old_plan, "stale-execute")),
                    rid="legacy-run",
                    action="execute",
                )
            assert failure.value.code == "stale_plan"
            assert_immutable(archive.root, images)
            owner.planner.plan.assert_not_awaited()
            owner.files.read.assert_not_called()
            if restart == 1:
                operation = await owner.prepare(
                    SID,
                    PlanIdentity(**identity(plan, "revised-execute")),
                    rid="legacy-run",
                    action="execute",
                )
                events = await finish(owner, operation)
                assert events[-1]["type"] == "run.completed"
                owner.files.read.assert_called_once()
                assert (await owner.get(SID)).runs[0].plan.digest == plan["digest"]
                assert_immutable(archive.root, images)
            owner.fetcher.fetch.assert_not_awaited()


async def test_schema_valid_cjk_planning_reaches_review_without_storage_quota(migration_archive):
    archive = SimpleNamespace(
        root=migration_archive.root.parent / "cjk-journal",
        workspace=migration_archive.workspace,
    )
    cjk = "界" * 8000
    proposed = proposal_json()
    proposed["objective"] = cjk
    proposed["assumptions"] = [cjk for _ in range(8)]
    proposed["steps"] = [deepcopy(proposed["steps"][0]) for _ in range(3)]
    for step in proposed["steps"]:
        step["description"] = cjk
    validated = Proposal.model_validate(proposed)
    async with opened_service(archive) as owner:
        utf8_bytes = len(json.dumps(proposed, ensure_ascii=False).encode("utf-8"))
        escaped_bytes = len(json.dumps(proposed, ensure_ascii=True).encode("utf-8"))
        assert utf8_bytes < 512_000 < escaped_bytes
        owner.planner.plan.return_value = validated
        created = await owner.create(CreateSession(**{**CREATE, "request_id": "cjk-create"}))
        operation = await owner.prepare(
            created.id,
            StartRun(request_id="cjk-plan", content="Review the fixture plan"),
        )
        events = await finish(owner, operation)
        assert events[-1]["type"] == "run.awaiting_approval", events[-1]
        snapshot = await owner.get(created.id)
        run = snapshot.runs[0]
        assert run.status == "awaiting_approval" and run.error is None
        plan = run.plan.model_dump(mode="json")
        assert plan["objective"] == cjk
        assert plan["assumptions"][-8:] == proposed["assumptions"]
        assert len(plan["assumptions"]) == 9
        assert [step["description"] for step in plan["steps"]] == [cjk] * 3
        assert plan["digest"] == digest(
            {
                "session": created.id,
                "run": run.id,
                "plan": {key: value for key, value in plan.items() if key != "digest"},
            }
        )
        assert len(snapshot.model_dump_json().encode("utf-8")) < 512_000
        images = payload_images(archive.root, created.id)
        assert any(cjk.encode("utf-8") in raw for raw in images.values())
        assert_v2((archive.root / created.id / "events.jsonl").read_bytes())
        owner.planner.plan.assert_awaited_once()
        owner.files.read.assert_not_called()
        owner.fetcher.fetch.assert_not_awaited()
        assert not owner.active


@pytest.mark.parametrize("pending_archive", [False, True])
async def test_legacy_unknown_work_and_pending_archive_settle_only_once(
    migration_archive,
    pending_archive,
):
    archive = migration_archive
    legacy = legacy_records(interrupted=True, pending_archive=pending_archive)
    log = archive.root / SID / "events.jsonl"
    raw = b"".join((json.dumps(record) + "\n").encode() for record in legacy)
    log.write_bytes(raw)
    committed = records = snapshot = used = None
    for _ in range(2):
        async with opened_service(archive) as owner:
            current = (await owner.get(SID)).model_dump(mode="json")
            run = current["runs"][0]
            assert run["status"] == "interrupted" and run["operation_id"] is None
            assert run["error"]["code"] == "outcome_unknown"
            assert run["results"][0]["status"] == "failed"
            assert run["results"][0]["error"] == run["error"]
            assert current["archived"] is pending_archive
            assert current["archive_pending"] is False
            assert current["sequence"] == len(legacy) + 1
            assert dumps(owner.store.records[SID])[: len(legacy)] == legacy
            recovery = owner.store.records[SID][-1]
            assert recovery.event.type == "run.interrupted"
            assert recovery.event.request_id == "recovery"
            assert recovery.audit["outcome"] == "unknown"
            assert_claims(owner.store, SID, legacy)
            assert not owner.active
            owner.planner.plan.assert_not_awaited()
            owner.files.read.assert_not_called()
            owner.fetcher.fetch.assert_not_awaited()
            assert log.read_bytes().startswith(raw)
            assert_v2(log.read_bytes()[len(raw) :])
            if committed is None:
                committed, records, snapshot, used = (
                    log.read_bytes(),
                    dumps(owner.store.records[SID]),
                    current,
                    owner.store.used,
                )
            else:
                assert log.read_bytes() == committed
                assert dumps(owner.store.records[SID]) == records
                assert current == snapshot and owner.store.used == used


@pytest.mark.parametrize("mixed", [False, True], ids=["legacy", "mixed"])
@pytest.mark.parametrize("damage", ["unknown-version", "bad-json", "bad-middle", "bad-v2-record"])
def test_complete_corruption_and_unknown_storage_version_fail_closed(tmp_path, mixed, damage):
    root = tmp_path / "journal"
    write_legacy(root, legacy_records())
    if mixed:
        with opened_store(root) as store:
            append_rejection(store)
    log = root / SID / "events.jsonl"
    lines = log.read_bytes().splitlines(keepends=True)
    if damage == "unknown-version":
        record = json.loads(lines[-1])
        record["storage_version"] = 999
        lines[-1] = (json.dumps(record) + "\n").encode()
    elif damage == "bad-json":
        lines.append(b'{"broken":\n')
    elif damage == "bad-middle":
        lines.insert(1, b'{"broken":\n')
    else:
        lines.append(b'{"storage_version":2}\n')
    corrupt = b"".join(lines)
    log.write_bytes(corrupt)
    store = SessionStore(root)
    try:
        with pytest.raises(AgentFault) as failure:
            store.start()
        assert failure.value.code == "storage_unavailable" and failure.value.status == 503
        with pytest.raises(AgentFault) as failure:
            store.get(SID)
        assert failure.value.code == "storage_unavailable"
        assert log.read_bytes() == corrupt  # A complete bad record must never be silently skipped.
    finally:
        store.close()


@pytest.mark.parametrize("mixed", [False, True], ids=["legacy", "mixed"])
@pytest.mark.parametrize(
    "tail",
    [b'{"storage_version":2,"partial":', b"\xff", b'{"storage_version":999}'],
)
def test_only_unterminated_last_line_is_discarded(tmp_path, mixed, tail):
    root = tmp_path / "journal"
    expected = legacy_records()
    committed = write_legacy(root, expected)
    if mixed:
        with opened_store(root) as store:
            expected.append(append_rejection(store))
        committed = (root / SID / "events.jsonl").read_bytes()
    log = root / SID / "events.jsonl"
    log.write_bytes(committed + tail)
    for _ in range(2):
        with opened_store(root) as store:
            assert dumps(store.records[SID]) == expected
            assert store.get(SID).model_dump(mode="json") == expected[-1]["event"]["session"]
            assert_claims(store, SID, expected)
            assert log.read_bytes() == committed


async def test_uncommitted_json_payload_orphans_do_not_become_work(migration_archive):
    archive = migration_archive
    with opened_store(archive.root) as store:
        expected = [*archive.legacy, append_rejection(store)]
        images = payload_images(archive.root)
    log = archive.root / SID / "events.jsonl"
    committed = log.read_bytes()
    # Valid JSON, but deliberately not the private payload schema. No journal reference
    # commits this work, claim, or pending archive. Recovery may account for/clean it.
    # Preserve a writer-created filename's shape without prescribing its ID length.
    stem = next(iter(images)).removesuffix(".json")
    suffix = "0" if stem[-1] != "0" else "1"
    orphan = archive.root / SID / "payloads" / (stem[:-1] + suffix + ".json")
    assert not orphan.exists()
    orphan.write_text(
        json.dumps(
            {
                "uncommitted": legacy_records(interrupted=True, pending_archive=True),
            }
        )
    )
    for _ in range(2):
        async with opened_service(archive) as owner:
            assert dumps(owner.store.records[SID]) == expected
            assert (await owner.get(SID)).model_dump(mode="json") == expected[-1]["event"][
                "session"
            ]
            assert owner.store.claim(SID, "legacy-execute", "uncommitted") is True
            assert owner.store.claim(SID, "legacy-archive", "uncommitted") is True
            assert len(await owner.listing(False)) == 1
            assert log.read_bytes() == committed
            assert_immutable(archive.root, images)
            assert not owner.active
            owner.planner.plan.assert_not_awaited()
            owner.files.read.assert_not_called()
            owner.fetcher.fetch.assert_not_awaited()


@pytest.mark.parametrize("damage", ["missing", "invalid-json"])
def test_committed_json_payload_loss_or_corruption_fails_closed(migration_archive, damage):
    archive = migration_archive
    with opened_store(archive.root) as store:
        append_rejection(store)
        images = payload_images(archive.root)
        assert_v2((archive.root / SID / "events.jsonl").read_bytes()[len(archive.raw) :])
    # These files were all emitted by committed writes; there are no test-created orphans.
    for name in images:
        path = archive.root / SID / "payloads" / name
        if damage == "missing":
            path.unlink()
        else:
            path.write_bytes(b'{"corrupt":\n')
    store = SessionStore(archive.root)
    try:
        with pytest.raises(AgentFault) as failure:
            store.start()
        assert failure.value.code == "storage_unavailable" and failure.value.status == 503
    finally:
        store.close()
