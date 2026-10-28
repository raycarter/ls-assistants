import asyncio
import threading

import pytest

import app.agent_routes as agent_routes
from app.agent_files import AgentFault
from app.agent_routes import AgentStreamingResponse
from app.agent_schemas import StartRun
from tests.test_agent import BASE, CREATE, approval, planned
from tests.test_agent import agent_setup as agent_setup_fixture

agent_setup = agent_setup_fixture


@pytest.mark.parametrize("action", ["plan", "execute", "revise"])
async def test_response_construction_failure_releases_route_ownership(
    agent_setup, monkeypatch, action
):
    async with agent_setup.opened() as (client, owner, _):
        if action == "plan":
            sid = (await client.post(BASE, json=CREATE)).json()["id"]
            url = f"{BASE}/{sid}/runs"
            body = {"request_id": "run", "content": "Read"}
        else:
            sid, run, _ = await planned(client)
            url = f"{BASE}/{sid}/runs/{run['id']}/{action}"
            body = approval(run, action)
            if action == "revise":
                body.update(kind="feedback", content="Shorter")

        captured = []

        def fail_response(service, op):
            assert service is owner and owner.streams[op.id] == sid
            assert not op.gate.is_set()
            captured.append(op)
            raise RuntimeError("injected response construction failure")

        monkeypatch.setattr(agent_routes, "AgentStreamingResponse", fail_response)
        with pytest.raises(RuntimeError, match="injected response construction failure"):
            await client.post(url, json=body)
        (op,) = captured
        assert op.stop.is_set() and op.gate.is_set()
        assert op.id not in owner.streams
        await asyncio.wait_for(op.task, 3)
        assert op.id not in owner.active
        assert (await owner.get(sid)).runs[-1].status == "interrupted"
        assert (await client.delete(f"{BASE}/{sid}")).status_code == 204


@pytest.mark.parametrize("phase", ["commit", "registered"])
async def test_route_cancel_during_admission_settles_and_releases_stream(
    agent_setup, monkeypatch, phase
):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        entered, release = asyncio.Event(), asyncio.Event()
        thread_entered, thread_release = threading.Event(), threading.Event()
        if phase == "commit":
            original_append = owner.store.append

            def blocked_append(snapshot, kind, *args, **kwargs):
                if kind == "run.planning":
                    thread_entered.set()
                    assert thread_release.wait(3)
                return original_append(snapshot, kind, *args, **kwargs)

            monkeypatch.setattr(owner.store, "append", blocked_append)
        else:
            original_admit = owner._admit

            async def blocked_admit(*args, **kwargs):
                op = await original_admit(*args, **kwargs)
                entered.set()
                await release.wait()
                return op

            monkeypatch.setattr(owner, "_admit", blocked_admit)

        request = asyncio.create_task(
            client.post(f"{BASE}/{sid}/runs", json={"request_id": "run", "content": "Read"})
        )
        try:
            if phase == "commit":
                assert await asyncio.to_thread(thread_entered.wait, 3)
            else:
                await asyncio.wait_for(entered.wait(), 3)
                assert len(owner.streams) == len(owner.active) == 1
            (admission,) = owner.admissions
            request.cancel()
            with pytest.raises(asyncio.CancelledError):
                await request
        finally:
            release.set()
            thread_release.set()
        op = await asyncio.wait_for(admission, 3)
        assert op.stop.is_set() and op.gate.is_set()
        await asyncio.wait_for(op.task, 3)
        assert not owner.active and not owner.streams
        assert (await owner.get(sid)).runs[-1].status == "interrupted"
        assert (await client.delete(f"{BASE}/{sid}")).status_code == 204


