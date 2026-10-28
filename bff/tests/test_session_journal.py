"""Private codec, physical accounting/efficiency and durable publication boundaries.

All filesystem data is synthetic and local to pytest's temporary directory.
Legacy/mixed public replay contracts are covered independently by migration tests.
"""

import errno
import hashlib
import json
import os
import stat
import threading
from collections.abc import Sequence
from unittest.mock import Mock

import pytest

import app.session_journal as codec
import app.sessions as storage
from app.agent import AgentService
from app.agent_files import AgentFault
from app.agent_schemas import (
    AgentError,
    AgentPlan,
    AgentRun,
    Claim,
    CreateSession,
    JournalRecord,
    PatchSession,
    PlanLimits,
    PlanStep,
    ReadArguments,
    StepResult,
)
from app.sessions import SessionStore

SID = "20261020T123456123456Z-ab12"
NOW = "2026-10-20T12:00:00Z"
CREATE = CreateSession(request_id="create", model="offline", reasoning_effort="high")


@pytest.fixture
def opened(tmp_path):
    store = SessionStore(tmp_path / "journal", clock=lambda: NOW, id_factory=lambda: SID)
    store.start()
    snapshot = store.create(CREATE)
    try:
        yield store, snapshot
    finally:
        store.close()


def run(rid="run", *, large=False):
    return AgentRun(
        id=rid,
        request="request" * (1000 if large else 1),
        created_at=NOW,
        updated_at=NOW,
        status="completed",
        plan=AgentPlan(
            id="plan-" + rid,
            revision=1,
            digest="a" * 64,
            objective="objective" * (800 if large else 1),
            assumptions=[],
            model="offline",
            reasoning_effort="high",
            limits=PlanLimits(),
            steps=[
                PlanStep(
                    id="step-0",
                    title="Read",
                    description="Read synthetic input",
                    tool="read_file",
                    arguments=ReadArguments(workspace_id="test", path="test.txt"),
                )
            ],
            access_policy={"test": "offline"},
        ),
        results=[
            StepResult(
                step_id=f"step-{n}",
                status="completed",
                content=str(n) * (24000 if large else 8),
                time=NOW,
            )
            for n in range(3)
        ],
        answer="answer" * (12000 if large else 1),
    )


def append_run(store, snapshot, **kwargs):
    snapshot.runs.append(run(**kwargs))
    return store.append(snapshot, "run.completed", "complete").session


def journal(store):
    return store.root / SID / "events.jsonl"


def payloads(store):
    directory = store.root / SID / "payloads"
    return sorted(directory.iterdir()) if directory.exists() else []


def physical_bytes(store):
    return journal(store).stat().st_size + sum(p.stat().st_size for p in payloads(store))


def entries(store):
    return [json.loads(line) for line in journal(store).read_bytes().splitlines()]


def test_incremental_control_writes_reuse_payloads_and_account_physical_bytes(opened, monkeypatch):
    store, snapshot = opened
    snapshot = append_run(store, snapshot, large=True)
    before = {p.name: (p.read_bytes(), p.stat().st_ino) for p in payloads(store)}
    assert len(before) == 6  # request, plan, three results, answer
    assert store.used == physical_bytes(store)
    assert journal(store).stat().st_size < 5000
    assert sum(len(raw) for raw, _ in before.values()) > 150000
    assert len(store._committed[SID].projection.model_dump_json()) < 5000
    read = Mock(side_effect=AssertionError("Append must not re-read historical payloads"))
    with monkeypatch.context() as patch:
        patch.setattr(store, "_read_payload", read)
        for n in range(40):
            snapshot.title = f"title-{n}"
            snapshot = store.append(snapshot, "session.updated", f"title-{n}").session
    read.assert_not_called()
    assert {p.name: (p.read_bytes(), p.stat().st_ino) for p in payloads(store)} == before
    for entry in entries(store)[2:]:
        assert entry["storage_version"] == 2
        assert "session" not in entry["event"]
        assert entry["runs"] == [] and "run_order" not in entry
        assert set(entry["session"]) == {"title", "sequence"}
        assert len(codec.json_bytes(entry)) < 1000
    snapshot.runs[0].status = "rejected"
    snapshot = store.append(snapshot, "run.rejected", "reject", run_id="run").session
    assert entries(store)[-1]["runs"] == [{"id": "run", "status": "rejected"}]
    snapshot.runs[0].results[1].content = "changed result"
    snapshot = store.append(snapshot, "session.updated", "result-change").session
    delta = entries(store)[-1]["runs"][0]
    assert set(delta) == {"id", "results"}
    assert "order" not in delta["results"]
    assert [r["step_id"] for r in delta["results"]["changed"]] == ["step-1"]
    assert len(payloads(store)) == len(before) + 1
    assert store.used == physical_bytes(store)
    assert store.get(SID) == snapshot


