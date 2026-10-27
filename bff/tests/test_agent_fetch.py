"""Approval, dispatch, settlement and offline public-web evidence integration."""

import asyncio
import os
import stat
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from pydantic import ValidationError

import app.agent as agent_module
from app.agent import AgentService
from app.agent_files import AgentFault, FilePolicy, canonical_digest
from app.agent_schemas import (
    Cancel,
    FetchArguments,
    PlanIdentity,
    Proposal,
    ProposedStep,
)
from app.main import create_app
from app.sessions import SessionStore
from app.tools import FetchDocument, WebFetcher
from app.tools.registry import ToolRegistry
from tests.test_agent import (
    BASE,
    CREATE,
    approval,
    consume,
    decode,
    proposal,
    run_detail,
    wait_until,
)
from tests.test_web_fetch import FakeStream, reply
from tests.test_web_fetch import fetcher as http_fetcher

URL = "https://example.com/article"
TEXT = "Public evidence 🌱\n" * 4000 + "END-OF-COMPLETE-EVIDENCE"


def fetch_proposal(*, offset=0, limit=12000, steps=1):
    return Proposal.model_validate(
        {
            "objective": "Read the supplied public URL directly",
            "assumptions": [],
            "steps": [
                {
                    "title": "Fetch supplied URL",
                    "description": "Preserve the complete static text as evidence.",
                    "tool": "web_fetch",
                    "arguments": {"url": URL, "offset": offset, "limit": limit},
                }
                for _ in range(steps)
            ],
        }
    )


def document(text=TEXT):
    return FetchDocument(
        requested_url=URL,
        final_url=URL + "/final",
        title="Public title",
        status_code=200,
        content_type="text/html",
        fetched_at="2026-10-19T10:00:00Z",
        redirects=(URL + "/final",),
        text=text,
    )


class ChunkDocument:
    """Chunk-only document double: accessing a materialized body is a test failure."""

    def __init__(self, chunks):
        self.chunks = chunks
        self.text_length = sum(len(chunk) for chunk in chunks)
        self.requested_url, self.final_url = URL, URL + "/final"
        self.title, self.status_code, self.content_type = "Public title", 200, "text/html"
        self.fetched_at, self.redirects = "2026-10-19T10:00:00Z", (URL + "/final",)
        self.closed, self.close_count, self.chunks_read = False, 0, 0

    @property
    def text(self):
        raise AssertionError("Runtime must not materialize document.text")

    def iter_text(self):
        for chunk in self.chunks:
            assert not self.closed, "Document closed before artifact worker settled"
            self.chunks_read += 1
            yield chunk

    def close(self):
        self.closed = True
        self.close_count += 1


@pytest.fixture
def fetch_setup(tmp_path, settings_factory, mock_client):
    root = tmp_path / "journal"
    planner = SimpleNamespace(plan=AsyncMock(return_value=fetch_proposal()))
    fetcher = SimpleNamespace(
        policy_version=WebFetcher.policy_version,
        validate_url=Mock(side_effect=WebFetcher().validate_url),
        fetch=AsyncMock(return_value=document()),
    )

    @asynccontextmanager
    async def opened(*, fetch=None):
        owner = AgentService(
            SessionStore(root),
            planner=planner,
            fetcher=fetch if fetch is not None else fetcher,
            models=("deployment-a",),
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
        mock_client.responses.create.assert_not_awaited()

    return SimpleNamespace(opened=opened, fetcher=fetcher, planner=planner, root=root)


async def planned_fetch(client):
    sid = (await client.post(BASE, json=CREATE)).json()["id"]
    frames = decode(
        await client.post(
            f"{BASE}/{sid}/runs", json={"request_id": "plan", "content": f"Read {URL}"}
        )
    )
    run = await run_detail(client, sid, frames[-1]["run_id"])
    assert run["status"] == "awaiting_approval"
    return sid, run


async def executed_fetch(client):
    sid, run = await planned_fetch(client)
    frames = decode(await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run)))
    detail = await run_detail(client, sid, run["id"])
    result = detail["results"][0]
    assert result["status"] == "completed", detail["error"]
    return sid, run, result, frames


