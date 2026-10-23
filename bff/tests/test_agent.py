import asyncio
import errno
import json
import os
import stat
import threading
import time
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from app.agent import AgentService
from app.agent_files import AgentFault, FilePolicy
from app.agent_schemas import (
    AgentRun,
    Cancel,
    CreateSession,
    PatchSession,
    PlanIdentity,
    Proposal,
    ReadArguments,
    StartRun,
)
from app.main import create_app
from app.sessions import SESSION_ID, SessionStore


def proposal(path="note.txt"):
    return Proposal.model_validate(
        {
            "objective": "Read the fixture",
            "assumptions": ["The fixture may exist."],
            "steps": [
                {
                    "title": "Read",
                    "description": "Inspect the approved text only.",
                    "tool": "read_file",
                    "arguments": {
                        "workspace_id": "fixture",
                        "path": path,
                        "offset": 1,
                        "limit": 2,
                    },
                }
            ],
        }
    )


@pytest.fixture
def agent_setup(tmp_path, settings_factory, mock_client):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("alpha\nbeta\ngamma\n")
    root = tmp_path / "journal"
    planner = SimpleNamespace(plan=AsyncMock(return_value=proposal()))

    @asynccontextmanager
    async def opened(*, enabled=True, files=None, **timeouts):
        store = SessionStore(root)
        owner = AgentService(
            store,
            planner=planner if enabled else None,
            files=files or FilePolicy({"fixture": workspace}),
            models=("deployment-a",),
            **timeouts,
        )
        application = create_app(
            settings_factory(),
            credential_factory=Mock(return_value=SimpleNamespace(close=AsyncMock())),
            client_factory=Mock(return_value=mock_client),
            agent_service_factory=lambda settings: owner,
        )
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://test"
            ) as client:
                yield client, owner, application

    return SimpleNamespace(opened=opened, planner=planner, root=root, workspace=workspace)


CREATE = {"request_id": "create", "model": "deployment-a", "reasoning_effort": "high"}
BASE = "/api/agent/sessions"


def decode(response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    frames = response.text.strip().split("\n\n")
    events = []
    for frame in frames:
        lines = frame.splitlines()
        assert lines[1] == "event: agent"
        event = json.loads(lines[2].removeprefix("data: "))
        assert lines[0] == f"id: {event['sequence']}"
        assert event["session"]["sequence"] == event["sequence"]
        assert set(event) == {
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
        assert event["schema_version"] == 2
        assert type(event["invalidates_runs"]) is bool
        assert event["session"]["projection_version"] == 2
        assert "runs" not in event["session"]
        events.append(event)
    assert [e["sequence"] for e in events] == list(
        range(events[0]["sequence"], events[0]["sequence"] + len(events))
    )
    return events


async def planned(client):
    created = await client.post(BASE, json=CREATE)
    assert created.status_code == 200
    sid = created.json()["id"]
    frames = decode(
        await client.post(
            f"{BASE}/{sid}/runs",
            json={
                "request_id": "plan",
                "content": "Read the note",
            },
        )
    )
    run = await run_detail(client, sid, frames[-1]["run_id"])
    assert run["status"] == "awaiting_approval"
    return sid, run, frames


async def run_detail(client, sid, rid):
    response = await client.get(f"{BASE}/{sid}/runs/{rid}")
    assert response.status_code == 200, response.text
    return response.json()


def approval(run, key="execute"):
    plan = run["plan"]
    return {
        "request_id": key,
        "plan_id": plan["id"],
        "revision": plan["revision"],
        "digest": plan["digest"],
    }


async def test_mocked_http_slice_reload_exact_approval_and_audit(agent_setup):
    setup = agent_setup
    async with setup.opened() as (client, owner, _):

        async def audited_plan(context):
            current = next(iter(owner.store.records.values()))
            assert current[-1].event.type == "planner.attempt"
            assert current[-1].audit["input"]["attempt"] == 1
            return proposal()

        setup.planner.plan.side_effect = audited_plan
        reads = Mock(wraps=owner.files.read)
        owner.files.read = reads
        sid, run, frames = await planned(client)
        assert not reads.called
        records = owner.store.records[sid]
        assert [r.event.type for r in records[:4]] == [
            "session.created",
            "run.planning",
            "planner.attempt",
            "planner.result",
        ]
        assert records[2].audit["input"]["request"] == "Read the note"
        assert records[2].audit["retries"] == 0
        assert records[3].audit["proposal"]["objective"] == "Read the fixture"
        path = f"{BASE}/{sid}/runs/{run['id']}/execute"
        stale = await client.post(path, json={**approval(run), "digest": "0" * 64})
        assert stale.status_code == 409
        assert stale.headers["content-type"].startswith("application/json")
        locked = await client.patch(
            f"{BASE}/{sid}",
            json={
                "request_id": "preferences",
                "reasoning_effort": "low",
            },
        )
        assert locked.status_code == 409
        output = decode(await client.post(path, json=approval(run)))
        assert [e["type"] for e in output] == [
            "run.approved",
            "run.executing",
            "tool.intent",
            "tool.result",
            "run.completed",
        ]
        result = (await run_detail(client, sid, run["id"]))["results"][0]
        assert result["content"] == "1: alpha\n2: beta"
        assert result["truncated"] is True
        assert result["returned_lines"] == 2
        assert reads.call_count == 1
        for key in ("execute", "different-key"):
            assert (await client.post(path, json=approval(run, key))).status_code == 409
        assert reads.call_count == 1
        replay = (await client.get(f"{BASE}/{sid}/events?after=0")).json()
        assert replay["has_more"] is False
        assert replay["events"][-1]["run"]["status"] == "completed"
        assert "audit" not in json.dumps(replay)
        before = (await client.get(f"{BASE}/{sid}")).json()
    async with setup.opened() as (client, owner, _):
        assert (await client.get(f"{BASE}/{sid}")).json() == before
        assert (await client.post(BASE, json=CREATE)).json()["id"] == sid
        assert (
            await client.post(BASE, json={**CREATE, "reasoning_effort": "low"})
        ).status_code == 409
        assert len((await client.get(BASE)).json()["sessions"]) == 1
        assert (
            await client.post(BASE, json={**CREATE, "model": "deployment-b"})
        ).status_code == 422
        assert setup.planner.plan.await_count == 1
    assert stat.S_IMODE(setup.root.stat().st_mode) == 0o700
    assert stat.S_IMODE((setup.root / sid / "events.jsonl").stat().st_mode) == 0o600


async def test_default_startup_does_not_open_session_root(app_factory, monkeypatch):
    forbidden = Mock(side_effect=AssertionError("Startup must not inspect real sessions"))
    monkeypatch.setattr(SessionStore, "start", forbidden)
    application, _, _ = app_factory()
    async with application.router.lifespan_context(application):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=application), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).status_code == 200
            assert (await client.get("/api/models")).status_code == 200
            assert application.state.agent_service.planner is not None
    forbidden.assert_not_called()


