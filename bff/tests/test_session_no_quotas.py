"""Former history boundaries succeed; actual device failures forbid acknowledgement."""

import errno
import hashlib
import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from app.agent import AgentService
from app.agent_files import AgentFault, FilePolicy
from app.agent_schemas import Claim, PlanIdentity, Proposal, StartRun
from app.sessions import SessionStore
from tests.test_session_journal import CREATE, NOW, SID, physical_bytes, run
from tests.test_session_migration import legacy_records, proposal_json, write_legacy


def test_more_than_100_sessions_and_500_events_survive_restart_without_identity_loss(tmp_path):
    root = tmp_path / "journal"
    ids = iter(f"20261020T123456123456Z-{n:04d}" for n in range(105))
    store = SessionStore(root, clock=lambda: NOW, id_factory=lambda: next(ids))
    store.start()
    try:
        created = [
            store.create(CREATE.model_copy(update={"request_id": f"create-{n}"}))
            for n in range(105)
        ]
        sid = created[0].id
        snapshot = created[0]
        for n in range(510):
            snapshot.title = f"event-{n}"
            snapshot = store.append(
                snapshot,
                "session.updated",
                f"title-{n}",
                claim=Claim(request_id=f"title-{n}", digest=f"digest-{n}"),
            ).session
        assert snapshot.sequence == 511
    finally:
        store.close()
    recovered = SessionStore(root, clock=lambda: NOW)
    recovered.start()
    try:
        assert len(recovered.records) == 105 and len(recovered.records[sid]) == 511
        # Full historical compatibility retains every old state, including after old ceilings.
        assert [r.event.sequence for r in recovered.records[sid]] == list(range(1, 512))
        assert recovered.records[sid][500].event.session.title == "event-499"
        assert recovered.claim_digest(sid, "title-509") == "digest-509"
        retry = recovered.create(CREATE.model_copy(update={"request_id": "create-104"}))
        assert retry.id == created[-1].id
        seen, cursor = [], None
        while True:
            page = recovered.public_list(False, 25, cursor)
            assert page["total"] == 105
            seen.extend(s["id"] for s in page["sessions"])
            cursor = page["next_cursor"]
            if cursor is None:
                break
        assert len(seen) == len(set(seen)) == 105
    finally:
        recovered.close()


def test_more_than_8_runs_revisions_and_512k_payloads_keep_all_old_material(tmp_path):
    root = tmp_path / "journal"
    store = SessionStore(root, clock=lambda: NOW, id_factory=lambda: SID)
    store.start()
    snapshot = store.create(CREATE)
    try:
        snapshot.runs = [run(f"run-{n}") for n in range(12)]
        snapshot.runs[0].request = "界" * 180000
        snapshot.runs[0].results[0].content = "large result " * 50000
        snapshot.runs[0].plan.objective = "large plan " * 50000
        snapshot = store.append(snapshot, "run.completed", "large", run_id="run-0").session
        for revision in range(2, 12):
            snapshot.runs[-1].plan.revision = revision
            snapshot.runs[-1].plan.digest = hashlib.sha256(str(revision).encode()).hexdigest()
            snapshot = store.append(
                snapshot, "run.awaiting_approval", f"revision-{revision}", run_id="run-11"
            ).session
        expected = snapshot.model_dump()
        refs = store._committed[SID].payloads
        assert sum(ref.bytes > 512_000 for ref in refs.values()) >= 3
        used = store.used
    finally:
        store.close()
    recovered = SessionStore(root, clock=lambda: NOW)
    recovered.start()
    try:
        assert recovered.get(SID).model_dump() == expected
        assert recovered.used == used == physical_bytes(recovered)
        records = list(recovered.records[SID])
        assert len(records) == 12 and len(records[-1].event.session.runs) == 12
        assert [r.event.session.runs[-1].plan.revision for r in records[1:]] == list(range(1, 12))
        assert records[1].event.session.runs[0].request == "界" * 180000
        assert recovered.public_run(SID, "run-0")["plan"]["objective"] == "large plan " * 50000
        assert recovered.public_events(SID, 0)["events"][-1]["plan"]["revision"] == 11
    finally:
        recovered.close()


def test_large_complete_legacy_line_has_no_recovery_line_bound(tmp_path):
    root = tmp_path / "journal"
    legacy = legacy_records()
    legacy[-1]["event"]["session"]["runs"][0]["plan"]["objective"] = "界" * 180000
    raw = write_legacy(root, legacy)
    sid = legacy[0]["event"]["session_id"]
    assert len(raw.splitlines()[-1]) > 512_000
    store = SessionStore(root)
    store.start()
    try:
        assert store.get(sid).runs[0].plan.objective == "界" * 180000
        assert store.public_events(sid, 4)["events"][0]["plan"]["objective"] == "界" * 180000
        assert (root / sid / "events.jsonl").read_bytes() == raw
    finally:
        store.close()


def test_diagnostic_accounting_above_former_64mb_is_not_an_admission_limit(tmp_path, monkeypatch):
    """Simulated accounting, not a claim to have written a 64MB physical fixture."""
    root = tmp_path / "journal"
    store = SessionStore(root, clock=lambda: NOW, id_factory=lambda: SID)
    store.start()
    try:
        snapshot = store.create(CREATE)
        snapshot.runs = [run()]
        store.used += 64_000_001
        snapshot = store.append(snapshot, "run.completed", "large-accounting", run_id="run").session
        assert store.used > 64_000_001
        assert store.public_session(SID)["run_count"] == 1
    finally:
        store.close()
    real_recover = SessionStore._recover_payload_files

    def simulated_recovery_accounting(self, directory):
        real_recover(self, directory)
        self.used += 64_000_001

    monkeypatch.setattr(SessionStore, "_recover_payload_files", simulated_recovery_accounting)
    recovered = SessionStore(root)
    recovered.start()
    try:
        assert recovered.used > 64_000_001
        assert recovered.get(SID) == snapshot
    finally:
        recovered.close()