def test_strict_fetch_and_matching_tool_arguments():
    assert FetchArguments(url=URL).model_dump() == {"url": URL, "offset": 0, "limit": 12000}
    for bad in (
        {"offset": -1},
        {"offset": "0"},
        {"offset": True},
        {"limit": 0},
        {"limit": 16001},
        {"limit": 1.5},
        {"headers": {}},
        {"url": 1},
    ):
        with pytest.raises(ValidationError):
            FetchArguments.model_validate({"url": URL, **bad})
    for tool, args in (
        ("read_file", {"url": URL}),
        ("web_fetch", {"workspace_id": "fixture", "path": "note.txt"}),
        ("web_search", {"url": URL}),
        ("grep_in_folder", {"url": URL}),
    ):
        with pytest.raises(ValidationError):
            ProposedStep(title="Read", description="Read", tool=tool, arguments=args)


async def test_real_fetcher_no_dns_http_or_tools_before_exact_approval(fetch_setup):
    network = FakeStream(reply("<main>Readable evidence</main>", content_type="text/html"))
    fetch, backend, resolver = http_fetcher(network)
    async with fetch_setup.opened(fetch=fetch) as (client, owner, _):
        reads = Mock(side_effect=AssertionError("No file reads"))
        owner.files.read = reads
        sid, run = await planned_fetch(client)
        resolver.assert_not_called()
        assert not backend.calls and not network.writes
        context = fetch_setup.planner.plan.call_args.args[0]
        assert len(context.available_tools) == 3
        assert "web_fetch:" in context.available_tools[1]
        assert "Planning performs no tool execution" in context.instructions
        assert "only the configured LLM provider is contacted" in context.instructions
        path = f"{BASE}/{sid}/runs/{run['id']}/execute"
        response = await client.post(path, json={**approval(run), "digest": "0" * 64})
        assert response.status_code == 409
        resolver.assert_not_called()
        real_resolve = resolver.side_effect

        async def resolve(*args):
            assert owner.store.records[sid][-1].event.type == "tool.intent"
            return ("93.184.216.34",)

        resolver.side_effect = resolve
        frames = decode(await client.post(path, json=approval(run)))
        resolver.side_effect = real_resolve
        assert frames[-1]["type"] == "run.completed"
        result = (await run_detail(client, sid, run["id"]))["results"][0]
        assert result["content"] == "Readable evidence"
        assert "<main>" not in result["content"]
        assert len(backend.calls) == 1 and network.closed
        reads.assert_not_called()
        for key in ("execute", "new-key"):
            assert (await client.post(path, json=approval(run, key))).status_code == 409
        assert len(backend.calls) == 1


@pytest.mark.parametrize("reason", ["stop", "recording_failure", "poisoned"])
async def test_queued_revocation_beats_fetch_task_without_any_network_entry(
    fetch_setup, monkeypatch, reason
):
    network = FakeStream(reply("No request should reach this response"))
    fetch, backend, resolver = http_fetcher(network)
    fetch.fetch = AsyncMock(wraps=fetch.fetch)
    async with fetch_setup.opened(fetch=fetch) as (client, owner, _):
        sid, run = await planned_fetch(client)
        validate = ToolRegistry.validate
        queued, fired, commands = False, [], []

        def revoke():
            op = next(iter(owner.active.values()))
            owner.store.poisoned = True
            if reason == "recording_failure":
                owner.stop_for_recording_failure()
            fired.append((op.stop.is_set(), owner.store.poisoned))

        async def cancel(op):
            await owner.cancel(sid, run["id"], Cancel(request_id="stop", operation_id=op.id))
            fired.append((op.stop.is_set(), owner.store.poisoned))

        def queue_before_dispatch(registry, step):
            nonlocal queued
            validate(registry, step)
            if not queued and owner.store.records[sid][-1].event.type == "tool.intent":
                queued = True
                if reason == "stop":
                    commands.append(asyncio.create_task(cancel(next(iter(owner.active.values())))))
                else:
                    asyncio.get_running_loop().call_soon(revoke)

        monkeypatch.setattr(ToolRegistry, "validate", queue_before_dispatch)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        await asyncio.gather(*commands)
        assert queued and fired
        fetch.fetch.assert_not_called()
        resolver.assert_not_called()
        assert not backend.calls and not network.writes and not network.tls
        assert "tool.result" not in response.text and "run.completed" not in response.text
        if reason == "stop":
            assert fired == [(True, False)]
            assert decode(response)[-1]["run"]["status"] == "cancelled"
        else:
            assert fired == [(reason == "recording_failure", True)]