async def test_planner_exception_is_sanitized_and_attempt_not_retried(agent_setup):
    agent_setup.planner.plan.side_effect = RuntimeError("Bearer secret-credential-payload")
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        response = await client.post(
            f"{BASE}/{sid}/runs",
            json={
                "request_id": "plan",
                "content": "Try the planner",
            },
        )
        frames = decode(response)
        assert frames[-1]["type"] == "run.failed"
        assert "secret-credential-payload" not in response.text
        assert agent_setup.planner.plan.await_count == 1
        assert [r.event.type for r in owner.store.records[sid]].count("planner.attempt") == 1


async def test_changed_access_policy_requires_new_run(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        sid, run, _ = await planned(client)
        owner.files = FilePolicy()
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "policy_changed"
        assert not any(r.event.type == "tool.intent" for r in owner.store.records[sid])


async def test_concurrent_creation_and_approval_have_one_durable_owner(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        created = await asyncio.gather(*(client.post(BASE, json=CREATE) for _ in range(4)))
        assert len({response.json()["id"] for response in created}) == 1
        sid, run, _ = await planned(client)
        responses = await asyncio.gather(
            *(
                client.post(
                    f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run, f"execute-{i}")
                )
                for i in range(3)
            )
        )
        assert sorted(r.status_code for r in responses) == [200, 409, 409]
        assert sum(r.event.type == "run.approved" for r in owner.store.records[sid]) == 1
        assert sum(r.event.type == "tool.intent" for r in owner.store.records[sid]) == 1


async def test_planner_audit_failure_prevents_first_call(agent_setup, monkeypatch):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        original = owner.store.append

        def failing(snapshot, kind, *args, **kwargs):
            if kind == "planner.attempt":
                owner.store.poisoned = True
                raise AgentFault("storage_unavailable", "Recording unavailable.", 503)
            return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", failing)
        response = await client.post(
            f"{BASE}/{sid}/runs",
            json={
                "request_id": "plan",
                "content": "Must not reach planner",
            },
        )
        assert "run.awaiting_approval" not in response.text
        agent_setup.planner.plan.assert_not_awaited()


async def test_rejection_revision_and_payload_binding(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        sid, run, _ = await planned(client)
        path = f"{BASE}/{sid}/runs/{run['id']}"
        revised_frames = decode(
            await client.post(
                path + "/revise",
                json={
                    **approval(run, "feedback"),
                    "kind": "feedback",
                    "content": "Keep it short",
                },
            )
        )
        revised = await run_detail(client, sid, revised_frames[-1]["run_id"])
        assert revised["plan"]["revision"] == 2
        assert revised["plan"]["digest"] != run["plan"]["digest"]
        assert agent_setup.planner.plan.call_args.args[0].feedback == "Keep it short"
        assert (await client.post(path + "/execute", json=approval(run))).status_code == 409
        response = await client.post(
            path + "/reject",
            json={
                **approval(revised, "reject"),
                "kind": "plan",
            },
        )
        assert response.json()["current_run"] is None
        assert (await run_detail(client, sid, run["id"]))["status"] == "rejected"
        assert not any(r.event.type == "tool.intent" for r in owner.store.records[sid])
        assert (await client.post(path + "/execute", json=approval(revised))).status_code == 409
        conflict = await client.post(
            f"{BASE}/{sid}/runs",
            json={
                "request_id": "plan",
                "content": "Different content",
            },
        )
        assert conflict.status_code == 409
        assert conflict.json()["error"]["code"] == "request_conflict"
        assert (
            await client.post(
                path + "/revise",
                json={
                    "request_id": "clarify",
                    "kind": "clarification",
                    "clarification_id": "q",
                    "content": "answer",
                },
            )
        ).status_code == 501


async def test_disabled_planner_and_strict_inputs_leave_chat_client_unused(
    agent_setup, mock_client
):
    async with agent_setup.opened(enabled=False) as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        response = await client.post(
            f"{BASE}/{sid}/runs",
            json={
                "request_id": "disabled",
                "content": "Try planning",
            },
        )
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "agent_not_configured"
        assert owner.store.get(sid).runs == []
        for extra in ({"tools": []}, {"reasoning_effort": "invalid"}):
            assert (await client.post(BASE, json={**CREATE, **extra})).status_code == 422
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/api/models")).json()["default_model"] == "deployment-a"
        mock_client.responses.create.assert_not_awaited()


async def consume(owner, op):
    return [frame async for frame in owner.frames(op)]


class BlockingFiles(FilePolicy):
    def __init__(self, roots):
        super().__init__(roots)
        self.entered, self.release = threading.Event(), threading.Event()
        self.calls = 0

    def read(self, args, stop, deadline):
        self.calls += 1
        self.entered.set()
        assert self.release.wait(5), "Test did not release worker"
        return super().read(args, stop, deadline)


async def wait_until(predicate):
    async with asyncio.timeout(3):
        while not predicate():  # noqa: ASYNC110 - bridge a thread-owned test signal
            await asyncio.sleep(0.005)


async def test_targeted_stop_pending_archive_and_worker_settlement(agent_setup):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    proposed = proposal()
    proposed.steps.append(proposed.steps[0].model_copy(deep=True))
    agent_setup.planner.plan.return_value = proposed
    async with agent_setup.opened(files=files) as (client, owner, _):
        sid, run, planning_frames = await planned(client)
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        stream = asyncio.create_task(consume(owner, op))
        try:
            await wait_until(files.entered.is_set)
            # Old planning Stop must not touch the newer execution operation.
            old = planning_frames[0]["operation_id"]
            snapshot, pending = await owner.cancel(
                sid,
                run["id"],
                Cancel(
                    request_id="old-stop",
                    operation_id=old,
                ),
            )
            assert not pending
            assert snapshot.runs[0].operation_id == op.id
            snapshot, pending = await owner.cancel(
                sid,
                run["id"],
                Cancel(
                    request_id="stop",
                    operation_id=op.id,
                ),
            )
            assert pending and snapshot.runs[0].status == "cancelling"
            assert op.id in owner.active and not op.task.done()
            response = await client.post(f"{BASE}/{sid}/archive", json={"request_id": "archive"})
            assert response.status_code == 202
            assert response.json()["archive_pending"] and not response.json()["archived"]
            with pytest.raises(AgentFault):
                await owner.prepare(sid, StartRun(request_id="new", content="New work"))
        finally:
            files.release.set()
            await stream
        settled = await owner.get(sid)
        assert settled.runs[0].status == "cancelled"
        assert settled.archived and not settled.archive_pending
        assert op.id not in owner.active and files.calls == 1
        listing = (await client.get(BASE)).json()
        assert listing == {"projection_version": 2, "sessions": [], "next_cursor": None, "total": 0}
        assert len((await client.get(BASE + "?include_archived=true")).json()["sessions"]) == 1


async def test_direct_asgi_disconnect_settles_without_background_progress(agent_setup):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    async with agent_setup.opened(files=files) as (client, owner, application):
        sid, run, _ = await planned(client)
        body = json.dumps(approval(run)).encode()
        disconnected = asyncio.Event()
        first = True

        async def receive():
            nonlocal first
            if first:
                first = False
                return {"type": "http.request", "body": body, "more_body": False}
            await wait_until(files.entered.is_set)
            disconnected.set()
            return {"type": "http.disconnect"}

        sent = []

        async def send(message):
            sent.append(message)

        scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": f"{BASE}/{sid}/runs/{run['id']}/execute",
            "query_string": b"",
            "headers": [(b"content-type", b"application/json")],
            "client": ("test", 1),
            "server": ("test", 80),
            "root_path": "",
        }
        task = asyncio.create_task(application(scope, receive, send))
        try:
            await asyncio.wait_for(disconnected.wait(), 3)
            await asyncio.wait_for(task, 3)
            assert owner.active
            assert (await owner.get(sid)).runs[0].status == "cancelling"
        finally:
            files.release.set()
            await asyncio.gather(*(op.task for op in list(owner.active.values())))
        assert (await owner.get(sid)).runs[0].status == "interrupted"
        assert files.calls == 1


async def test_shutdown_drains_worker_before_shared_client_close(agent_setup, mock_client):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    context = agent_setup.opened(files=files)
    client, owner, _ = await context.__aenter__()
    closing = stream = None
    try:
        sid, run, _ = await planned(client)
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        stream = asyncio.create_task(consume(owner, op))
        await wait_until(files.entered.is_set)
        closing = asyncio.create_task(context.__aexit__(None, None, None))
        await wait_until(lambda: owner.closing)
        assert not closing.done()
        mock_client.close.assert_not_awaited()
        assert op.id in owner.active
    finally:
        files.release.set()
        if stream:
            await stream
        if closing:
            await closing
        else:
            await context.__aexit__(None, None, None)
    mock_client.close.assert_awaited_once()


async def test_malformed_planner_result_is_not_approvable(agent_setup):
    agent_setup.planner.plan.return_value = {"objective": "not a validated plan"}
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        response = await client.post(
            f"{BASE}/{sid}/runs", json={"request_id": "plan", "content": "Read"}
        )
        frames = decode(response)
        run = await run_detail(client, sid, frames[-1]["run_id"])
        assert run["status"] == "failed" and run["plan"] is None
        assert not any(r.event.type == "tool.intent" for r in owner.store.records[sid])


async def test_audit_write_failure_prevents_tool_dispatch_and_recovers_unknown(
    agent_setup, monkeypatch
):
    async with agent_setup.opened() as (client, owner, _):
        sid, run, _ = await planned(client)
        original = owner.store.append

        def failing(snapshot, kind, *args, **kwargs):
            if kind == "tool.intent":
                owner.store.poisoned = True
                raise AgentFault("storage_unavailable", "Recording unavailable.", 503)
            return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", failing)
        read = Mock(wraps=owner.files.read)
        owner.files.read = read
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert "run.completed" not in response.text
        assert not read.called
    async with agent_setup.opened() as (client, owner, _):
        recovered = await run_detail(client, sid, run["id"])
        assert recovered["status"] == "interrupted"
        assert recovered["error"]["code"] == "outcome_unknown"
        assert (
            await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run, "retry"))
        ).status_code == 409


