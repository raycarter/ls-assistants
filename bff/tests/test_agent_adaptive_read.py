"""Default main API composition: synthetic provider, real files, private durable evidence."""

import asyncio
import json
import os
import threading
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from azure.core.credentials import AccessToken
from pydantic import ValidationError

import app.main as main
from app.agent import AgentService
from app.agent_files import FilePolicy, canonical_digest
from app.agent_read_chunks import FileChunkReader
from app.agent_schemas import StepResult
from app.llm_schemas import LLMRecord
from app.sessions import SessionStore
from tests.test_agent import BASE, approval, decode, run_detail
from tests.test_agent_responses_api import CREATE, PRIVATE, TOKEN, audits, provider_body

PARTIAL_COVERAGE_NOTE = (
    "Coverage note: A file read stopped before EOF. "
    "This answer is based on partial file content.\n\n"
)
FETCH_METADATA = {
    "artifact_id": "a" * 32,
    "requested_url": "https://example.com/synthetic",
    "final_url": "https://example.com/synthetic",
    "title": None,
    "status_code": 200,
    "content_type": "text/plain",
    "fetched_at": "2026-10-26T00:00:00Z",
    "redirects": [],
    "total_chars": 1,
    "offset": 0,
    "returned_chars": 1,
    "next_offset": None,
}


def step_result_payload():
    return {
        "step_id": "synthetic-step",
        "status": "completed",
        "content": "Synthetic accumulated findings",
        "time": "2026-10-26T00:00:00Z",
        "path": "notes/note.txt",
        "truncated": False,
        "read": {
            "chunks": 1,
            "bytes_read": 10,
            "total_bytes": 10,
            "eof": True,
            "stop_reason": "eof",
            "file_version": "synthetic-version",
        },
    }


@pytest.mark.parametrize(
    "path,bytes_read,total_bytes,eof,reason",
    [
        ("note.txt", 10, 10, True, "eof"),
        ("notes/note.txt", 0, 0, True, "eof"),
        ("原文🌱.txt", 4, 10, False, "enough"),
        ("notes..txt", 0, 10, False, "enough"),
    ],
)
def test_adaptive_step_result_valid_coverage(path, bytes_read, total_bytes, eof, reason):
    payload = step_result_payload()
    payload.update(path=path, truncated=not eof, fetch=None, offset=None, returned_lines=None)
    payload["read"].update(
        bytes_read=bytes_read, total_bytes=total_bytes, eof=eof, stop_reason=reason
    )
    result = StepResult.model_validate(payload)
    assert result.model_dump()["read"] == payload["read"]
    assert result.path == path and result.truncated == (not result.read.eof)
    assert result.fetch is result.offset is result.returned_lines is None
    assert StepResult.model_validate_json(result.model_dump_json()) == result


@pytest.mark.parametrize(
    "metadata,coverage",
    [
        pytest.param({"path": None}, {}, id="missing-path"),
        pytest.param({"path": ""}, {}, id="empty-path"),
        pytest.param({"path": "/note.txt"}, {}, id="absolute-path"),
        pytest.param({"path": "../note.txt"}, {}, id="parent-path"),
        pytest.param({"path": "notes/../note.txt"}, {}, id="nested-parent-path"),
        pytest.param({"fetch": FETCH_METADATA}, {}, id="fetch-with-read"),
        pytest.param({"offset": 0}, {}, id="offset-zero"),
        pytest.param({"offset": 41}, {}, id="offset-positive"),
        pytest.param({"returned_lines": 0}, {}, id="returned-lines-zero"),
        pytest.param({"returned_lines": 1}, {}, id="returned-lines-positive"),
        pytest.param({"truncated": True}, {}, id="eof-truncated"),
        pytest.param(
            {}, {"bytes_read": 4, "eof": False, "stop_reason": "enough"}, id="partial-not-truncated"
        ),
        pytest.param({}, {"bytes_read": 11}, id="bytes-past-total"),
        pytest.param({}, {"bytes_read": 4}, id="eof-before-total"),
        pytest.param(
            {"truncated": True}, {"eof": False, "stop_reason": "enough"}, id="enough-at-total"
        ),
        pytest.param({}, {"stop_reason": "enough"}, id="eof-with-enough-reason"),
        pytest.param(
            {"truncated": True}, {"bytes_read": 4, "eof": False}, id="partial-with-eof-reason"
        ),
        pytest.param({}, {"bytes_read": -1}, id="negative-bytes"),
        pytest.param({}, {"total_bytes": -1}, id="negative-total"),
        pytest.param({}, {"chunks": 0}, id="no-chunks"),
        pytest.param({}, {"file_version": ""}, id="empty-version"),
    ],
)
def test_adaptive_step_result_rejects_inconsistent_metadata(metadata, coverage):
    payload = step_result_payload()
    payload.update(metadata)
    payload["read"].update(coverage)
    with pytest.raises(ValidationError):
        StepResult.model_validate(payload)