@pytest.mark.parametrize("chunks", [(), ("",), ("α🌱", "", "beta\n", "終" * 20000)])
async def test_chunk_only_document_full_evidence_and_close_on_success(fetch_setup, chunks):
    fetched = ChunkDocument(chunks)
    fetch_setup.fetcher.fetch.return_value = fetched
    async with fetch_setup.opened() as (client, owner, _):
        sid, _, result, _ = await executed_fetch(client)
        expected = "".join(chunks)
        assert fetched.closed and fetched.close_count == 1
        assert fetched.chunks_read == len(chunks)
        assert result["fetch"]["total_chars"] == len(expected)
        assert result["content"] == expected[:12000]
        path = owner.store.root / sid / "artifacts" / (result["fetch"]["artifact_id"] + ".txt")
        assert path.read_text() == expected
        assert owner.store.artifact_bytes == len(expected.encode())


async def test_document_closed_on_artifact_write_failure(fetch_setup, monkeypatch):
    fetched = ChunkDocument(("Complete ", "evidence 🌱"))
    fetch_setup.fetcher.fetch.return_value = fetched
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        original = os.write

        def fail_evidence(fd, raw):
            if bytes(raw).startswith(b"Complete "):
                raise OSError("artifact write failed")
            return original(fd, raw)

        monkeypatch.setattr(os, "write", fail_evidence)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert fetched.closed and fetched.close_count == 1
        assert owner.store.poisoned
        assert "tool.result" not in response.text and "run.completed" not in response.text


@pytest.mark.parametrize("reason", ["stop", "disconnect", "shutdown", "native_cancel"])
async def test_document_stays_open_until_artifact_worker_settles_on_cancellation(
    fetch_setup, monkeypatch, reason
):
    entered, release = threading.Event(), threading.Event()
    fetched = ChunkDocument(("Evidence 🌱\n",) * 8)
    fetch_setup.fetcher.fetch.return_value = fetched
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        original = owner.store.write_artifact

        def blocked(*args):
            entered.set()
            assert release.wait(5), "Test did not release the artifact worker"
            assert not fetched.closed
            return original(*args)

        monkeypatch.setattr(owner.store, "write_artifact", blocked)
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        stream = asyncio.create_task(consume(owner, op))
        closing = None
        try:
            await wait_until(entered.is_set)
            if reason == "stop":
                await owner.cancel(sid, run["id"], Cancel(request_id="stop", operation_id=op.id))
            elif reason == "disconnect":
                await owner.disconnect(op)
            elif reason == "shutdown":
                closing = asyncio.create_task(owner.close())
                await wait_until(lambda: owner.closing)
            else:
                op.task.cancel()
                await asyncio.sleep(0)
            assert not fetched.closed and op.id in owner.active and not op.task.done()
            assert owner.io_tasks and owner.store.fd is not None
        finally:
            release.set()
            await stream
            await asyncio.gather(op.task, return_exceptions=True)
            if closing:
                await closing
        assert fetched.closed and fetched.close_count == 1
        assert not owner.active and not owner.io_tasks
        assert not any(record.event.type == "tool.result" for record in owner.store.records[sid])