def test_codec_explicit_clearing_all_changed_runs_and_order_roundtrip(opened):
    store, snapshot = opened
    snapshot.runs = [run("one"), run("two")]
    for item in snapshot.runs:
        item.operation_id = "old-operation"
        item.error = AgentError(code="test", message="test")
    snapshot = store.append(snapshot, "session.updated", "add-runs").session
    before = store.records[SID][-1]
    for item in snapshot.runs:
        item.operation_id = item.plan = item.answer = item.error = None
        item.status = "rejected"
        item.results.reverse()
    snapshot.runs.reverse()
    event = store.append(snapshot, "session.archived", "archive")
    entry = entries(store)[-1]
    assert entry["event"]["run_id"] is None
    assert entry["run_order"] == ["two", "one"]
    assert [r["id"] for r in entry["runs"]] == ["two", "one"]
    for delta in entry["runs"]:
        for name in ("operation_id", "plan", "answer", "error"):
            assert name in delta and delta[name] is None
        assert delta["results"] == {"order": ["step-2", "step-1", "step-0"], "changed": []}
    assert store.records[SID][-1].event == event
    assert store.records[SID][-2] == before
    assert store.get(SID) == event.session


def test_lazy_records_are_ordinary_independent_readonly_and_replay_once(opened, monkeypatch):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    for n in range(58):
        snapshot.title = str(n)
        snapshot = store.append(snapshot, "session.updated", f"title-{n}", audit={"n": n}).session
    records = store.records[SID]
    assert isinstance(records, Sequence) and not isinstance(records, list)
    spy = Mock(wraps=storage.decode)
    monkeypatch.setattr(storage, "decode", spy)
    ordinary = list(records)
    assert spy.call_count == len(records) == 60
    assert all(type(item) is JournalRecord for item in ordinary)
    assert next(iter(store.records.values())) is records and SID in store.records
    assert records[-1] == ordinary[-1] and records[1:8:2] == ordinary[1:8:2]
    assert records[::-3] == ordinary[::-3] and ordinary[2] in records
    with pytest.raises(TypeError):
        store.records[SID] = []
    with pytest.raises(TypeError):
        records[0] = ordinary[0]
    with pytest.raises(IndexError):
        _ = records[len(records)]
    with pytest.raises(IndexError):
        _ = records[-len(records) - 1]
    with pytest.raises(TypeError):
        _ = records[1.5]
    assert not store.poisoned
    ordinary[-1].event.session.runs[0].request = "caller mutation"
    ordinary[-1].audit["n"] = "caller mutation"
    assert records[-1].audit == {"n": 57}
    assert store.get(SID).runs[0].request == "request"
    spy.reset_mock()
    page = store.events(SID, 0, 100)
    assert len(page) == 50 and spy.call_count == 50
    spy.reset_mock()
    page = store.events(SID, 50)
    assert len(page) == 10 and spy.call_count == 60