@pytest.mark.parametrize("read_field", [{}, {"read": None}], ids=["absent", "null"])
@pytest.mark.parametrize(
    "metadata",
    [
        {"path": None, "offset": None, "returned_lines": None, "truncated": False},
        {"path": "note.txt", "offset": 41, "returned_lines": 1, "truncated": True},
        {"path": "/legacy/note.txt", "offset": 0, "returned_lines": 0, "truncated": False},
        {"path": None, "fetch": FETCH_METADATA, "truncated": False},
    ],
    ids=["running-style", "legacy-page", "legacy-path", "fetch"],
)
def test_legacy_step_result_without_read_retains_metadata(read_field, metadata):
    payload = step_result_payload()
    payload.pop("read")
    payload.update(metadata)
    payload.update(read_field)
    result = StepResult.model_validate(payload)
    assert result.read is None and "read" not in result.model_dump()
    assert {key: result.model_dump()[key] for key in metadata} == metadata
    assert StepResult.model_validate_json(result.model_dump_json()) == result


def response_body(value):
    body = provider_body()
    body["output"][1]["content"] = [
        {"type": "output_text", "text": value if isinstance(value, str) else json.dumps(value)}
    ]
    return body


@pytest.fixture
def adaptive(tmp_path, monkeypatch, settings_factory, mock_client):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # First 16000-byte boundary splits a four-byte UTF-8 codepoint inside a long line.
    source = "x" * 15999 + "🌱" * 9000 + "\n" + "tail 原文\n" * 60
    (workspace / "note.txt").write_text(source, encoding="utf-8")
    (workspace / "second.txt").write_text("second synthetic file", encoding="utf-8")
    files = FilePolicy({"fixture": workspace})
    state = SimpleNamespace(
        workspace=workspace,
        source=source,
        files=files,
        calls=[],
        decisions=[],
        syntheses=[],
        paths=["note.txt"],
        reads=[],
        released=[],
        decision=lambda data: {
            "decision": "continue",
            "findings": data["findings"] + f"Evidence through {data['chunk']['end_byte']}. ",
        },
    )
    next_chunk, close = FileChunkReader.next_chunk, FileChunkReader.close

    def observed_read(reader, *args):
        state.reads.append(reader)
        return next_chunk(reader, *args)

    def observed_close(reader):
        try:
            return close(reader)
        finally:
            state.released.append(reader)

    monkeypatch.setattr(FileChunkReader, "next_chunk", observed_read)
    monkeypatch.setattr(FileChunkReader, "close", observed_close)

    def service(**kwargs):
        assert kwargs["files"] is files
        assert kwargs["read_assessor"] is not None
        state.owner = AgentService(store=SessionStore(tmp_path / "journal"), **kwargs)
        return state.owner

    async def dispatch(request):
        wire = json.loads(request.content)
        state.calls.append(wire)
        assert request.headers["authorization"] == "Bearer " + TOKEN
        assert wire["model"] == "deployment-b" and wire["reasoning"] == {"effort": "xhigh"}
        assert wire["stream"] is wire["store"] is False
        assert not {"tools", "tool_choice", "previous_response_id"} & wire.keys()
        name = wire["text"]["format"].get("name")
        if name == "agent_read_assessment_v1":
            data = json.loads(wire["input"][0]["content"])
            (sid,) = state.owner.store.records
            durable = chunks(state.owner, sid)[-1]
            assert data["chunk"] == {key: durable[key] for key in data["chunk"]}
            state.decisions.append(data)
            value = state.decision(data)
            if asyncio.iscoroutine(value):
                value = await value
            return (
                value
                if isinstance(value, httpx.Response)
                else httpx.Response(200, json=response_body(value))
            )
        if wire["text"]["format"]["type"] == "text":
            state.syntheses.append(json.loads(wire["input"][0]["content"]))
            return httpx.Response(200, json=response_body("Synthetic final answer"))
        proposal = {
            "objective": "Summarize the entire approved files",
            "assumptions": [],
            "steps": [
                {
                    "title": "Read",
                    "description": "Read the entire named file",
                    "tool": "read_file",
                    "arguments": {
                        "workspace_id": "fixture",
                        "path": path,
                        "offset": 41,
                        "limit": 1,
                    },
                }
                for path in state.paths
            ],
        }
        return httpx.Response(200, json=response_body(proposal))

    def http_factory(**kwargs):
        state.http = httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch), trust_env=False, **kwargs
        )
        return state.http

    monkeypatch.setattr(main, "build_file_policy", Mock(return_value=files))
    monkeypatch.setattr(main, "AgentService", service)
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken(TOKEN, 4102444800)), close=AsyncMock()
    )

    @asynccontextmanager
    async def opened():
        app = main.create_app(
            settings_factory(),
            credential_factory=lambda settings: credential,
            client_factory=lambda **kwargs: mock_client,
            agent_http_client_factory=http_factory,
        )
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app), base_url="http://test", trust_env=False
            ) as client:
                yield client, state.owner
        assert state.http.is_closed
        assert set(state.reads) <= set(state.released)
        assert all(reader._fd is None for reader in state.reads)

    state.opened = opened
    return state