async def test_document_returned_concurrently_with_stop_is_closed_not_persisted(fetch_setup):
    fetched = ChunkDocument(("Not committed",))
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)

        async def stopped_result(url):
            owner.request_stop(next(iter(owner.active.values())), "cancelled")
            return fetched

        fetch_setup.fetcher.fetch.side_effect = stopped_result
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert fetched.closed and fetched.close_count == 1 and fetched.chunks_read == 0
        assert decode(response)[-1]["run"]["status"] == "cancelled"
        assert not (owner.store.root / sid / "artifacts").exists()


@pytest.mark.parametrize("phase", ["success", "write_failure", "cancel"])
async def test_production_spooled_document_streams_to_durable_evidence_and_closes(
    fetch_setup, monkeypatch, phase
):
    text = "Public 🌱 evidence\n" * 40000 + "COMPLETE-END"
    network = FakeStream(reply(text))
    fetch, backend, resolver = http_fetcher(network)
    fetched, closed = [], []
    original_fetch, original_close = fetch.fetch, FetchDocument.close

    async def capture(url):
        result = await original_fetch(url)
        assert not isinstance(result.text, str), "Production evidence must be disk-spooled"
        assert result.text.spooled_to_disk and not result.text.closed
        assert result.text_length == len(text)
        fetched.append(result)
        return result

    def close(result):
        closed.append(result)
        original_close(result)

    fetch.fetch = AsyncMock(side_effect=capture)
    monkeypatch.setattr(FetchDocument, "close", close)
    fetch_setup.planner.plan.return_value = fetch_proposal(offset=65000, limit=16000)
    async with fetch_setup.opened(fetch=fetch) as (client, owner, _):
        if phase == "write_failure":
            write = os.write

            def fail_evidence(fd, raw):
                if bytes(raw).startswith(b"Public "):
                    raise OSError("artifact write failed")
                return write(fd, raw)

            monkeypatch.setattr(os, "write", fail_evidence)
        if phase == "cancel":
            entered, release = threading.Event(), threading.Event()
            original_write = owner.store.write_artifact

            def blocked(*args):
                entered.set()
                assert release.wait(5), "Test did not release the artifact worker"
                assert not fetched[0].text.closed
                return original_write(*args)

            monkeypatch.setattr(owner.store, "write_artifact", blocked)
        sid, run = await planned_fetch(client)
        if phase == "cancel":
            op = await owner.prepare(
                sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
            )
            stream = asyncio.create_task(consume(owner, op))
            try:
                await wait_until(entered.is_set)
                await owner.cancel(sid, run["id"], Cancel(request_id="stop", operation_id=op.id))
                assert not fetched[0].text.closed and op.id in owner.active
            finally:
                release.set()
                await stream
                await op.task
        else:
            response = await client.post(
                f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run)
            )
        assert len(fetched) == len(closed) == 1 and fetched[0] is closed[0]
        assert fetched[0].text.closed
        if phase != "success":
            records = owner.store.records[sid]
            assert not any(record.event.type == "tool.result" for record in records)
            if phase == "write_failure":
                assert owner.store.poisoned
                assert "run.completed" not in response.text
            else:
                assert records[-1].event.session.runs[0].status == "cancelled"
            return
        assert decode(response)[-1]["type"] == "run.completed"
        terminal = await run_detail(client, sid, run["id"])
        assert terminal["status"] == "completed", terminal["error"]
        result = terminal["results"][0]
        assert result["content"] == text[65000:81000]
        assert result["fetch"]["total_chars"] == len(text)
        aid = result["fetch"]["artifact_id"]
        evidence = owner.store.root / sid / "artifacts" / (aid + ".txt")
        assert evidence.read_text() == text and owner.store.artifact_bytes == len(text.encode())
        tail = await client.get(f"{BASE}/{sid}/artifacts/{aid}?offset={len(text) - 12}")
        assert tail.status_code == 200 and tail.json()["content"] == "COMPLETE-END"
        fetch.fetch.assert_awaited_once()
        resolver.assert_awaited_once()
        assert len(backend.calls) == 1 and network.closed
    async with fetch_setup.opened(fetch=fetch) as (client, owner, _):
        tail = await client.get(f"{BASE}/{sid}/artifacts/{aid}?offset={len(text) - 12}")
        assert tail.status_code == 200 and tail.json()["content"] == "COMPLETE-END"
        fetch.fetch.assert_awaited_once()