def test_indexes_remain_immutable_lockfree_and_unpublished_until_final_fsync(opened, monkeypatch):
    store, snapshot = opened
    snapshot.runs.append(run())
    old = store._committed[SID]
    entered, release, read_done = threading.Event(), threading.Event(), threading.Event()
    failures, observations = [], []
    real_open, real_sync = os.open, os.fsync
    names = {}

    def opened_fd(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        names[fd] = path
        return fd

    def fsync(fd):
        if names.get(fd) == "events.jsonl":
            entered.set()
            assert release.wait(5)
        real_sync(fd)

    def writer():
        try:
            store.append(
                snapshot,
                "planner.attempt",
                "attempt",
                run_id="run",
                operation_id="operation",
                claim=Claim(request_id="attempt", digest="digest"),
                audit={"input": "test"},
            )
        except BaseException as exc:
            failures.append(exc)

    def reader():
        observations.append(
            (
                store.claim_digest(SID, "attempt"),
                store.attempt_count(SID, "run"),
                store.operation_known(SID, "run", "operation"),
                store.claim_digest(SID, "create"),
            )
        )
        read_done.set()

    monkeypatch.setattr(os, "open", opened_fd)
    monkeypatch.setattr(os, "fsync", fsync)
    worker = threading.Thread(target=writer)
    worker.start()
    lookup = threading.Thread(target=reader)
    try:
        assert entered.wait(3)
        lookup.start()
        assert read_done.wait(0.5), "Published claim lookup blocked on the store lock"
        assert observations == [(None, 0, False, old.claims["create"])]
        assert store._committed[SID] is old and len(store.records[SID]) == 1
    finally:
        release.set()
        worker.join(5)
        if lookup.ident is not None:
            lookup.join(5)
    assert not failures and not worker.is_alive()
    assert store.claim_digest(SID, "attempt") == "digest"
    assert store.attempt_count(SID, "run") == 1
    assert store.operation_known(SID, "run", "operation")
    assert not store.operation_known(SID, "other-run", "operation")
    assert "attempt" not in old.claims
    with pytest.raises(TypeError):
        old.claims["attempt"] = "mutation"
    assert not store.claim(SID, "attempt", "digest")
    with pytest.raises(AgentFault) as failure:
        store.claim(SID, "attempt", "different")
    assert failure.value.code == "request_conflict"


@pytest.mark.parametrize(
    "phase",
    [
        "payload_write",
        "payload_sync",
        "payload_dir",
        "payload_parent",
        "journal_write",
        "journal_sync",
        "journal_parent",
    ],
)
def test_fault_boundaries_no_publication_or_false_success_and_safe_restart(
    opened, monkeypatch, phase
):
    store, snapshot = opened
    snapshot.runs.append(run())
    old = store._committed[SID]
    real_open, real_write, real_sync = os.open, os.write, os.fsync
    names, calls = {}, []
    journal_written = False

    def opened_fd(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        names[fd] = os.fspath(path)
        return fd

    def write(fd, raw):
        nonlocal journal_written
        name = names[fd]
        point = "journal_write" if name == "events.jsonl" else "payload_write"
        if phase == point:
            real_write(fd, bytes(raw)[:7])  # retain a safe partial orphan/tail
            raise OSError("credential-shaped private fault")
        count = real_write(fd, raw)
        if name == "events.jsonl":
            journal_written = True
        return count

    def fsync(fd):
        name = names[fd]
        point = (
            "payload_sync"
            if name.endswith(".json")
            else "payload_dir"
            if name == "payloads"
            else "journal_sync"
            if name == "events.jsonl"
            else "journal_parent"
            if journal_written
            else "payload_parent"
        )
        calls.append(point)
        if phase == point:
            raise OSError("credential-shaped private fault")
        real_sync(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "open", opened_fd)
        patch.setattr(os, "write", write)
        patch.setattr(os, "fsync", fsync)
        with pytest.raises(AgentFault) as failure:
            store.append(
                snapshot,
                "planner.attempt",
                "attempt",
                run_id="run",
                operation_id="operation",
                claim=Claim(request_id="attempt", digest="digest"),
                audit={"test": "audit"},
            )
    assert failure.value.code == "storage_unavailable"
    assert "private fault" not in str(failure.value) and store.poisoned
    assert store._committed[SID] is old and len(store.records[SID]) == 1
    assert store.claim_digest(SID, "attempt") is None
    assert store.attempt_count(SID, "run") == 0
    assert not store.operation_known(SID, "run", "operation")
    files = {p.name for p in payloads(store)}
    store.close()
    recovered = SessionStore(store.root)
    recovered.start()
    try:
        complete = phase in {"journal_sync", "journal_parent"}
        assert recovered.get(SID).sequence == (2 if complete else 1)
        assert recovered.claim_digest(SID, "attempt") == ("digest" if complete else None)
        assert recovered.attempt_count(SID, "run") == int(complete)
        assert {p.name for p in payloads(recovered)} == files  # no orphan deletion
        assert recovered.used == physical_bytes(recovered)
        if phase in {"journal_write", "journal_sync", "journal_parent"}:
            assert calls.index("payload_sync") < calls.index("payload_dir")
            assert calls.index("payload_dir") < calls.index("payload_parent")
            if "journal_sync" in calls:
                assert calls.index("payload_parent") < calls.index("journal_sync")
        if not complete:
            assert recovered.get(SID).runs == []
        else:
            assert recovered.records[SID][-1].audit == {"test": "audit"}
    finally:
        recovered.close()


@pytest.mark.parametrize(
    "damage", ["missing", "same_size", "truncated", "symlink", "hardlink", "fifo"]
)
def test_payload_corruption_never_uses_cached_get_or_historical_bodies(opened, tmp_path, damage):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    assert store.get(SID) == snapshot and store.records[SID][-1].event.session == snapshot
    ref = entries(store)[-1]["runs"][0]["request"]
    path = store.root / SID / "payloads" / (ref["id"] + ".json")
    raw = path.read_bytes()
    if damage == "same_size":
        path.write_bytes(b"X" + raw[1:])
    elif damage == "truncated":
        path.write_bytes(raw[:-1])
    else:
        path.unlink()
        if damage in {"symlink", "hardlink"}:
            outside = tmp_path / "outside"
            outside.write_bytes(raw)
            if damage == "symlink":
                path.symlink_to(outside)
            else:
                os.link(outside, path)
        elif damage == "fifo":
            os.mkfifo(path)
    for read in (lambda: store.get(SID), lambda: store.records[SID][-1]):
        with pytest.raises(AgentFault) as failure:
            read()
        assert failure.value.code == "storage_unavailable"
        assert "outside" not in str(failure.value) and "request" not in str(failure.value)
    store.close()
    failed = SessionStore(store.root)
    with pytest.raises(AgentFault):
        failed.start()
    assert failed.fd is None and not failed.records and not failed.creations


@pytest.mark.parametrize("body", ["superseded_plan", "audit_only"])
@pytest.mark.parametrize("damage", ["missing", "same_size"])
@pytest.mark.parametrize("access", ["get", "patch", "append"])
async def test_all_committed_refs_fail_closed_live_and_on_restart(opened, body, damage, access):
    store, snapshot = opened
    snapshot.runs.append(run())
    snapshot = store.append(
        snapshot, "session.updated", "first", audit={"private": "old audit"}
    ).session
    original = entries(store)[-1]
    ref = original["audit"] if body == "audit_only" else original["runs"][0]["plan"]
    snapshot.runs[0].plan.revision = 2
    snapshot.runs[0].plan.digest = "b" * 64
    snapshot = store.append(snapshot, "session.updated", "revision").session
    assert store.get(SID) == snapshot
    committed = store._committed[SID]
    before = journal(store).read_bytes()
    path = store.root / SID / "payloads" / (ref["id"] + ".json")
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"X" * ref["bytes"])
    owner = AgentService(store)
    try:
        with pytest.raises(AgentFault) as failure:
            if access == "get":
                store.get(SID)
            elif access == "patch":
                await owner.patch(SID, PatchSession(request_id="patch", title="changed"))
            else:
                store.append(snapshot, "session.updated", "control")
        assert failure.value.code == "storage_unavailable"
        assert store._committed[SID] is committed and journal(store).read_bytes() == before
    finally:
        await owner.close()
    with pytest.raises(AgentFault):
        _ = store.records[SID][1]
    with pytest.raises(AgentFault):
        SessionStore(store.root).start()


def test_payload_ids_independent_of_public_uuid_stream_and_exclusive(opened, monkeypatch):
    store, snapshot = opened
    public_ids = Mock(side_effect=AssertionError("Payloads must not consume public UUIDs"))
    monkeypatch.setattr(storage, "uuid4", public_ids)
    snapshot = append_run(store, snapshot)
    public_ids.assert_not_called()
    for path in payloads(store):
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.parent.parent.stat().st_mode) == 0o700
    original = payloads(store)[0]
    raw = original.read_bytes()
    monkeypatch.setattr(codec.secrets, "token_hex", lambda n: original.stem)
    snapshot.runs[0].answer = "changed"
    with pytest.raises(AgentFault):
        store.append(snapshot, "session.updated", "collision")
    assert original.read_bytes() == raw
    assert len(store.records[SID]) == 2


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EIO])
def test_json_offloading_physical_failure_never_publishes(opened, monkeypatch, error):
    store, snapshot = opened
    snapshot.runs.append(run(large=True))
    monkeypatch.setattr(os, "write", Mock(side_effect=OSError(error, "physical failure")))
    with pytest.raises(AgentFault) as failure:
        store.append(snapshot, "session.updated", "too-large")
    assert failure.value.code == "storage_unavailable" and store.poisoned
    assert len(store.records[SID]) == 1
    assert store.used == physical_bytes(store)
    assert store._committed[SID].baseline.runs == []
    assert store._committed[SID].views.session.run_count == 0