async def planned(client):
    created = await client.post(BASE, json=CREATE)
    assert created.status_code == 200
    sid = created.json()["id"]
    frames = decode(
        await client.post(
            f"{BASE}/{sid}/runs",
            json={
                "request_id": "plan",
                "content": "Summarize the entire fixture/note.txt from its start",
            },
        )
    )
    run = await run_detail(client, sid, frames[-1]["run_id"])
    assert run["status"] == "awaiting_approval"
    return sid, run


def execution(sid, run):
    return f"{BASE}/{sid}/runs/{run['id']}/execute"


def chunks(owner, sid):
    return [r.audit for r in owner.store.records[sid] if r.event.type == "tool.chunk"]


def assert_unpublished(run):
    assert run["status"] in {"failed", "cancelled", "interrupted", "executing"}
    assert run["answer"] is None
    assert all(r["status"] != "completed" for r in run["results"])


async def test_entire_file_utf8_cursor_private_audit_and_fresh_assessment_identities(adaptive):
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        policy = run["plan"]["access_policy"]
        assert policy["read_semantics"] == "file-scoped-v1"
        assert policy["tool_registry_version"] == "agent-tools-v4"
        assert adaptive.decisions == [] and chunks(owner, sid) == []
        stale = {**approval(run, "stale-digest"), "digest": "0" * 64}
        assert (await client.post(execution(sid, run), json=stale)).status_code == 409
        assert not adaptive.reads and len(adaptive.calls) == 1
        transport = owner.read_assessor.transport
        assert (
            transport.client is owner.planner.transport.client is owner.synthesizer.transport.client
        )
        assert transport.bearer_provider is owner.planner.transport.bearer_provider
        response = await client.post(execution(sid, run), json=approval(run))
        frames = decode(response)
        done = await run_detail(client, sid, run["id"])
        assert done["status"] == "completed" and frames[-1]["type"] == "run.completed"
        material = chunks(owner, sid)
        assert len(material) == len(adaptive.decisions) == len(adaptive.reads) > 1
        assert "".join(c["content"] for c in material) == adaptive.source
        assert material[0]["start_byte"] == 0 and material[0]["end_byte"] == 15999
        cursor, findings = 0, ""
        for number, (chunk, decision) in enumerate(
            zip(material, adaptive.decisions, strict=True), 1
        ):
            assert chunk["chunk"] == number
            assert chunk["start_byte"] == cursor
            assert len(chunk["content"].encode()) <= 16000
            assert chunk["end_byte"] - cursor == len(chunk["content"].encode())
            assert decision["chunk"] == {k: chunk[k] for k in decision["chunk"]}
            assert decision["approved_plan"] == run["plan"] and decision["findings"] == findings
            cursor = chunk["end_byte"]
            findings += f"Evidence through {cursor}. "
        result = done["results"][0]
        assert result["content"] == findings and not result["truncated"]
        assert result["path"] == "note.txt"
        assert result["offset"] is result["returned_lines"] is None
        assert result["read"] == {
            "chunks": len(material),
            "bytes_read": cursor,
            "total_bytes": len(adaptive.source.encode()),
            "eof": True,
            "stop_reason": "eof",
            "file_version": material[0]["file_version"],
        }
        assert len({c["file_version"] for c in material}) == 1
        assert adaptive.syntheses[0]["results"] == done["results"]
        groups = audits(owner, sid)
        assessments = groups[1:-1]
        assert len(assessments) == len(material)
        identities = [g[0]["identity"] for g in assessments]
        assert len({i["stage_id"] for i in identities}) == 1
        for key in ("interaction_id", "attempt_id"):
            assert len({i[key] for i in identities}) == len(material)
        approved = next(r for r in owner.store.records[sid] if r.event.type == "run.approved")
        for group in assessments:
            assert [r["kind"] for r in group] == [
                "intent",
                "dispatched",
                "output",
                "outcome",
                "validation",
            ]
            identity = group[0]["identity"]
            assert (
                identity["phase"] == "execution"
                and identity["step_id"] == run["plan"]["steps"][0]["id"]
            )
            assert identity["operation_id"] == approved.event.operation_id
            assert identity["session_id"] == sid and identity["run_id"] == run["id"]
            assert identity["plan_id"] == run["plan"]["id"] and identity["revision"] == 1
            assert identity["attempt"] == 1 and identity["retry_of"] is None
            for row in group:
                LLMRecord.model_validate(row)
                assert row["identity"] == identity
        synth = groups[-1][0]["identity"]
        assert synth["phase"] == "synthesis" and synth["stage_id"] == approved.audit["stage_id"]
        assert synth["stage_id"] != identities[0]["stage_id"]
        assert sum(r.event.type == "run.approved" for r in owner.store.records[sid]) == 1
        public = response.text + json.dumps(done)
        assert PRIVATE not in public and adaptive.source[:200] not in public
        for frame in frames:
            if frame["type"] == "tool.chunk":
                assert frame["result"] is None
                assert not {"audit", "content", "chunk"} & frame.keys()
        assert (
            await client.post(execution(sid, run), json=approval(run, "again"))
        ).status_code == 409