async def test_full_evidence_fsynced_before_result_and_pages_never_refetch(
    fetch_setup, monkeypatch
):
    async with fetch_setup.opened() as (client, owner, _):
        real_append, real_fsync = owner.store.append, os.fsync
        synced = []

        def fsync(fd):
            synced.append((os.fstat(fd).st_ino, stat.S_ISDIR(os.fstat(fd).st_mode)))
            return real_fsync(fd)

        def append(snapshot, kind, *args, **kwargs):
            if kind == "tool.result":
                proof = kwargs["audit"]["artifact"]
                directory = owner.store.root / snapshot.id
                evidence = directory / "artifacts" / (proof["artifact_id"] + ".txt")
                assert evidence.read_text() == TEXT
                assert (evidence.stat().st_ino, False) in synced
                assert ((directory / "artifacts").stat().st_ino, True) in synced
                assert (directory.stat().st_ino, True) in synced
                assert owner.store.records[snapshot.id][-1].event.type == "tool.intent"
            return real_append(snapshot, kind, *args, **kwargs)

        monkeypatch.setattr(os, "fsync", fsync)
        monkeypatch.setattr(owner.store, "append", append)
        sid, run, result, frames = await executed_fetch(client)
        metadata = result["fetch"]
        aid = metadata["artifact_id"]
        assert metadata == {
            "artifact_id": aid,
            "requested_url": URL,
            "final_url": URL + "/final",
            "title": "Public title",
            "status_code": 200,
            "content_type": "text/html",
            "fetched_at": "2026-10-19T10:00:00Z",
            "redirects": [URL + "/final"],
            "total_chars": len(TEXT),
            "offset": 0,
            "returned_chars": 12000,
            "next_offset": 12000,
        }
        assert result["content"] == TEXT[:12000]
        assert "tool call(s)" in frames[-1]["answer"]
        rebuilt, offset = "", 0
        while True:
            response = await client.get(f"{BASE}/{sid}/artifacts/{aid}?offset={offset}&limit=16000")
            assert response.status_code == 200
            page = response.json()
            assert set(page) == {
                "artifact_id",
                "content",
                "offset",
                "returned_chars",
                "total_chars",
                "next_offset",
            }
            assert page["offset"] == offset and page["returned_chars"] == len(page["content"])
            rebuilt += page["content"]
            if page["next_offset"] is None:
                break
            offset = page["next_offset"]
        assert rebuilt == TEXT and len(rebuilt) > 24000
        fetch_setup.fetcher.fetch.assert_awaited_once_with(URL)
        evidence = owner.store.root / sid / "artifacts" / (aid + ".txt")
        assert stat.S_IMODE(evidence.stat().st_mode) == 0o600
        assert stat.S_IMODE(evidence.parent.stat().st_mode) == 0o700
        assert owner.store.artifact_bytes == len(TEXT.encode())
        assert TEXT not in (evidence.parent.parent / "events.jsonl").read_text()


@pytest.mark.parametrize("changed", ["network", "tools", "after_intent"])
async def test_policy_binding_rechecked_before_dispatch(fetch_setup, monkeypatch, changed):
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        assert run["plan"]["access_policy"]["enabled_tools"] == "read_file,web_fetch,grep_in_folder"
        assert run["plan"]["access_policy"]["network_policy_version"] == "public-web-v1"
        if changed == "network":
            fetch_setup.fetcher.policy_version = "changed"
        elif changed == "tools":
            monkeypatch.setattr(ToolRegistry, "enabled", ("read_file",))
        else:
            original = owner.store.append

            def append(snapshot, kind, *args, **kwargs):
                result = original(snapshot, kind, *args, **kwargs)
                if kind == "tool.intent":
                    fetch_setup.fetcher.policy_version = "changed"
                return result

            monkeypatch.setattr(owner.store, "append", append)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        if changed == "after_intent":
            assert decode(response)[-1]["error"]["code"] == "policy_changed"
        else:
            assert response.status_code == 409
            assert response.json()["error"]["code"] == "policy_changed"
        fetch_setup.fetcher.fetch.assert_not_awaited()