def test_store_orphans_truncated_tail_restart_idempotency_collision_and_middle_corruption(tmp_path):
    root = tmp_path / "storage"
    ids = iter(
        [
            "20261016T123456123456Z-ab12",
            "20261016T123456123456Z-ab12",
            "20261016T123456123456Z-cd34",
        ]
    )
    store = SessionStore(root, id_factory=lambda: next(ids))
    store.start()
    one = store.create(CreateSession(**CREATE))
    two = store.create(CreateSession(**{**CREATE, "request_id": "second"}))
    assert one.id != two.id
    with pytest.raises(AgentFault):
        store.get("../../escape")
    with pytest.raises(AgentFault):
        other = SessionStore(root)
        other.start()
    store.close()
    orphan = root / "20261016T123456123456Z-zzzz"
    orphan.mkdir()
    log = root / one.id / "events.jsonl"
    with log.open("ab") as handle:
        handle.write(b'{"incomplete":')
    recovered = SessionStore(root)
    recovered.start()
    assert recovered.create(CreateSession(**CREATE)).id == one.id
    assert not orphan.exists()
    assert (root / (".orphan-" + orphan.name)).exists()
    assert log.read_bytes().endswith(b"\n")
    recovered.close()
    raw = log.read_bytes()
    log.write_bytes(raw + b"bad-middle\n" + raw)
    with pytest.raises(AgentFault, match="safely"):
        SessionStore(root).start()