@pytest.mark.parametrize(
    "source,decision,reason",
    [
        (None, "enough", "enough"),
        ("short 原文🌱", "continue", "eof"),
        ("", "continue", "eof"),
    ],
)
async def test_early_enough_and_eof_override_continue(adaptive, source, decision, reason):
    if source is not None:
        (adaptive.workspace / "note.txt").write_text(source, encoding="utf-8")
    adaptive.decision = lambda data: {
        "decision": decision,
        "findings": "Only observed evidence; partial if not EOF.",
    }
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        frames = decode(await client.post(execution(sid, run), json=approval(run)))
        done = await run_detail(client, sid, run["id"])
        assert done["status"] == "completed"
        expected_answer = (
            PARTIAL_COVERAGE_NOTE if reason == "enough" else ""
        ) + "Synthetic final answer"
        assert done["answer"] == frames[-1]["answer"] == expected_answer
        result = done["results"][0]
        read = result["read"]
        assert len(adaptive.decisions) == len(chunks(owner, sid)) == read["chunks"] == 1
        assert len(adaptive.reads) == 1
        assert read["stop_reason"] == reason and read["eof"] == (reason == "eof")
        assert result["truncated"] == (reason == "enough")
        assert read["bytes_read"] == chunks(owner, sid)[0]["end_byte"]
        assert read["total_bytes"] == len((adaptive.source if source is None else source).encode())
        assert read["file_version"] == chunks(owner, sid)[0]["file_version"]
        assert result["content"] == "Only observed evidence; partial if not EOF."
        assert adaptive.syntheses[0]["results"] == [result]
        synthesis = audits(owner, sid)[-1]
        assert synthesis[0]["data"]["instructions_version"] == "agent-responses-synthesis-v4"
        assert adaptive.syntheses[0]["results"][0]["read"]["stop_reason"] == reason
        assert len(adaptive.calls) == 3


async def test_multiple_files_have_separate_stages_and_findings(adaptive):
    adaptive.paths.append("second.txt")
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        decode(await client.post(execution(sid, run), json=approval(run)))
        done = await run_detail(client, sid, run["id"])
        assert done["status"] == "completed" and len(done["results"]) == 2
        groups = audits(owner, sid)[1:-1]
        stages = {}
        for group in groups:
            identity = group[0]["identity"]
            stages.setdefault(identity["step_id"], set()).add(identity["stage_id"])
        assert len(stages) == 2 and all(len(s) == 1 for s in stages.values())
        assert len(set.union(*stages.values())) == 2
        assert adaptive.decisions[-1]["findings"] == ""
        assert adaptive.decisions[-1]["chunk"]["start_byte"] == 0
        assert adaptive.decisions[-1]["chunk"]["content"] == "second synthetic file"


