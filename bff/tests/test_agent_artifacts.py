"""Durable evidence integrity and descriptor-relative storage/recovery boundaries."""

import json
import os
from uuid import UUID

import pytest

import app.sessions as sessions_module
from app.agent_files import AgentFault
from app.agent_schemas import CreateSession
from app.sessions import SessionStore, StoreLimits
from tests.test_agent import BASE, CREATE, approval, guard_session_syscalls
from tests.test_agent_fetch import (
    TEXT,
    document,
    executed_fetch,
    fetch_proposal,
    planned_fetch,
)
from tests.test_agent_fetch import (
    fetch_setup as fetch_setup,  # explicit pytest fixture re-export
)


async def test_restart_keeps_evidence_and_page_continuation_without_fetch(fetch_setup):
    fetch_setup.planner.plan.return_value = fetch_proposal(offset=16001, limit=16000)
    async with fetch_setup.opened() as (client, owner, _):
        sid, run, result, _ = await executed_fetch(client)
        aid = result["fetch"]["artifact_id"]
        assert result["content"] == TEXT[16001:32001]
        before = (await client.get(f"{BASE}/{sid}")).json()
        used, artifact_bytes = owner.store.used, owner.store.artifact_bytes
    async with fetch_setup.opened() as (client, owner, _):
        assert (await client.get(f"{BASE}/{sid}")).json() == before
        assert owner.store.used == used and owner.store.artifact_bytes == artifact_bytes
        response = await client.get(f"{BASE}/{sid}/artifacts/{aid}?offset=32001&limit=16000")
        assert response.status_code == 200 and response.json()["content"] == TEXT[32001:48001]
        assert (await client.get(f"{BASE}/{sid}/events")).status_code == 200
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert response.status_code == 409
        fetch_setup.fetcher.fetch.assert_awaited_once()


async def test_artifact_endpoint_only_exposes_committed_session_evidence(fetch_setup):
    async with fetch_setup.opened() as (client, owner, _):
        sid, _, result, _ = await executed_fetch(client)
        aid = result["fetch"]["artifact_id"]
        other = (await client.post(BASE, json={**CREATE, "request_id": "other"})).json()["id"]
        orphan, _ = owner.store.write_artifact(sid, document("Uncommitted evidence"))
        for selected_sid, selected_id in (
            (other, aid),
            (sid, orphan.artifact_id),
            (sid, "f" * 32),
        ):
            response = await client.get(f"{BASE}/{selected_sid}/artifacts/{selected_id}")
            assert response.status_code == 404
            assert response.json()["error"]["message"] == "Agent artifact not found."
        for invalid in ("invalid", "A" * 32, "f" * 31, "events.jsonl", "..", "id.txt"):
            with pytest.raises(AgentFault) as failure:
                owner.store.read_artifact(sid, invalid)
            assert failure.value.status == 422
        for invalid in ("invalid", "A" * 32, "f" * 31, "events.jsonl", "id.txt"):
            response = await client.get(f"{BASE}/{sid}/artifacts/{invalid}")
            assert response.status_code == 422
            assert response.json()["error"]["message"] == "Invalid Agent artifact ID."
        for query in ("offset=-1", "offset=bad", "limit=0", "limit=16001", "limit=1.5"):
            assert (await client.get(f"{BASE}/{sid}/artifacts/{aid}?{query}")).status_code == 422
        assert (
            await client.get(f"{BASE}/{sid}/artifacts/{aid}?offset={len(TEXT) + 1}")
        ).status_code == 422
        empty = (await client.get(f"{BASE}/{sid}/artifacts/{aid}?offset={len(TEXT)}")).json()
        assert empty["content"] == "" and empty["next_offset"] is None
        fetch_setup.fetcher.fetch.assert_awaited_once()
    async with fetch_setup.opened() as (client, owner, _):
        assert (await client.get(f"{BASE}/{sid}/artifacts/{orphan.artifact_id}")).status_code == 404
        assert (await client.get(f"{BASE}/{sid}/artifacts/{aid}")).status_code == 200


@pytest.mark.parametrize(
    "damage", ["missing", "same_length", "truncated", "invalid_utf8", "symlink"]
)
async def test_missing_or_corrupt_committed_evidence_blocks_success_live_and_restart(
    fetch_setup, tmp_path, damage
):
    async with fetch_setup.opened() as (client, owner, _):
        sid, _, result, _ = await executed_fetch(client)
        aid = result["fetch"]["artifact_id"]
        path = owner.store.root / sid / "artifacts" / (aid + ".txt")
        raw = path.read_bytes()
        if damage == "missing":
            path.unlink()
        elif damage == "same_length":
            path.write_bytes(b"X" + raw[1:])
        elif damage == "truncated":
            path.write_bytes(raw[:-1])
        elif damage == "invalid_utf8":
            path.write_bytes(b"\xff" + raw[1:])
        else:
            outside = tmp_path / "outside"
            outside.write_bytes(raw)
            path.unlink()
            path.symlink_to(outside)
        for suffix in (f"/artifacts/{aid}", "", "/events"):
            response = await client.get(f"{BASE}/{sid}{suffix}")
            assert response.status_code == 503
            assert "outside" not in response.text and "Public evidence" not in response.text
        assert owner.store.poisoned
    recovered = SessionStore(fetch_setup.root)
    with pytest.raises(AgentFault) as failure:
        recovered.start()
    assert failure.value.code == "storage_unavailable"
    assert recovered.fd is None
    fetch_setup.fetcher.fetch.assert_awaited_once()