def test_interrupted_recovery_does_not_replay_tools(tmp_path):
    store = SessionStore(tmp_path / "storage")
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    snapshot.runs.append(
        AgentRun(
            id="run",
            request="request",
            status="executing",
            operation_id="operation",
            created_at=store.clock(),
            updated_at=store.clock(),
        )
    )
    store.append(snapshot, "run.executing", "execute", run_id="run", operation_id="operation")
    store.close()
    store = SessionStore(tmp_path / "storage")
    store.start()
    assert store.get(snapshot.id).runs[0].status == "interrupted"
    assert store.get(snapshot.id).runs[0].operation_id is None
    store.close()


def test_fsync_failure_is_not_published(tmp_path, monkeypatch):
    store = SessionStore(tmp_path / "storage")
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    store.create(CreateSession(**{**CREATE, "request_id": "second"}))
    before = len(store.records[snapshot.id])

    def fail(fd):
        raise OSError("credential-shaped private exception must not escape")

    monkeypatch.setattr(os, "fsync", fail)
    with pytest.raises(AgentFault) as failure:
        store.append(snapshot, "session.updated", "update")
    assert "private exception" not in str(failure.value)
    assert store.poisoned
    assert len(store.records[snapshot.id]) == before
    store.close()


@pytest.mark.parametrize(
    "path",
    [
        "../note.txt",
        "/etc/passwd",
        ".env",
        ".env.example",
        "credentials.json",
        "dev-plans-archive/plan.md",
        ".git/config",
        "node_modules/pkg/a",
        "sessions/a",
        "private/plan",
        "a/../../note.txt",
        "a\\note.txt",
    ],
    ids=range(12),
)
def test_file_exclusions(agent_setup, path):
    policy = FilePolicy({"fixture": agent_setup.workspace})
    with pytest.raises(AgentFault):
        policy.read(
            ReadArguments(workspace_id="fixture", path=path),
            threading.Event(),
            time.monotonic() + 3,
        )


def test_file_symlinks_binary_and_storage_symlinks(agent_setup, tmp_path):
    workspace = agent_setup.workspace
    (workspace / "link").symlink_to(workspace / "note.txt")
    (workspace / "dirlink").symlink_to(workspace, target_is_directory=True)
    (workspace / "binary").write_bytes(b"a\x00b")
    (workspace / "large").write_bytes(b"a" * 256001)
    os.mkfifo(workspace / "pipe")
    policy = FilePolicy({"fixture": workspace})
    for path in ("link", "dirlink/note.txt", "binary", "pipe"):
        with pytest.raises(AgentFault):
            policy.read(
                ReadArguments(workspace_id="fixture", path=path),
                threading.Event(),
                time.monotonic() + 3,
            )
    page = policy.read(
        ReadArguments(workspace_id="fixture", path="large"),
        threading.Event(),
        time.monotonic() + 3,
    )
    assert page["content"].startswith("1: a") and page["returned_lines"] == 1
    assert page["truncated"] is True
    link = tmp_path / "storage-link"
    link.symlink_to(workspace, target_is_directory=True)
    with pytest.raises(AgentFault):
        SessionStore(link).start()