@pytest.mark.parametrize(
    "failure,code",
    [
        ("malformed", "invalid_read_decision"),
        ("path", "invalid_read_decision"),
        ("refusal", "read_assessment_unavailable"),
        ("timeout", "read_assessment_timeout"),
        ("http", "read_assessment_unavailable"),
        ("failed", "read_assessment_unavailable"),
    ],
)
async def test_decision_failure_never_advances_or_synthesizes(adaptive, failure, code):
    def decide(data):
        if failure == "timeout":
            raise httpx.ReadTimeout("SYNTHETIC_TIMEOUT")
        if failure == "http":
            return httpx.Response(503, json={"error": PRIVATE})
        body = response_body({"decision": "continue", "findings": "observed"})
        if failure == "malformed":
            body = response_body({"decision": "next", "findings": ""})
        elif failure == "path":
            body = response_body(
                {"decision": "continue", "findings": "observed", "path": "second.txt"}
            )
        elif failure == "refusal":
            body["output"][1]["content"] = [{"type": "refusal", "refusal": PRIVATE}]
        else:
            body["status"] = "failed"
        return httpx.Response(200, json=body)

    adaptive.decision = decide
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        response = await client.post(execution(sid, run), json=approval(run))
        decode(response)
        failed = await run_detail(client, sid, run["id"])
        assert_unpublished(failed)
        assert failed["error"]["code"] == code
        assert len(chunks(owner, sid)) == len(adaptive.decisions) == 1
        assert len(adaptive.reads) == 1
        assert not adaptive.syntheses and len(adaptive.calls) == 2
        if code == "invalid_read_decision":
            assert audits(owner, sid)[-1][-1]["data"]["status"] == "invalid"
        assert PRIVATE not in response.text and "SYNTHETIC_TIMEOUT" not in response.text


@pytest.mark.parametrize("kind", ["tool.chunk", "llm.output", "llm.validation"])
async def test_recording_failure_blocks_next_read_and_publication(adaptive, monkeypatch, kind):
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        write = owner.store._write
        attempted = []

        def fail(record, *args):
            if record.event.type == kind:
                attempted.append(record)
                raise OSError("SYNTHETIC_DISK_FAILURE")
            return write(record, *args)

        monkeypatch.setattr(owner.store, "_write", fail)
        response = await client.post(execution(sid, run), json=approval(run))
        assert len(attempted) == 1 and owner.store.poisoned
        assert len(adaptive.reads) == 1
        durable = owner.store.records[sid][-1].event.session.runs[0]
        assert_unpublished(durable.model_dump())
        assert not adaptive.syntheses
        assert len(adaptive.decisions) == (0 if kind == "tool.chunk" else 1)
        assert len(chunks(owner, sid)) == (0 if kind == "tool.chunk" else 1)
        assert "SYNTHETIC_DISK_FAILURE" not in response.text
        assert (
            await client.post(execution(sid, run), json=approval(run, "retry"))
        ).status_code == 503


@pytest.mark.parametrize("boundary", ["tool.intent", "tool.chunk", "llm.validation"])
async def test_stop_at_boundaries_prevents_next_dispatch(adaptive, monkeypatch, boundary):
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        if boundary == "llm.validation":
            original = owner.record_llm

            async def record(op, kind, audit):
                await original(op, kind, audit)
                if kind == boundary:
                    await stop(op)

            monkeypatch.setattr(owner, "record_llm", record)
        else:
            original = owner.transition

            async def transition(op, kind, *args, **kwargs):
                result = await original(op, kind, *args, **kwargs)
                if kind == boundary:
                    await stop(op)
                return result

            monkeypatch.setattr(owner, "transition", transition)

        async def stop(op):
            response = await client.post(
                f"{BASE}/{sid}/runs/{run['id']}/cancel",
                json={"request_id": "stop", "operation_id": op.id},
            )
            assert response.status_code == 202

        decode(await client.post(execution(sid, run), json=approval(run)))
        stopped = await run_detail(client, sid, run["id"])
        assert stopped["status"] == "cancelled"
        assert_unpublished(stopped)
        assert len(chunks(owner, sid)) == (0 if boundary == "tool.intent" else 1)
        assert len(adaptive.reads) == (0 if boundary == "tool.intent" else 1)
        assert len(adaptive.decisions) == (1 if boundary == "llm.validation" else 0)
        assert not adaptive.syntheses and not owner.active