@pytest.mark.parametrize("name", ["web_search"])
async def test_known_disabled_capability_fails_before_approval(fetch_setup, name):
    # Even an adapter bypassing Pydantic construction cannot make a stub approvable.
    proposed = fetch_proposal()
    proposed.steps[0].tool = name
    fetch_setup.planner.plan.return_value = proposed
    async with fetch_setup.opened() as (client, owner, _):
        with pytest.raises(AgentFault) as fault:
            owner.registry.require(name)
        assert fault.value.code == "unsupported_capability"
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        frames = decode(
            await client.post(
                f"{BASE}/{sid}/runs", json={"request_id": "plan", "content": "Try capability"}
            )
        )
        run = await run_detail(client, sid, frames[-1]["run_id"])
        assert run["plan"] is None and run["error"]["code"] == "unsupported_capability"
        fetch_setup.fetcher.fetch.assert_not_awaited()


async def test_network_oserror_does_not_poison_storage_or_retry(fetch_setup):
    fetch_setup.fetcher.fetch.side_effect = OSError("unsafe transport detail")
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        terminal = decode(response)[-1]["run"]
        assert terminal["status"] == "failed"
        assert terminal["error"]["code"] == "web_fetch_failed"
        assert "unsafe transport detail" not in response.text
        assert not owner.store.poisoned
        assert (await client.get(f"{BASE}/{sid}")).status_code == 200
        assert (
            await client.post(BASE, json={**CREATE, "request_id": "another"})
        ).status_code == 200
        fetch_setup.fetcher.fetch.assert_awaited_once()


@pytest.mark.parametrize("phase", ["tool.intent", "artifact", "tool.result"])
async def test_physical_storage_fault_stops_dispatch_without_unrecorded_success(
    fetch_setup, monkeypatch, phase
):
    fetch_setup.planner.plan.return_value = fetch_proposal(steps=2)
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        original_append, original_write = owner.store.append, owner.store.write_artifact

        def fail_sync(fd):
            raise OSError("physical disk failure")

        def append(snapshot, kind, *args, **kwargs):
            if kind == phase:
                with monkeypatch.context() as patch:
                    patch.setattr(os, "fsync", fail_sync)
                    return original_append(snapshot, kind, *args, **kwargs)
            return original_append(snapshot, kind, *args, **kwargs)

        def write(*args):
            with monkeypatch.context() as patch:
                patch.setattr(os, "fsync", fail_sync)
                return original_write(*args)

        monkeypatch.setattr(owner.store, "append", append)
        if phase == "artifact":
            monkeypatch.setattr(owner.store, "write_artifact", write)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert "run.completed" not in response.text and "tool.result" not in response.text
        assert owner.store.poisoned
        assert fetch_setup.fetcher.fetch.await_count == (0 if phase == "tool.intent" else 1)
        assert not any(r.event.type == "tool.result" for r in owner.store.records[sid])


@pytest.mark.parametrize("reason", ["stop", "disconnect", "shutdown"])
async def test_blocked_fetch_cleanup_retains_ownership_until_settlement(fetch_setup, reason):
    entered, cleaning, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

    async def blocked(url):
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            cleaning.set()
            await release.wait()

    fetch_setup.fetcher.fetch.side_effect = blocked
    fetch_setup.planner.plan.return_value = fetch_proposal(steps=2)
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        op = await owner.prepare(
            sid, PlanIdentity(**approval(run)), rid=run["id"], action="execute"
        )
        stream = asyncio.create_task(consume(owner, op))
        closing = None
        try:
            await asyncio.wait_for(entered.wait(), 3)
            if reason == "stop":
                _, pending = await owner.cancel(
                    sid, run["id"], Cancel(request_id="stop", operation_id=op.id)
                )
                assert pending
            elif reason == "disconnect":
                await owner.disconnect(op)
            else:
                closing = asyncio.create_task(owner.close())
            await asyncio.wait_for(cleaning.wait(), 3)
            assert op.id in owner.active and not op.task.done()
            assert owner.store.fd is not None
            assert fetch_setup.fetcher.fetch.await_count == 1
            if closing:
                assert not closing.done()
        finally:
            release.set()
            await stream
            await op.task
            if closing:
                await closing
        assert not owner.active
        assert fetch_setup.fetcher.fetch.await_count == 1
        status = owner.store.records[sid][-1].event.session.runs[0].status
        assert status == ("cancelled" if reason == "stop" else "interrupted")