def test_streaming_recovery_bounds_reads_and_fsyncs_partial_tail(opened, monkeypatch):
    store, snapshot = opened
    for n in range(80):
        snapshot.title = f"title-{n}"
        snapshot = store.append(snapshot, "session.updated", f"title-{n}").session
    original = journal(store).read_bytes()
    store.close()
    with journal(store).open("ab") as handle:
        handle.write(b"{" + b"x" * 100000)
    real_fdopen, real_sync = os.fdopen, os.fsync
    sizes, syncs = [], []

    class BoundedLines:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.stream.close()

        def readline(self, size):
            assert 0 < size <= 65536
            sizes.append(size)
            return self.stream.readline(size)

    def fsync(fd):
        syncs.append(stat.S_ISREG(os.fstat(fd).st_mode))
        real_sync(fd)

    monkeypatch.setattr(os, "fdopen", lambda *a, **kw: BoundedLines(real_fdopen(*a, **kw)))
    monkeypatch.setattr(os, "fsync", fsync)
    recovered = SessionStore(store.root)
    recovered.start()
    try:
        assert recovered.get(SID) == snapshot
        assert journal(store).read_bytes() == original
        assert len(sizes) > 82 and syncs == [True, False]
        assert recovered.used == len(original)
    finally:
        recovered.close()


@pytest.mark.parametrize("quarantined", [False, True])
def test_partial_creation_payload_orphans_are_quarantined_retained_and_counted(
    tmp_path, quarantined
):
    root = tmp_path / "journal"
    root.mkdir()
    folder = root / ((".orphan-" if quarantined else "") + SID)
    folder.mkdir()
    bodies = folder / "payloads"
    bodies.mkdir()
    raw = b'{"partial":'
    (bodies / ("a" * 32 + ".json")).write_bytes(raw)
    (folder / "events.jsonl").write_bytes(b'{"partial journal":')
    for _ in range(2):
        store = SessionStore(root)
        store.start()
        try:
            assert not store.records and not store.creations and store.listing(True) == []
            retained = root / (".orphan-" + SID)
            assert (retained / "payloads" / ("a" * 32 + ".json")).read_bytes() == raw
            assert store.used >= len(raw)
        finally:
            store.close()


@pytest.mark.parametrize("unsafe", ["folder_symlink", "file_symlink", "hardlink", "fifo", "name"])
@pytest.mark.parametrize("quarantined", [False, True])
def test_unsafe_orphan_payloads_fail_closed(tmp_path, unsafe, quarantined):
    root = tmp_path / "journal"
    root.mkdir()
    folder = root / ((".orphan-" if quarantined else "") + SID)
    folder.mkdir()
    bodies = folder / "payloads"
    bodies.mkdir()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    path = bodies / ("a" * 32 + ".json")
    if unsafe == "folder_symlink":
        bodies.rmdir()
        bodies.symlink_to(tmp_path, target_is_directory=True)
    elif unsafe == "file_symlink":
        path.symlink_to(outside)
    elif unsafe == "hardlink":
        os.link(outside, path)
    elif unsafe == "fifo":
        os.mkfifo(path)
    else:
        (bodies / "unexpected.json").write_text("{}")
    with pytest.raises(AgentFault):
        SessionStore(root).start()
    assert outside.read_text() == "untouched"