def test_many_safe_orphans_are_retained_counted_and_never_public(tmp_path):
    root = tmp_path / "journal"
    root.mkdir()
    for n in range(210):
        folder = root / f".orphan-20261020T123456123456Z-{n:04d}"
        folder.mkdir()
        bodies = folder / "payloads"
        bodies.mkdir()
        # Arbitrary safe file bytes, not necessarily complete JSON or a known work schema.
        (bodies / ("a" * 32 + ".json")).write_bytes(b"not-json")
    for _ in range(2):
        store = SessionStore(root)
        store.start()
        try:
            assert not store.records and store.used == 210 * len(b"not-json")
            assert store.public_list(True) == {
                "projection_version": 2,
                "sessions": [],
                "next_cursor": None,
                "total": 0,
            }
            assert len(list(root.iterdir())) == 211  # orphan directories plus the lease
            with pytest.raises(AgentFault) as failure:
                store.public_session("20261020T123456123456Z-0000")
            assert failure.value.status == 404
        finally:
            store.close()


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EIO])
@pytest.mark.parametrize("phase", ["write", "fsync"])
async def test_actual_filesystem_errors_prevent_ack_claim_and_planner_dispatch(
    tmp_path,
    monkeypatch,
    error,
    phase,
):
    store = SessionStore(tmp_path / "journal", clock=lambda: NOW, id_factory=lambda: SID)
    planner = SimpleNamespace(plan=AsyncMock())
    fetcher = SimpleNamespace(policy_version="offline", fetch=AsyncMock())
    files = FilePolicy({})
    files.read = Mock()
    owner = AgentService(store, planner=planner, fetcher=fetcher, files=files, models=("offline",))
    await owner.start()
    await owner.create(CREATE)
    before = store._committed[SID]
    try:
        with monkeypatch.context() as patch:
            patch.setattr(os, phase, Mock(side_effect=OSError(error, "synthetic device failure")))
            with pytest.raises(AgentFault) as failure:
                await owner.prepare(SID, StartRun(request_id="plan", content="Read fixture"))
        assert failure.value.code == "storage_unavailable" and failure.value.status == 503
        assert not owner.active and store.poisoned
        assert store._committed[SID] is before and store.claim_digest(SID, "plan") is None
        assert before.views.session.run_count == 0
        planner.plan.assert_not_awaited()
        fetcher.fetch.assert_not_awaited()
        files.read.assert_not_called()
    finally:
        await owner.close()
    recovered = SessionStore(store.root)
    recovered.start()
    try:
        assert recovered.public_session(SID)["run_count"] == 0
        assert recovered.claim_digest(SID, "plan") is None
        assert json.loads((store.root / SID / "events.jsonl").read_bytes())["storage_version"] == 2
    finally:
        recovered.close()


@pytest.mark.parametrize("error", [errno.ENOSPC, errno.EIO])
@pytest.mark.parametrize("boundary", ["run.approved", "tool.intent"])
async def test_device_failure_before_execution_evidence_never_dispatches_tools(
    tmp_path,
    monkeypatch,
    error,
    boundary,
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("synthetic input\n")
    files = FilePolicy({"fixture": workspace})
    files.read = Mock(wraps=files.read)
    planner = SimpleNamespace(plan=AsyncMock(return_value=Proposal.model_validate(proposal_json())))
    fetcher = SimpleNamespace(policy_version="offline", fetch=AsyncMock())
    store = SessionStore(tmp_path / "journal", clock=lambda: NOW, id_factory=lambda: SID)
    owner = AgentService(store, planner=planner, files=files, fetcher=fetcher, models=("offline",))
    await owner.start()
    try:
        await owner.create(CREATE)
        planning = await owner.prepare(SID, StartRun(request_id="plan", content="Read fixture"))
        planning.gate.set()
        await planning.task
        planned = (await owner.get(SID)).runs[0]
        body = PlanIdentity(
            request_id="execute",
            plan_id=planned.plan.id,
            revision=planned.plan.revision,
            digest=planned.plan.digest,
        )
        write = os.write

        def failed_write(fd, raw):
            if ('"type":"' + boundary + '"').encode() in bytes(raw):
                raise OSError(error, "synthetic device failure")
            return write(fd, raw)

        with monkeypatch.context() as patch:
            patch.setattr(os, "write", failed_write)
            if boundary == "run.approved":
                with pytest.raises(AgentFault) as failure:
                    await owner.prepare(SID, body, rid=planned.id, action="execute")
                assert failure.value.code == "storage_unavailable"
            else:
                operation = await owner.prepare(SID, body, rid=planned.id, action="execute")
                operation.gate.set()
                await operation.task
        assert store.poisoned
        kinds = [e.envelope.type for e in store._committed[SID].views.events]
        assert boundary not in kinds and "tool.result" not in kinds and "run.completed" not in kinds
        assert (store.claim_digest(SID, "execute") is None) == (boundary == "run.approved")
        files.read.assert_not_called()
        fetcher.fetch.assert_not_awaited()
        planner.plan.assert_awaited_once()
    finally:
        await owner.close()