def test_file_replacement_race_and_denied_configured_root(agent_setup, monkeypatch):
    workspace = agent_setup.workspace
    policy = FilePolicy({"fixture": workspace})
    original = os.open

    def replaced(path, flags, *args, **kwargs):
        if path == "note.txt" and kwargs.get("dir_fd") is not None:
            (workspace / "note.txt").unlink()
            (workspace / "note.txt").symlink_to(workspace / "other.txt")
        return original(path, flags, *args, **kwargs)

    (workspace / "other.txt").write_text("not-approved-target")
    monkeypatch.setattr(os, "open", replaced)
    with pytest.raises(AgentFault):
        policy.read(
            ReadArguments(workspace_id="fixture", path="note.txt"),
            threading.Event(),
            time.monotonic() + 3,
        )
    with pytest.raises(AgentFault):
        FilePolicy({"fixture": workspace / "sessions"})


def test_journal_symlink_cannot_redirect_write(tmp_path):
    store = SessionStore(tmp_path / "storage")
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    journal = store.root / snapshot.id / "events.jsonl"
    journal.unlink()
    outside = tmp_path / "outside"
    outside.write_text("untouched")
    journal.symlink_to(outside)
    with pytest.raises(AgentFault):
        store.append(snapshot, "session.updated", "update")
    assert outside.read_text() == "untouched"
    assert store.poisoned
    store.close()


async def test_enospc_before_admission_is_not_published_and_restart_recovers(
    agent_setup, monkeypatch
):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        before = owner.store.get(sid).sequence
        original = os.write

        def no_space(fd, raw):
            raise OSError(errno.ENOSPC, "No space left on device")

        with monkeypatch.context() as patch:
            patch.setattr(os, "write", no_space)
            response = await client.post(
                f"{BASE}/{sid}/runs", json={"request_id": "plan", "content": "Read"}
            )
            assert response.status_code == 503
            assert response.headers["content-type"].startswith("application/json")
        assert os.write is original
        assert not owner.active
        assert owner.store.poisoned
        agent_setup.planner.plan.assert_not_awaited()
    async with agent_setup.opened() as (client, owner, _):
        assert owner.store.get(sid).sequence == before
        assert (await client.get(f"{BASE}/{sid}")).json()["run_count"] == 0
        assert len((await client.get(BASE)).json()["sessions"]) == 1


async def test_mutations_do_not_expire_or_revoke_owned_operation(agent_setup):
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        op = await owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        for index in range(100):
            await owner.patch(sid, PatchSession(request_id=f"title-{index}", title="x" * 200))
        assert not op.stop.is_set()
        await owner.disconnect(op)
        await op.task
        assert not owner.active
        settled = await owner.get(sid)
        assert settled.runs[0].status == "interrupted"
        assert settled.sequence > 100
    async with agent_setup.opened() as (_, owner, _):
        assert (await owner.get(sid)).runs[0].status == "interrupted"
        assert not owner.store.poisoned


def test_recovery_of_legacy_operation_is_repeatable(tmp_path):
    root = tmp_path / "journal"
    store = SessionStore(root)
    store.start()
    snapshot = store.create(CreateSession(**CREATE))
    snapshot.runs.append(
        AgentRun(
            id="run",
            request="Read",
            created_at=store.clock(),
            updated_at=store.clock(),
            status="planning",
            operation_id="operation",
        )
    )
    store.append(snapshot, "run.planning", "plan", run_id="run", operation_id="operation")
    store.close()
    for _ in range(2):
        recovered = SessionStore(root)
        recovered.start()
        snapshot = recovered.get(snapshot.id)
        assert snapshot.runs[0].status == "interrupted"
        assert snapshot.runs[0].error.code == "outcome_unknown"
        assert snapshot.sequence == 3  # Exactly one recovery record, not one per restart.
        assert len(recovered.listing(False)) == 1
        assert not recovered.poisoned
        recovered.close()