@pytest.mark.parametrize(
    "damage", ["version", "version_type", "extra", "null_control", "ref_id", "hash", "bytes"]
)
def test_complete_bad_storage_entries_fail_closed(opened, damage):
    store, snapshot = opened
    append_run(store, snapshot)
    lines = entries(store)
    entry = lines[-1]
    if damage == "version":
        entry["storage_version"] = 99
    elif damage == "version_type":
        entry["storage_version"] = 2.0
    elif damage == "extra":
        entry["session"]["arbitrary_patch"] = {}
    elif damage == "null_control":
        entry["session"]["title"] = None
    else:
        ref = entry["runs"][0]["request"]
        if damage == "ref_id":
            ref["id"] = "../outside"
        elif damage == "hash":
            ref["sha256"] = "0" * 64
        else:
            ref["bytes"] += 1
    store.close()
    journal(store).write_bytes(b"".join(codec.json_bytes(line) + b"\n" for line in lines))
    with pytest.raises(AgentFault) as failure:
        SessionStore(store.root).start()
    assert failure.value.code == "storage_unavailable"


def test_payload_receipts_bind_exact_json_bytes(opened):
    store, snapshot = opened
    append_run(store, snapshot)
    for ref in [r.payload for r in store._committed[SID].projection.runs[0].results]:
        raw = (store.root / SID / "payloads" / (ref.id + ".json")).read_bytes()
        assert len(raw) == ref.bytes and hashlib.sha256(raw).hexdigest() == ref.sha256


@pytest.mark.parametrize("zero", [False, True])
def test_short_and_zero_writes_preserve_commit_integrity(opened, monkeypatch, zero):
    store, snapshot = opened
    snapshot.runs.append(run())
    snapshot.runs[0].request = "Unicode 🌱 終 é"
    write = os.write

    def short_write(fd, raw):
        return 0 if zero else write(fd, raw[:7])

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", short_write)
        if zero:
            with pytest.raises(AgentFault):
                store.append(snapshot, "session.updated", "short-write")
            assert store.poisoned and len(store.records[SID]) == 1
        else:
            event = store.append(snapshot, "session.updated", "short-write")
            assert store.get(SID) == event.session
            assert store.records[SID][-1].event == event
            assert store.used == physical_bytes(store)


def test_invalid_server_payload_identity_is_sanitized_before_any_write(opened, monkeypatch):
    store, snapshot = opened
    snapshot.runs.append(run())
    monkeypatch.setattr(codec.secrets, "token_hex", lambda n: "../credential-shaped")
    before = journal(store).read_bytes()
    with pytest.raises(AgentFault) as failure:
        store.append(snapshot, "session.updated", "invalid-id")
    assert failure.value.code == "storage_unavailable"
    assert "credential-shaped" not in str(failure.value)
    assert not payloads(store) and journal(store).read_bytes() == before
    assert len(store.records[SID]) == 1


def test_reused_payload_removed_after_get_is_checked_at_write_boundary(opened, monkeypatch):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    snapshot = store.get(SID)
    ref = store._committed[SID].projection.runs[0].request
    path = store.root / SID / "payloads" / (ref.id + ".json")
    old = store._committed[SID]
    before = journal(store).read_bytes()
    write = store._write

    def remove_before_write(*args, **kwargs):
        path.unlink()
        return write(*args, **kwargs)

    monkeypatch.setattr(store, "_write", remove_before_write)
    snapshot.title = "updated after read"
    with pytest.raises(AgentFault) as failure:
        store.append(
            snapshot, "session.updated", "update", claim=Claim(request_id="update", digest="digest")
        )
    assert failure.value.code == "storage_unavailable"
    assert store._committed[SID] is old and store.claim_digest(SID, "update") is None
    assert journal(store).read_bytes() == before


def test_live_history_uses_pinned_root_and_closed_diagnostics_reject_replacement(opened, tmp_path):
    store, snapshot = opened
    original = journal(store).read_bytes()
    moved = tmp_path / "moved"
    store.root.rename(moved)
    replacement = SessionStore(store.root, clock=lambda: NOW, id_factory=lambda: SID)
    replacement.start()
    try:
        replacement.create(CREATE.model_copy(update={"model": "replace"}))
        assert len(journal(replacement).read_bytes()) == len(original)
        assert store.get(SID) == snapshot
        assert store.events(SID, 0)[0].session == snapshot
        assert store.records[SID][0].event.session == snapshot
        snapshot.title = "original root update"
        event = store.append(snapshot, "session.updated", "update")
        assert store.events(SID, 0)[-1] == event
        assert replacement.get(SID).model == "replace"
        assert replacement.get(SID).sequence == 1
        store.close()
        with pytest.raises(AgentFault) as failure:
            _ = store.records[SID][0]
        assert failure.value.code == "storage_unavailable"
    finally:
        replacement.close()