@pytest.mark.parametrize(
    "unsafe", ["directory_symlink", "file_symlink", "hardlink", "fifo", "name"]
)
def test_recovery_rejects_unsafe_orphan_artifacts(tmp_path, unsafe):
    root = tmp_path / "journal"
    store = SessionStore(root)
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    page, _ = store.write_artifact(snapshot.id, document("uncommitted"))
    store.close()
    directory = root / snapshot.id / "artifacts"
    path = directory / (page.artifact_id + ".txt")
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    if unsafe == "directory_symlink":
        directory.rename(tmp_path / "saved")
        directory.symlink_to(tmp_path / "saved", target_is_directory=True)
    else:
        path.unlink()
        if unsafe == "file_symlink":
            path.symlink_to(outside)
        elif unsafe == "hardlink":
            os.link(outside, path)
        elif unsafe == "fifo":
            os.mkfifo(path)
        else:
            (directory / "unexpected.txt").write_text("not evidence")
    with pytest.raises(AgentFault) as failure:
        SessionStore(root).start()
    assert failure.value.code == "storage_unavailable"
    assert outside.read_text() == "untouched"


def test_exclusive_creation_cannot_overwrite_or_follow_existing_artifact(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "journal")
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    page, _ = store.write_artifact(snapshot.id, document("original evidence"))
    monkeypatch.setattr(sessions_module, "uuid4", lambda: UUID(hex=page.artifact_id))
    with pytest.raises(AgentFault) as failure:
        store.write_artifact(snapshot.id, document("replacement"))
    assert failure.value.code == "storage_unavailable"
    path = store.root / snapshot.id / "artifacts" / (page.artifact_id + ".txt")
    assert path.read_text() == "original evidence"
    store.close()


async def test_uncommitted_artifact_after_physical_failure_recovers_interrupted_not_reexecuted(
    fetch_setup, monkeypatch
):
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        original = owner.store.append

        def failed(snapshot, kind, *args, **kwargs):
            if kind == "tool.result":
                raise OSError("physical storage fault before append")
            return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", failed)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert "tool.result" not in response.text and "run.completed" not in response.text
        files = list((owner.store.root / sid / "artifacts").iterdir())
        assert len(files) == 1 and files[0].read_text() == TEXT
        aid = files[0].stem
    async with fetch_setup.opened() as (client, owner, _):
        recovered = (await client.get(f"{BASE}/{sid}")).json()["runs"][0]
        assert recovered["status"] == "interrupted"
        assert recovered["error"]["code"] == "outcome_unknown"
        assert recovered["results"][0]["status"] == "failed"
        assert (await client.get(f"{BASE}/{sid}/artifacts/{aid}")).status_code == 404
        fetch_setup.fetcher.fetch.assert_awaited_once()


def test_artifact_body_not_subject_to_existing_journal_disk_budget_and_legacy_stays_valid(tmp_path):
    root = tmp_path / "journal"
    limits = StoreLimits(disk_bytes=2000)
    store = SessionStore(root, limits=limits)
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    used = store.used
    page, _ = store.write_artifact(snapshot.id, document(TEXT))
    assert store.used == used and store.artifact_bytes > limits.disk_bytes
    # Legacy events have no fetch field at all and no artifact references.
    store.close()
    journal = root / snapshot.id / "events.jsonl"
    record = json.loads(journal.read_text())
    assert record["event"]["session"]["runs"] == []
    recovered = SessionStore(root, limits=limits)
    recovered.start()
    assert recovered.get(snapshot.id).runs == []
    assert recovered.artifact_bytes == len(TEXT.encode())
    with pytest.raises(AgentFault) as failure:
        recovered.read_artifact(snapshot.id, page.artifact_id)
    assert failure.value.status == 404
    recovered.close()


def test_artifact_operations_require_live_descriptor_before_any_relative_syscall(
    tmp_path, monkeypatch
):
    store = SessionStore(tmp_path / "journal")
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    page, receipt = store.write_artifact(snapshot.id, document("orphan"))
    store.close()
    unsafe, writes = guard_session_syscalls(monkeypatch)
    for call in (
        lambda: store.write_artifact(snapshot.id, document("late")),
        lambda: store.read_artifact(snapshot.id, page.artifact_id),
    ):
        with pytest.raises(AgentFault) as failure:
            call()
        assert failure.value.code == "storage_unavailable"
    assert not unsafe and not writes