async def test_delete_complete_archived_and_restart(agent_setup, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("untouched")
    async with agent_setup.opened() as (client, owner, _):
        sid, _, _ = await planned(client)
        other = (await client.post(BASE, json={**CREATE, "request_id": "other"})).json()["id"]
        snapshot = await owner.get(sid)
        snapshot.archived = True
        await owner.io(owner.store.append, snapshot, "session.archived", "legacy")
        folder = agent_setup.root / sid
        assert list((folder / "payloads").iterdir())
        (folder / "artifacts").mkdir(exist_ok=True)
        (folder / "artifacts" / ("a" * 32 + ".txt")).write_text("orphan")
        (folder / "link").symlink_to(outside, target_is_directory=True)
        response = await client.post(f"{BASE}/{sid}/archive", json={"request_id": "x"})
        assert response.status_code == 404
        response = await client.delete(f"{BASE}/{sid}")
        assert response.status_code == 204 and response.content == b""
        assert not folder.exists() and (outside / "keep").read_text() == "untouched"
        assert (await client.get(f"{BASE}/{sid}")).status_code == 404
        assert (await client.get(f"{BASE}/{other}")).status_code == 200
        assert (await client.delete(f"{BASE}/{sid}")).json() == {
            "error": {"code": "session_not_found", "message": "Agent session not found."}
        }
        assert (await client.delete(f"{BASE}/bad-id")).status_code == 422
        replacement = await client.post(BASE, json=CREATE)
        assert replacement.status_code == 200 and replacement.json()["id"] != sid
    async with agent_setup.opened() as (client, _, __):
        assert (await client.get(f"{BASE}/{sid}")).status_code == 404
        assert (await client.get(f"{BASE}/{other}")).status_code == 200


async def test_delete_failure_recovers_marker_on_restart(agent_setup, monkeypatch):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        original = owner.store._remove_tree

        def fail(*_):
            raise OSError("injected deletion failure")

        monkeypatch.setattr(owner.store, "_remove_tree", fail)
        response = await client.delete(f"{BASE}/{sid}")
        assert response.status_code == 503
        assert response.json()["error"] == {
            "code": "storage_unavailable",
            "message": "Agent recording failed; no further work is allowed.",
        }
        assert (agent_setup.root / (".deleted-" + sid)).exists()
        monkeypatch.setattr(owner.store, "_remove_tree", original)
    async with agent_setup.opened() as (client, _, __):
        assert (await client.get(f"{BASE}/{sid}")).status_code == 404
        assert not (agent_setup.root / (".deleted-" + sid)).exists()


async def test_replaced_session_symlink_fails_closed(agent_setup, tmp_path):
    outside = tmp_path / "target"
    outside.mkdir()
    (outside / "keep").write_text("untouched")
    async with agent_setup.opened() as (client, _, __):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        folder = agent_setup.root / sid
        folder.rename(agent_setup.root / "moved")
        folder.symlink_to(outside, target_is_directory=True)
        response = await client.delete(f"{BASE}/{sid}")
        assert response.status_code == 503
        assert (outside / "keep").read_text() == "untouched"


async def test_cancelled_delete_serializes_admission_until_removal(agent_setup, monkeypatch):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        entered, release = threading.Event(), threading.Event()
        original = owner.store._remove_tree

        def blocked(*args):
            entered.set()
            assert release.wait(3)
            original(*args)

        monkeypatch.setattr(owner.store, "_remove_tree", blocked)
        deletion = asyncio.create_task(owner.delete(sid))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            deletion.cancel()
            admission = asyncio.create_task(
                owner.prepare(sid, StartRun(request_id="run", content="go"))
            )
            await asyncio.sleep(0.02)
            assert not admission.done()
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await deletion
        with pytest.raises(AgentFault) as exc:
            await admission
        assert exc.value.code == "session_not_found"
        assert not (agent_setup.root / sid).exists()


async def test_paused_history_blocks_delete_and_can_resume(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        sid, _, _ = await planned(client)
        history = iter(owner.store.records[sid])
        assert next(history).event.type == "session.created"
        response = await client.delete(f"{BASE}/{sid}")
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "session_in_use",
            "message": "Wait for session readers and streams to close.",
        }
        assert "run.awaiting_approval" in [record.event.type for record in history]
        assert not owner.store.poisoned
        assert (await client.delete(f"{BASE}/{sid}")).status_code == 204
        assert (await client.get(BASE)).status_code == 200


async def test_unstarted_history_after_delete_does_not_poison_store(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        history = iter(owner.store.records[sid])
        assert (await client.delete(f"{BASE}/{sid}")).status_code == 204
        with pytest.raises(AgentFault) as exc:
            next(history)
        assert exc.value.code == "session_not_found"
        assert not owner.store.poisoned


async def test_history_registration_races_delete(agent_setup, monkeypatch):
    async with agent_setup.opened() as (client, owner, _):
        sid, _, _ = await planned(client)
        history = iter(owner.store.records[sid])
        entered, release = threading.Event(), threading.Event()
        original = owner.store._verify_journal

        def blocked(*args, **kwargs):
            entered.set()
            assert release.wait(3)
            return original(*args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(owner.store, "_verify_journal", blocked)
            reader = asyncio.create_task(asyncio.to_thread(next, history))
            try:
                assert await asyncio.to_thread(entered.wait, 3)
                deletion = asyncio.create_task(client.delete(f"{BASE}/{sid}"))
                await asyncio.sleep(0.02)
                assert not deletion.done()
            finally:
                release.set()
            assert (await reader).event.type == "session.created"
            assert (await deletion).status_code == 409
        history.close()
        assert (await client.delete(f"{BASE}/{sid}")).status_code == 204


@pytest.mark.parametrize("pause_at", ["start", "body"])
async def test_paused_response_blocks_delete_until_send_or_cancellation(
    agent_setup, pause_at, monkeypatch
):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        op = await owner.prepare(
            sid, StartRun(request_id="plan", content="Read"), response_stream=True
        )
        op.gate.set()
        await op.task
        assert op.id not in owner.active
        entered, release = asyncio.Event(), asyncio.Event()
        sent = []
        pages = []
        original = owner.store.public_events

        def record_page(*args, **kwargs):
            page = original(*args, **kwargs)
            pages.append(len(page["events"]))
            return page

        monkeypatch.setattr(owner.store, "public_events", record_page)

        async def send(message):
            is_pause = (pause_at == "start" and message["type"] == "http.response.start") or (
                pause_at == "body"
                and message["type"] == "http.response.body"
                and bool(message.get("body"))
                and not entered.is_set()
            )
            if is_pause:
                entered.set()
                await release.wait()
            sent.append(message)

        async def receive():
            await asyncio.Event().wait()

        response = AgentStreamingResponse(owner, op)
        task = asyncio.create_task(
            response({"type": "http", "asgi": {"spec_version": "2.4"}}, receive, send)
        )
        try:
            await asyncio.wait_for(entered.wait(), 3)
            result = await client.delete(f"{BASE}/{sid}")
            assert result.status_code == 409
            assert result.json()["error"]["code"] == "session_in_use"
            assert not pages if pause_at == "start" else pages[0] >= 2
            if pause_at == "start":
                task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await task
            else:
                release.set()
                await asyncio.wait_for(task, 3)
                bodies = [m["body"] for m in sent if m["type"] == "http.response.body"]
                assert sum(bool(body) for body in bodies) >= 2
            assert not owner.streams
            assert (await client.delete(f"{BASE}/{sid}")).status_code == 204
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