async def test_legacy_reviewed_read_plan_keeps_exact_digest_and_cannot_authorize_fetch(
    fetch_setup, tmp_path
):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "note.txt").write_text("legacy read\n")
    fetch_setup.planner.plan.return_value = proposal()
    async with fetch_setup.opened() as (client, owner, _):
        owner.files = FilePolicy({"fixture": workspace})
        sid, run = await planned_fetch(client)
        snapshot = await owner.get(sid)
        plan = snapshot.runs[0].plan
        plan.access_policy = owner.files.projection()
        plan.digest = canonical_digest(
            {"session": sid, "run": run["id"], "plan": plan.model_dump(exclude={"digest"})}
        )
        legacy_digest = plan.digest
        owner.store.append(snapshot, "session.updated", "legacy-fixture")
    async with fetch_setup.opened() as (client, owner, _):
        owner.files = FilePolicy({"fixture": workspace})
        snapshot = await owner.get(sid)
        run = snapshot.runs[0].model_dump()
        assert run["plan"]["digest"] == legacy_digest
        assert "network_policy_version" not in run["plan"]["access_policy"]
        frames = decode(
            await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        )
        assert frames[-1]["type"] == "run.completed"
        assert frames[-1]["run"]["plan_identity"]["digest"] == legacy_digest
        assert not owner.registry.matches(owner.files.projection(), fetch_proposal().steps)
        fetch_setup.fetcher.fetch.assert_not_awaited()


async def test_async_fetch_race_has_no_overall_operation_timeout(fetch_setup, monkeypatch):
    clock = SimpleNamespace(value=100.0)
    monkeypatch.setattr(agent_module, "time", SimpleNamespace(monotonic=lambda: clock.value))
    fetched = []

    async def elapsed_fetch(url):
        clock.value += 3600
        result = ChunkDocument(("Independent public evidence",))
        fetched.append(result)
        return result

    fetch_setup.fetcher.fetch.side_effect = elapsed_fetch
    fetch_setup.planner.plan.return_value = fetch_proposal(steps=4)
    async with fetch_setup.opened() as (client, owner, _):
        sid, run = await planned_fetch(client)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        assert decode(response)[-1]["type"] == "run.completed"
        assert (await run_detail(client, sid, run["id"]))["completed_steps"] == 4
        assert fetch_setup.fetcher.fetch.await_count == 4 and not owner.active
        assert all(item.closed and item.close_count == 1 for item in fetched)


async def test_web_request_timeout_fails_honestly_and_settles_connections(fetch_setup):
    network = FakeStream(block="read")
    fetch, backend, resolver = http_fetcher(network, timeout=0.02)
    fetch_setup.planner.plan.return_value = fetch_proposal(steps=4)
    async with fetch_setup.opened(fetch=fetch) as (client, owner, _):
        sid, run = await planned_fetch(client)
        response = await client.post(f"{BASE}/{sid}/runs/{run['id']}/execute", json=approval(run))
        terminal = decode(response)[-1]
        assert terminal["type"] == "run.failed" and terminal["error"]["code"] == "web_timeout"
        assert "budget" not in terminal["error"]["message"].lower()
        assert terminal["result"]["status"] == "failed"
        assert network.closed and network.settled.is_set()
        assert len(backend.calls) == resolver.await_count == 1
        assert not owner.active and not owner.store.poisoned