@pytest.mark.parametrize("field", ["title", "claim"])
@pytest.mark.parametrize("access", ["history", "get", "append"])
@pytest.mark.parametrize("replace_file", [False, True])
def test_same_length_journal_mutation_never_diverges_from_projection_or_indexes(
    opened, tmp_path, field, access, replace_file
):
    store, snapshot = opened
    original = journal(store).read_bytes()
    value = entries(store)[0]
    if field == "title":
        value["session"]["title"] = "Bad Agent session"
    else:
        value["event"]["request_id"] = value["claim"]["request_id"] = "creatx"
    changed = codec.json_bytes(value) + b"\n"
    assert len(changed) == len(original)
    if replace_file:
        replacement = tmp_path / "replacement"
        replacement.write_bytes(changed)
        os.replace(replacement, journal(store))
    else:
        journal(store).write_bytes(changed)
    info = journal(store).stat()
    os.utime(journal(store), ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    old = store._committed[SID]
    with pytest.raises(AgentFault) as failure:
        if access == "history":
            _ = store.records[SID][0]
        elif access == "get":
            store.get(SID)
        else:
            store.append(snapshot, "session.updated", "cached-delta")
    assert failure.value.code == "storage_unavailable"
    assert store._committed[SID] is old
    assert store.claim_digest(SID, "create") is not None
    assert store.claim_digest(SID, "creatx") is None
    assert journal(store).read_bytes() == changed


def test_paused_iterator_owns_prefix_and_fds_without_blocking_io_or_close(opened):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    iterator = iter(store.records[SID])
    first = next(iterator)
    done, failures = threading.Event(), []

    def worker():
        try:
            latest = store.get(SID)
            latest.title = "concurrent title"
            store.append(latest, "session.updated", "concurrent")
            store.close()
        except BaseException as exc:
            failures.append(exc)
        finally:
            done.set()

    thread = threading.Thread(target=worker)
    thread.start()
    try:
        assert done.wait(1), "A suspended history consumer blocked store I/O or shutdown"
        assert not failures and store.fd is None
        assert first.event.sequence == 1
        with pytest.raises(AgentFault) as failure:
            next(iterator)
        assert failure.value.code == "storage_unavailable"
        assert not store.poisoned
    finally:
        iterator.close()
        thread.join(5)
    assert not thread.is_alive()


@pytest.mark.parametrize("same_store", [False, True])
def test_history_after_close_and_reopen_delete_does_not_poison(opened, same_store):
    store, snapshot = opened
    append_run(store, snapshot)
    iterator = iter(store.records[SID])
    assert next(iterator).event.sequence == 1
    store.close()
    reopened = store if same_store else SessionStore(store.root, clock=lambda: NOW)
    reopened.start()
    try:
        reopened.delete(SID)
        with pytest.raises(AgentFault) as failure:
            next(iterator)
        assert (failure.value.code, failure.value.status) == ("storage_unavailable", 503)
        assert not store.poisoned and not reopened.poisoned
        assert reopened.public_list(False)["total"] == 0
    finally:
        iterator.close()
        reopened.close()


def test_inline_history_invalidated_before_deletion_or_resume(opened):
    store, snapshot = opened
    snapshot.title = "inline only"
    store.append(snapshot, "session.updated", "inline")
    assert not payloads(store)
    iterator = iter(store.records[SID])
    unstarted = iter(store.records[SID])
    assert next(iterator).event.sequence == 1
    store.close()
    reopened = SessionStore(store.root, clock=lambda: NOW)
    reopened.start()
    try:
        reopened.delete(SID)
        for reader in (iterator, unstarted):
            with pytest.raises(AgentFault) as failure:
                next(reader)
            assert (failure.value.code, failure.value.status) == ("storage_unavailable", 503)
        assert not store.poisoned and not reopened.poisoned
    finally:
        iterator.close()
        unstarted.close()
        reopened.close()


def test_close_waits_for_inflight_history_before_delete(opened, monkeypatch):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    store.append(snapshot, "session.updated", "more")
    iterator = iter(store.records[SID])
    next(iterator)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    errors, records = [], []
    original = store._read_payload

    def paused_read(*args, **kwargs):
        entered.set()
        assert release.wait(3)
        return original(*args, **kwargs)

    def read():
        try:
            records.append(next(iterator))
        except BaseException as exc:
            errors.append(exc)

    def close():
        store.close()
        closed.set()

    monkeypatch.setattr(store, "_read_payload", paused_read)
    reader = threading.Thread(target=read)
    closer = threading.Thread(target=close)
    reader.start()
    try:
        assert entered.wait(3)
        closer.start()
        assert not closed.wait(0.05)
    finally:
        release.set()
        reader.join(3)
        if closer.ident is not None:
            closer.join(3)
    assert closed.is_set() and not reader.is_alive() and not errors
    assert [record.event.sequence for record in records] == [2]
    reopened = SessionStore(store.root, clock=lambda: NOW)
    reopened.start()
    try:
        reopened.delete(SID)
        with pytest.raises(AgentFault) as failure:
            next(iterator)
        assert failure.value.code == "storage_unavailable"
        assert not store.poisoned and not reopened.poisoned
    finally:
        iterator.close()
        reopened.close()


def test_history_corruption_before_concurrent_close_still_poisons(opened, monkeypatch):
    store, snapshot = opened
    append_run(store, snapshot)
    iterator = iter(store.records[SID])
    next(iterator)
    entered, release, closed = threading.Event(), threading.Event(), threading.Event()
    errors = []

    def fail_read(*args):
        entered.set()
        assert release.wait(3)
        raise OSError("injected read corruption")

    def read():
        try:
            next(iterator)
        except BaseException as exc:
            errors.append(exc)

    def close():
        store.close()
        closed.set()

    monkeypatch.setattr(store, "_record_bytes", fail_read)
    reader = threading.Thread(target=read)
    closer = threading.Thread(target=close)
    reader.start()
    try:
        assert entered.wait(3)
        closer.start()
        assert not closed.wait(0.05)
    finally:
        release.set()
        reader.join(3)
        if closer.ident is not None:
            closer.join(3)
        iterator.close()
    assert closed.is_set() and len(errors) == 1
    assert isinstance(errors[0], AgentFault) and errors[0].code == "storage_unavailable"
    assert store.poisoned


def test_title_and_control_changes_do_not_serialize_or_hash_unchanged_bodies(opened, monkeypatch):
    store, snapshot = opened
    snapshot = append_run(store, snapshot, large=True)
    serialized, hashed = [], []
    json_bytes, sha256 = codec.json_bytes, hashlib.sha256

    def serialize(value):
        raw = json_bytes(value)
        serialized.append(raw)
        return raw

    def digest(raw=b""):
        hashed.append(bytes(raw))
        return sha256(raw)

    with monkeypatch.context() as patch:
        patch.setattr(codec, "json_bytes", serialize)
        patch.setattr(codec.hashlib, "sha256", digest)
        patch.setattr(
            AgentPlan, "model_dump", Mock(side_effect=AssertionError("unchanged plan dump"))
        )
        patch.setattr(
            StepResult, "model_dump", Mock(side_effect=AssertionError("unchanged result dump"))
        )
        patch.setattr(store, "_read_payload", Mock(side_effect=AssertionError("stable body read")))
        for n in range(5):
            snapshot.title = f"new title {n}"
            snapshot.runs[0].status = "completed" if n % 2 else "rejected"
            snapshot = store.append(snapshot, "session.updated", f"title-{n}").session
            assert store.get(SID) == snapshot
    assert serialized and max(map(len, serialized)) < 1000
    assert sum(map(len, serialized)) < 5000
    # Commit fences hash the physical journal, including earlier ref/control
    # records. They must not serialize/hash the separate unchanged large bodies.
    physical_records = set(journal(store).read_bytes().splitlines(keepends=True))
    assert hashed and all(raw in physical_records for raw in hashed)
    assert max(map(len, hashed)) < 5000


def test_changed_metadata_reverifies_body_or_prefix_once_then_uses_metadata(opened, monkeypatch):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    snapshot.runs[0].plan.revision = 2
    snapshot = store.append(
        snapshot, "session.updated", "revision", audit={"only": "audit"}
    ).session
    old_ref = entries(store)[1]["runs"][0]["plan"]
    path = store.root / SID / "payloads" / (old_ref["id"] + ".json")
    for selected in (path, journal(store)):
        info = selected.stat()
        os.utime(selected, ns=(info.st_atime_ns, info.st_mtime_ns + 1000000))
    read = Mock(wraps=store._read_payload)
    prefix = Mock(wraps=store._record_bytes)
    monkeypatch.setattr(store, "_read_payload", read)
    monkeypatch.setattr(store, "_record_bytes", prefix)
    assert store.get(SID) == snapshot
    assert read.call_count == 1 and prefix.call_count == 3
    read.reset_mock()
    prefix.reset_mock()
    assert store.get(SID) == snapshot
    prefix.assert_not_called()  # stable cached reads keep the metadata fast path
    store.append(snapshot, "session.updated", "title")
    read.assert_not_called()
    assert prefix.call_count == 4  # final fence binds the prefix plus the new record


async def test_service_patch_detects_same_length_prefix_corruption_during_journal_fsync(
    opened, monkeypatch
):
    store, snapshot = opened
    owner = AgentService(store)
    path = journal(store)
    original = path.read_bytes()
    title_offset = original.index(b"New Agent session")
    journal_info = path.stat()
    old = store._committed[SID]
    old_stamp = store._verified[SID].journal
    real_sync = os.fsync
    corrupted = False

    def corrupt_then_sync(fd):
        nonlocal corrupted
        info = os.fstat(fd)
        if not corrupted and (info.st_dev, info.st_ino) == (
            journal_info.st_dev,
            journal_info.st_ino,
        ):
            corrupted = True
            # The patch changes only effort, so replay would inherit this title
            # while the cached baseline would otherwise keep the original one.
            with path.open("r+b") as handle:
                handle.seek(title_offset)
                handle.write(b"Bad Agent session")
                handle.flush()
        real_sync(fd)

    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, "fsync", corrupt_then_sync)
            with pytest.raises(AgentFault) as failure:
                await owner.patch(SID, PatchSession(request_id="effort", reasoning_effort="medium"))
        assert corrupted and failure.value.code == "storage_unavailable"
        assert failure.value.status == 503 and store.poisoned
        assert store._committed[SID] is old and len(store.records[SID]) == 1
        assert store.claim_digest(SID, "effort") is None
        assert store._verified[SID].journal == old_stamp
        physical = entries(store)
        assert len(physical) == 2
        assert physical[0]["session"]["title"] == "Bad Agent session"
        assert physical[1]["session"]["reasoning_effort"] == "medium"
        assert "title" not in physical[1]["session"]
        for _ in range(2):
            with pytest.raises(AgentFault) as repeated:
                await owner.get(SID)
            assert repeated.value.code == "storage_unavailable"
        with pytest.raises(AgentFault):
            _ = store.records[SID][0]
    finally:
        await owner.close()