@pytest.mark.parametrize("mutation", ["modify", "replace"])
async def test_changed_file_is_not_published_or_switched(adaptive, mutation):
    def decide(data):
        path = adaptive.workspace / "note.txt"
        if mutation == "replace":
            path.unlink()
        path.write_text("different synthetic file", encoding="utf-8")
        return {"decision": "enough", "findings": "must not publish"}

    adaptive.decision = decide
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        decode(await client.post(execution(sid, run), json=approval(run)))
        failed = await run_detail(client, sid, run["id"])
        assert_unpublished(failed)
        assert failed["error"]["code"] == "file_changed"
        assert len(adaptive.decisions) == len(chunks(owner, sid)) == 1
        assert not adaptive.syntheses


async def test_old_exact_policy_cannot_authorize_adaptive_read(adaptive):
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        # A retained old plan remains internally digest-valid; it still cannot gain new semantics.
        snapshot = owner.store.get(sid)
        stored = snapshot.runs[0].plan
        stored.access_policy.pop("read_semantics")
        stored.access_policy["tool_registry_version"] = "agent-tools-v1"
        material = stored.model_dump(exclude={"digest"})
        stored.digest = canonical_digest({"session": sid, "run": run["id"], "plan": material})
        assert stored.digest != run["plan"]["digest"]
        owner.store.append(
            snapshot, "run.awaiting_approval", "synthetic-old-plan", run_id=run["id"]
        )
        old = await run_detail(client, sid, run["id"])
        response = await client.post(execution(sid, old), json=approval(old))
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "policy_changed"
        assert not adaptive.decisions and not chunks(owner, sid)
        assert not adaptive.reads


@pytest.mark.parametrize("ending", ["stop", "cancel", "deadline"])
async def test_inflight_decision_is_owned_and_closed(adaptive, ending):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"synthetic_partial":'
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    adaptive.decision = lambda data: httpx.Response(200, stream=Stream())
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        if ending == "deadline":
            owner.planner_timeout_seconds = 0.05
        pending = asyncio.create_task(client.post(execution(sid, run), json=approval(run)))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            (op,) = owner.active.values()
            if ending == "stop":
                assert (
                    await client.post(
                        f"{BASE}/{sid}/runs/{run['id']}/cancel",
                        json={"request_id": "stop", "operation_id": op.id},
                    )
                ).status_code == 202
            elif ending == "cancel":
                op.task.cancel()
            await asyncio.wait_for(asyncio.shield(pending), 3)
            await asyncio.wait_for(asyncio.gather(op.task, return_exceptions=True), 3)
            assert closed.is_set() and not owner.active
            failed = await run_detail(client, sid, run["id"])
            assert_unpublished(failed)
            assert (
                failed["status"]
                == {"stop": "cancelled", "cancel": "interrupted", "deadline": "failed"}[ending]
            )
            assert len(adaptive.decisions) == len(chunks(owner, sid)) == 1
            assert not adaptive.syntheses
            group = audits(owner, sid)[-1]
            assert any(r["kind"] == "outcome" for r in group)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_repeated_native_cancellation_drains_chunk_worker_before_fd_close(
    adaptive, monkeypatch
):
    entered, release = threading.Event(), threading.Event()
    readers, descriptors, closed = [], [], []
    next_chunk, close = FileChunkReader.next_chunk, FileChunkReader.close

    def held_read(reader, *args):
        chunk = next_chunk(reader, *args)
        readers.append(reader)
        descriptors.append(reader._fd)
        entered.set()
        assert release.wait(3), "Read worker barrier was not released"
        return chunk

    def tracked_close(reader):
        assert release.is_set(), "Descriptor closed while chunk worker was still owned"
        close(reader)
        closed.append(reader)

    monkeypatch.setattr(FileChunkReader, "next_chunk", held_read)
    monkeypatch.setattr(FileChunkReader, "close", tracked_close)
    async with adaptive.opened() as (client, owner):
        sid, run = await planned(client)
        pending = asyncio.create_task(client.post(execution(sid, run), json=approval(run)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            (op,) = owner.active.values()
            for _ in range(2):
                op.task.cancel()
                await asyncio.sleep(0)
            assert not closed and not pending.done()
            os.fstat(descriptors[0])
            release.set()
            await asyncio.wait_for(asyncio.shield(pending), 3)
            assert closed == readers and readers[0]._fd is None
            with pytest.raises(OSError):
                os.fstat(descriptors[0])
            assert not adaptive.decisions and not adaptive.syntheses and not owner.active
            assert_unpublished(await run_detail(client, sid, run["id"]))
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)