def test_creation_enospc_does_not_publish_or_grow_empty_sessions(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    store = SessionStore(root)
    store.start()
    first = store.create(CreateSession(**CREATE))
    entries = set(root.iterdir())
    retry = CreateSession(**{**CREATE, "request_id": "retry"})

    def no_space(fd, raw):
        raise OSError(errno.ENOSPC, "No space left on device")

    with monkeypatch.context() as patch:
        patch.setattr(os, "write", no_space)
        with pytest.raises(AgentFault) as failure:
            store.create(retry)
        assert failure.value.code == "storage_unavailable"
        assert list(store.records) == [first.id]
        assert store.poisoned
    # An uncertain physical write may retain one unpublished directory for recovery.
    assert len(set(root.iterdir()) - entries) <= 1
    store.close()
    recovered = SessionStore(root)
    recovered.start()
    second = recovered.create(retry)
    assert recovered.create(CreateSession(**CREATE)).id == first.id
    assert recovered.create(retry).id == second.id
    assert len(recovered.listing(False)) == 2
    recovered.close()


def test_creation_uncertain_write_is_retained_for_replay(tmp_path, monkeypatch):
    root = tmp_path / "journal"
    store = SessionStore(root)
    store.start()
    original = os.fsync
    calls = 0

    def fail_after_write(fd):
        nonlocal calls
        calls += 1
        # First fsync commits the session mkdir, second follows the journal write.
        if calls == 2:
            raise OSError("unavailable")
        original(fd)

    with monkeypatch.context() as patch:
        patch.setattr(os, "fsync", fail_after_write)
        with pytest.raises(AgentFault):
            store.create(CreateSession(**CREATE))
    assert not store.records and not store.creations
    folders = [path for path in root.iterdir() if path.is_dir()]
    assert len(folders) == 1 and (folders[0] / "events.jsonl").is_file()
    store.close()
    recovered = SessionStore(root)
    recovered.start()
    assert recovered.create(CreateSession(**CREATE)).id == folders[0].name
    recovered.close()


async def test_native_cancellation_during_admission_retains_owner_and_settles(
    agent_setup, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    async with agent_setup.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        original = owner.store.append

        def blocked(snapshot, kind, *args, **kwargs):
            if kind == "run.planning":
                entered.set()
                assert release.wait(5)
            return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", blocked)
        caller = asyncio.create_task(
            owner.prepare(sid, StartRun(request_id="plan", content="Read"))
        )
        try:
            await wait_until(entered.is_set)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(caller, 0.5)
            assert owner.admissions
        finally:
            release.set()
        await wait_until(lambda: not owner.admissions and not owner.active)
        snapshot = await owner.get(sid)
        run = snapshot.runs[0]
        assert run.status == "interrupted" and run.operation_id is None
        operation_id = owner.store.records[sid][1].event.operation_id
        _, pending = await owner.cancel(
            sid, run.id, Cancel(request_id="old-stop", operation_id=operation_id)
        )
        assert not pending
        agent_setup.planner.plan.assert_not_awaited()
    async with agent_setup.opened() as (_, owner, _):
        assert (await owner.get(sid)).runs[0].status == "interrupted"


def execution_transport(application, sid, run, disconnected):
    body = json.dumps(approval(run)).encode()
    first = True
    sent = []

    async def receive():
        nonlocal first
        if first:
            first = False
            return {"type": "http.request", "body": body, "more_body": False}
        await disconnected.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": f"{BASE}/{sid}/runs/{run['id']}/execute",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 1),
        "server": ("test", 80),
        "root_path": "",
    }
    return asyncio.create_task(application(scope, receive, send)), sent


async def test_file_request_timeout_settles_after_owned_worker_returns(agent_setup):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    async with agent_setup.opened(files=files, file_timeout_seconds=0.02) as (
        client,
        owner,
        application,
    ):
        sid, run, _ = await planned(client)
        transport, sent = execution_transport(application, sid, run, asyncio.Event())
        try:
            await wait_until(files.entered.is_set)
            op = next(iter(owner.active.values()))
            await asyncio.sleep(0.03)
            assert not transport.done() and not op.stop.is_set()
            assert op.id in owner.active and not op.task.done()
            assert files.calls == 1 and sent[0]["status"] == 200
        finally:
            files.release.set()
            await transport
            await asyncio.gather(*(op.task for op in list(owner.active.values())))
        snapshot = await owner.get(sid)
        assert snapshot.runs[0].status == "failed"
        assert snapshot.runs[0].error.code == "file_timeout"


async def test_asgi_disconnect_does_not_wait_for_blocked_storage_lock(agent_setup, monkeypatch):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    entered, release = threading.Event(), threading.Event()
    disconnected = asyncio.Event()
    async with agent_setup.opened(files=files) as (client, owner, application):
        sid, run, _ = await planned(client)
        transport, _ = execution_transport(application, sid, run, disconnected)
        await wait_until(files.entered.is_set)
        op = next(iter(owner.active.values()))
        original = owner.store.append

        def blocked(snapshot, kind, *args, **kwargs):
            with owner.store.lock:
                if kind == "session.updated":
                    entered.set()
                    assert release.wait(5)
                return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", blocked)
        mutation = asyncio.create_task(
            owner.patch(sid, PatchSession(request_id="title", title="Still owned"))
        )
        try:
            await wait_until(entered.is_set)
            disconnected.set()
            await asyncio.wait_for(asyncio.shield(transport), 1)
            assert op.stop.is_set() and op.id in owner.active
            assert owner.control_tasks and owner.io_tasks
            assert not op.stopping.done() and not mutation.done()
            assert not op.task.done()
        finally:
            release.set()
            files.release.set()
            await mutation
            await transport
            await op.task
            await op.stopping
        assert (await owner.get(sid)).runs[0].status == "interrupted"


async def test_targeted_stop_signal_bypasses_blocked_storage_before_ack(agent_setup, monkeypatch):
    files = BlockingFiles({"fixture": agent_setup.workspace})
    entered, release = threading.Event(), threading.Event()
    async with agent_setup.opened(files=files) as (client, owner, _):
        sid, run, _ = await planned(client)
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        stream = asyncio.create_task(consume(owner, op))
        await wait_until(files.entered.is_set)
        with pytest.raises(AgentFault) as conflict:
            await owner.cancel(sid, run["id"], Cancel(request_id="execute", operation_id=op.id))
        assert conflict.value.code == "request_conflict" and not op.stop.is_set()
        original = owner.store.append

        def blocked(snapshot, kind, *args, **kwargs):
            with owner.store.lock:
                if kind == "session.updated" and args[0] == "title":
                    entered.set()
                    assert release.wait(5)
                return original(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(owner.store, "append", blocked)
        mutation = asyncio.create_task(
            owner.patch(sid, PatchSession(request_id="title", title="x"))
        )
        stop = None
        try:
            await wait_until(entered.is_set)
            stop = asyncio.create_task(
                owner.cancel(sid, run["id"], Cancel(request_id="stop", operation_id=op.id))
            )
            await wait_until(op.stop.is_set)
            assert not stop.done() and op.id in owner.active
            release.set()
            await mutation
            snapshot, pending = await stop
            assert pending and snapshot.runs[0].status == "cancelling"
        finally:
            release.set()
            files.release.set()
            await mutation
            if stop is not None:
                await stop
            await stream
        assert (await owner.get(sid)).runs[0].status == "cancelled"


async def test_file_policy_binds_custom_storage_and_workspace_aliases(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root = workspace / "journal"
    store = SessionStore(root)
    store.start()
    session = store.create(CreateSession(**{**CREATE, "request_id": "audit-sentinel"}))
    alias = workspace / "alias"
    alias.symlink_to(root, target_is_directory=True)
    files = FilePolicy(
        {
            "parent": workspace,
            "direct": root,
            "child": root / session.id,
            "alias": alias,
        }
    )
    owner = AgentService(store, files=files)
    try:
        for workspace_id, path in (
            ("parent", f"journal/{session.id}/events.jsonl"),
            ("direct", f"{session.id}/events.jsonl"),
            ("child", "events.jsonl"),
            ("parent", f"alias/{session.id}/events.jsonl"),
            ("alias", f"{session.id}/events.jsonl"),
        ):
            with pytest.raises(AgentFault) as failure:
                files.read(
                    ReadArguments(workspace_id=workspace_id, path=path),
                    threading.Event(),
                    time.monotonic() + 3,
                )
            assert "audit-sentinel" not in failure.value.message
    finally:
        await owner.close()


@pytest.mark.parametrize(
    "component",
    [
        "private-archive",
        "credentials-backup",
        "dev-plans-archive-copy",
    ],
    ids=range(3),
)
def test_policy_component_rules_match_for_roots_ancestors_and_paths(tmp_path, component):
    for root in (tmp_path / component, tmp_path / component / "child"):
        with pytest.raises(AgentFault):
            FilePolicy({"fixture": root})
    policy = FilePolicy({"fixture": tmp_path})
    with pytest.raises(AgentFault):
        policy.validate(ReadArguments(workspace_id="fixture", path=f"{component}/note.txt"))


def test_policy_allows_platform_anchor_but_not_nested_names(agent_setup):
    policy = FilePolicy({"fixture": agent_setup.workspace})
    assert policy.read(
        ReadArguments(workspace_id="fixture", path="note.txt"),
        threading.Event(),
        time.monotonic() + 3,
    )["content"].startswith("1: alpha")
    with pytest.raises(AgentFault):
        FilePolicy({"fixture": agent_setup.workspace / "private"})


def guard_session_syscalls(monkeypatch):
    """Intercept the dangerous fallback rather than ever create/read cwd data."""
    original_mkdir, original_open, original_listdir = os.mkdir, os.open, os.listdir
    unsafe = []
    writes = []

    def mkdir(path, *args, **kwargs):
        if SESSION_ID.fullmatch(os.fspath(path)):
            if kwargs.get("dir_fd") is None:
                unsafe.append("mkdir")
                raise AssertionError("Unanchored session mkdir intercepted")
            writes.append(kwargs["dir_fd"])
        return original_mkdir(path, *args, **kwargs)

    def opened(path, flags, *args, **kwargs):
        if (
            not os.path.isabs(path)
            and (
                flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT)
                or SESSION_ID.fullmatch(os.fspath(path))
            )
            and kwargs.get("dir_fd") is None
        ):
            unsafe.append("open")
            raise AssertionError("Unanchored session open intercepted")
        return original_open(path, flags, *args, **kwargs)

    def listed(path=None):
        if path is None:
            unsafe.append("listdir")
            raise AssertionError("Cwd directory listing intercepted")
        return original_listdir(path)

    monkeypatch.setattr(os, "mkdir", mkdir)
    monkeypatch.setattr(os, "open", opened)
    monkeypatch.setattr(os, "listdir", listed)
    return unsafe, writes


async def test_shutdown_barrier_covers_ready_to_create_scheduling_gap(agent_setup, monkeypatch):
    async with agent_setup.opened() as (_, owner, _):
        ready_done, queue_create = asyncio.Event(), asyncio.Event()
        entered, release = threading.Event(), threading.Event()
        original_ready, original_create = owner.ready, owner.store.create

        async def paused_ready():
            await original_ready()
            ready_done.set()
            await queue_create.wait()

        def blocked_create(body):
            entered.set()  # Deliberately before SessionStore's own lock.
            assert release.wait(5)
            return original_create(body)

        monkeypatch.setattr(owner, "ready", paused_ready)
        monkeypatch.setattr(owner.store, "create", blocked_create)
        unsafe, writes = guard_session_syscalls(monkeypatch)
        caller = asyncio.create_task(owner.create(CreateSession(**CREATE)))
        closing = None
        try:
            await asyncio.wait_for(ready_done.wait(), 3)
            assert not owner.io_tasks and owner.lock.locked()
            closing = asyncio.create_task(owner.close())
            await wait_until(lambda: owner.closing)
            assert not closing.done() and owner.store.fd is not None
            with pytest.raises(AgentFault) as rejected:
                await owner.prepare("unused", StartRun(request_id="new", content="Read"))
            assert rejected.value.code == "service_closing" and not owner.admissions
            queue_create.set()
            await wait_until(entered.is_set)
            assert not closing.done() and owner.store.fd is not None
        finally:
            queue_create.set()
            release.set()
            snapshot = await caller
            if closing is not None:
                await closing
        assert not unsafe and len(writes) == 1
        assert (owner.store.root / snapshot.id / "events.jsonl").is_file()
        assert owner.store.fd is None and not owner.io_tasks
        with pytest.raises(AgentFault) as rejected:
            await owner.create(CreateSession(**{**CREATE, "request_id": "late"}))
        assert rejected.value.code == "service_closing"


@pytest.mark.parametrize("cancel_waiter", [False, True])
async def test_shutdown_drains_inflight_creation_before_store_close(
    agent_setup, monkeypatch, cancel_waiter
):
    async with agent_setup.opened() as (_, owner, _):
        entered, release = threading.Event(), threading.Event()
        original_create = owner.store.create

        def blocked_create(body):
            entered.set()
            assert release.wait(5)
            return original_create(body)

        monkeypatch.setattr(owner.store, "create", blocked_create)
        unsafe, writes = guard_session_syscalls(monkeypatch)
        caller = asyncio.create_task(owner.create(CreateSession(**CREATE)))
        closing = None
        try:
            await wait_until(entered.is_set)
            if cancel_waiter:
                caller.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await caller
            closing = asyncio.create_task(owner.close())
            await wait_until(lambda: owner.closing)
            assert not closing.done() and owner.store.fd is not None and owner.io_tasks
        finally:
            release.set()
            if not cancel_waiter:
                await caller
            if closing is not None:
                await closing
        assert not unsafe and len(writes) == 1
        assert len(owner.store.records) == 1
        assert owner.store.fd is None and not owner.io_tasks
    recovered = SessionStore(agent_setup.root)
    recovered.start()
    assert recovered.create(CreateSession(**CREATE)).id in recovered.records
    assert len(recovered.listing(False)) == 1
    recovered.close()


@pytest.mark.parametrize("opened_first", [False, True])
def test_store_writes_require_live_descriptor_before_any_syscall(
    tmp_path, monkeypatch, opened_first
):
    store = SessionStore(tmp_path / "journal")
    snapshot = record = None
    if opened_first:
        store.start()
        snapshot = store.create(CreateSession(**CREATE))
        record = store.records[snapshot.id][0]
        store.close()
    unsafe, writes = guard_session_syscalls(monkeypatch)
    operations = [
        lambda: store.create(CreateSession(**{**CREATE, "request_id": "new"})),
        lambda: store._directory("20261016T123456123456Z-ab12"),
        store._recover,
        store.identity,
    ]
    if snapshot is not None:
        operations.extend(
            [
                lambda: store.append(snapshot, "session.updated", "update"),
                lambda: store._write(record, b"uncommitted\n"),
            ]
        )
    for operation in operations:
        with pytest.raises(AgentFault) as rejected:
            operation()
        assert rejected.value.code == "storage_unavailable"
    assert not unsafe and not writes


async def test_policy_directory_identity_blocks_renamed_storage_and_root_ancestors(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    original, moved = workspace / "journal", workspace / "moved"
    files = FilePolicy({"parent": workspace})
    owner = AgentService(SessionStore(original), files=files, models=("deployment-a",))
    await owner.start()
    try:
        session = await owner.create(CreateSession(**CREATE))
        identity = owner.store.identity()
        assert identity in files.storage_identities[original]
        original.rename(moved)
        files.roots.update({"direct": moved, "nested": moved / session.id})
        original_open = os.open
        descended = []

        def guarded(path, flags, *args, **kwargs):
            if path in (session.id, "events.jsonl"):
                descended.append(path)
                raise AssertionError(
                    "Storage must be denied before descending/opening its contents"
                )
            return original_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", guarded)
            for workspace_id, path in (
                ("parent", f"moved/{session.id}/events.jsonl"),
                ("direct", f"{session.id}/events.jsonl"),
                ("nested", "events.jsonl"),
            ):
                args = ReadArguments(workspace_id=workspace_id, path=path)
                files.validate(args)  # No lexical containment match after rename.
                with pytest.raises(AgentFault) as denied:
                    files.read(args, threading.Event(), time.monotonic() + 3)
                assert denied.value.code == "path_denied"
        assert not descended
    finally:
        await owner.close()


async def test_policy_case_alias_storage_identity_and_configured_root_ancestors(
    tmp_path, monkeypatch
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    root, alias = workspace / "journal", workspace / "JOURNAL"
    files = FilePolicy({"parent": workspace})
    owner = AgentService(SessionStore(root), files=files, models=("deployment-a",))
    await owner.start()
    try:
        if not alias.exists() or not alias.samefile(root):
            pytest.skip("Temporary fixture filesystem is case-sensitive")
        session = await owner.create(CreateSession(**{**CREATE, "request_id": "audit-sentinel"}))
        files.roots.update({"direct": alias, "nested": alias / session.id})
        normal = ReadArguments(workspace_id="parent", path=f"journal/{session.id}/events.jsonl")
        with pytest.raises(AgentFault) as denied:
            files.read(normal, threading.Event(), time.monotonic() + 3)
        assert denied.value.code == "path_denied"
        original_open = os.open
        descended = []

        def guarded(path, flags, *args, **kwargs):
            if path in (session.id, "events.jsonl"):
                descended.append(path)
                raise AssertionError("Case alias reached internal storage contents")
            return original_open(path, flags, *args, **kwargs)

        with monkeypatch.context() as patch:
            patch.setattr(os, "open", guarded)
            for workspace_id, path in (
                ("parent", f"JOURNAL/{session.id}/events.jsonl"),
                ("direct", f"{session.id}/events.jsonl"),
                ("nested", "events.jsonl"),
            ):
                args = ReadArguments(workspace_id=workspace_id, path=path)
                files.validate(args)  # Uppercase spelling defeats only the lexical shortcut.
                with pytest.raises(AgentFault) as denied:
                    files.read(args, threading.Event(), time.monotonic() + 3)
                assert denied.value.code == "path_denied"
        assert not descended
    finally:
        await owner.close()