def test_final_fence_rechecks_payload_metadata_after_durability_io(opened, monkeypatch):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    old = store._committed[SID]
    request = old.projection.runs[0].request
    path = store.root / SID / "payloads" / (request.id + ".json")
    journal_info = journal(store).stat()
    real_sync = os.fsync
    corrupted = False

    def corrupt_then_sync(fd):
        nonlocal corrupted
        info = os.fstat(fd)
        if not corrupted and (info.st_dev, info.st_ino) == (
            journal_info.st_dev,
            journal_info.st_ino,
        ):
            corrupted = True
            path.write_bytes(b"X" * request.bytes)
        real_sync(fd)

    snapshot.title = "Update after verified payload read"
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", corrupt_then_sync)
        with pytest.raises(AgentFault) as failure:
            store.append(
                snapshot,
                "session.updated",
                "update",
                claim=Claim(request_id="update", digest="digest"),
            )
    assert corrupted and failure.value.code == "storage_unavailable"
    assert store.poisoned and store._committed[SID] is old
    assert len(store.records[SID]) == 2 and store.claim_digest(SID, "update") is None
    with pytest.raises(AgentFault):
        store.get(SID)
    store.close()
    with pytest.raises(AgentFault):
        SessionStore(store.root).start()


@pytest.mark.parametrize("failure_source", ["history", "assignment"])
@pytest.mark.parametrize("boundary", ["fsync", "publication"])
def test_observed_failure_prevents_durable_writer_publication_and_recovers_unknown_outcome(
    opened, monkeypatch, failure_source, boundary
):
    store, snapshot = opened
    snapshot = append_run(store, snapshot)
    iterator = iter(store.records[SID])
    assert next(iterator).event.sequence == 1
    old, old_verified = store._committed[SID], store._verified[SID]
    entered, release, reader_done = threading.Event(), threading.Event(), threading.Event()
    writer_errors, reader_errors, acknowledgements = [], [], []
    journal_info = journal(store).stat()
    real_sync, real_pread, publish = os.fsync, os.pread, store._publish
    snapshot.title = "Durable but initially unacknowledged"

    def blocked_sync(fd):
        info = os.fstat(fd)
        if boundary == "fsync" and (info.st_dev, info.st_ino) == (
            journal_info.st_dev,
            journal_info.st_ino,
        ):
            entered.set()
            assert release.wait(5), "Test did not release the fsync worker"
        real_sync(fd)

    def blocked_publish(*args, **kwargs):
        if boundary == "publication":
            entered.set()
            assert release.wait(5), "Test did not release the publication worker"
        return publish(*args, **kwargs)

    def failed_reader_pread(fd, length, offset):
        if threading.current_thread() is reader and offset == old.locations[1].offset:
            raise OSError("injected diagnostic read failure")
        return real_pread(fd, length, offset)

    def write():
        try:
            acknowledgements.append(
                store.append(
                    snapshot,
                    "planner.attempt",
                    "uncertain",
                    run_id="run",
                    operation_id="operation",
                    claim=Claim(request_id="uncertain", digest="digest"),
                )
            )
        except BaseException as exc:
            writer_errors.append(exc)

    def fail():
        try:
            if failure_source == "history":
                next(iterator)
                reader_errors.append(AssertionError("Expected a diagnostic read failure"))
            else:
                # Same direct assignment used by AgentService's I/O failure path.
                store.poisoned = True
        except BaseException as exc:
            reader_errors.append(exc)
        finally:
            reader_done.set()

    writer, reader = threading.Thread(target=write), threading.Thread(target=fail)
    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", blocked_sync)
        patch.setattr(os, "pread", failed_reader_pread)
        patch.setattr(store, "_publish", blocked_publish)
        writer.start()
        try:
            assert entered.wait(3)
            reader.start()
            assert reader_done.wait(1), "Failure signalling blocked behind durable writer I/O"
            assert store.poisoned and writer.is_alive()
            assert store._committed[SID] is old
            assert store.claim_digest(SID, "uncertain") is None
        finally:
            release.set()
            writer.join(5)
            if reader.ident is not None:
                reader.join(5)
            if not reader.is_alive():
                iterator.close()
    assert not writer.is_alive() and not reader.is_alive()
    if failure_source == "history":
        assert len(reader_errors) == 1 and isinstance(reader_errors[0], AgentFault)
        assert reader_errors[0].code == "storage_unavailable"
    else:
        assert not reader_errors
    assert not acknowledgements
    assert len(writer_errors) == 1 and isinstance(writer_errors[0], AgentFault)
    assert writer_errors[0].code == "storage_unavailable" and writer_errors[0].status == 503
    assert store._committed[SID] is old and store._verified[SID] is old_verified
    assert len(store.records[SID]) == 2 and store.claim_digest(SID, "uncertain") is None
    assert store.attempt_count(SID, "run") == 0
    assert not store.operation_known(SID, "run", "operation")
    assert len(entries(store)) == 3  # durability can precede the rejected acknowledgement
    with pytest.raises(AgentFault):
        store.get(SID)
    store.close()
    recovered = SessionStore(store.root)
    recovered.start()
    try:
        current = recovered.get(SID)
        assert current.sequence == 3 and current.title == snapshot.title
        assert recovered.claim_digest(SID, "uncertain") == "digest"
        assert recovered.attempt_count(SID, "run") == 1
        assert recovered.operation_known(SID, "run", "operation")
        assert not recovered.claim(SID, "uncertain", "digest")
        assert recovered.used == physical_bytes(recovered)
    finally:
        recovered.close()
