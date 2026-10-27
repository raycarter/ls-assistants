"""Default application grep flow, with offline provider HTTP and temporary files only."""

import asyncio
import json
import os
import threading
from unittest.mock import Mock

import httpx
import pytest

import app.agent as agent_module
from app import agent_grep
from app.agent_schemas import GrepArguments
from app.tools.registry import ToolRegistry
from tests.test_agent import BASE, approval, decode, run_detail
from tests.test_agent_responses_api import audits, plan, provider_body
from tests.test_agent_responses_api import wired as wired  # explicit fixture re-export
from tests.test_agent_synthesis_api import ANSWER, answer_body, execute_path

COVERAGE_NOTE = (
    "Coverage note: A folder search skipped files or supplied only an excerpt "
    "of its retained matches to synthesis. This answer is not based on complete "
    "search evidence.\n\n"
)


@pytest.fixture
def grep_api(wired, tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    wired.files.roots["fixture"] = root
    wired.workspace = root
    wired.arguments = GrepArguments(workspace_id="fixture", pattern="needle").model_dump()

    def handler(request):
        if len(wired.calls) == 1:
            body = provider_body()
            proposal = {
                "objective": "Search the exact approved folder",
                "assumptions": [],
                "steps": [
                    {
                        "title": "Search",
                        "description": "Find literal matching lines",
                        "tool": "grep_in_folder",
                        "arguments": dict(wired.arguments),
                    }
                ],
            }
            body["output"][1]["content"][0]["text"] = json.dumps(proposal)
            return httpx.Response(200, json=body)
        return httpx.Response(200, json=answer_body())

    wired.handler = handler
    return wired


async def execute(client, sid, run):
    response = await client.post(execute_path(sid, run), json=approval(run))
    return response, await run_detail(client, sid, run["id"])


async def retained(client, sid, metadata):
    chunks, offset = [], 0
    while True:
        response = await client.get(
            f"{BASE}/{sid}/artifacts/{metadata['artifact_id']}?offset={offset}&limit=997"
        )
        assert response.status_code == 200
        page = response.json()
        assert page["offset"] == offset
        assert page["returned_chars"] == len(page["content"]) <= 997
        assert page["total_chars"] == metadata["total_chars"]
        chunks.append(page["content"])
        if page["next_offset"] is None:
            break
        assert page["next_offset"] == offset + page["returned_chars"]
        offset = page["next_offset"]
    return "".join(chunks)


async def test_default_planning_approval_real_search_synthesis_full_paging_restart(
    grep_api, monkeypatch
):
    fixture = grep_api
    literal = '[a-z]+.*(é雪)"<&'
    long_line = "x" * 17000 + literal + "🌱" * 17000 + "\\end"
    (fixture.workspace / "src").mkdir()
    target = fixture.workspace / "src" / "文.txt"
    target.write_text(f"{literal.upper()}\n{long_line}\n{literal}\n")
    (fixture.workspace / "outside.txt").write_text(literal)
    (fixture.workspace / "src" / "filtered.py").write_text(literal)
    fixture.arguments.update(path="src", pattern=literal, file_glob="*.txt", offset=17, limit=137)
    scan = Mock(wraps=agent_module.scan_folder)
    monkeypatch.setattr(agent_module, "scan_folder", scan)
    async with fixture.opened() as (client, owner, _):
        # Only source workspace access is guarded; journal storage remains real.
        original_open, original_scan = os.open, os.scandir

        def no_source_open(name, *args, **kwargs):
            assert str(name) != str(fixture.workspace)
            return original_open(name, *args, **kwargs)

        def no_source_scan(fd):
            assert os.fstat(fd).st_ino != fixture.workspace.stat().st_ino
            return original_scan(fd)

        with monkeypatch.context() as guard:
            guard.setattr(os, "open", no_source_open)
            guard.setattr(os, "scandir", no_source_scan)
            sid, run, _ = await plan(client)
        assert run["status"] == "awaiting_approval"
        assert run["plan"]["steps"][0]["arguments"] == fixture.arguments
        assert run["plan"]["access_policy"]["tool_registry_version"] == "agent-tools-v3-exact"
        scan.assert_not_called()
        fixture.files.read.assert_not_called()
        fixture.fetcher.fetch.assert_not_awaited()
        planning = audits(owner, sid)[0][0]["data"]
        assert planning["instructions_version"] == "agent-responses-planning-v4"
        response, completed = await execute(client, sid, run)
        assert decode(response)[-1]["type"] == "run.completed"
        assert completed["answer"] == COVERAGE_NOTE + ANSWER
        (result,) = completed["results"]
        metadata = result["grep"]
        assert result["status"] == "completed" and result["truncated"] is True
        assert result.get("fetch") is None
        assert {key: metadata[key] for key in ("workspace_id", "path", "pattern", "file_glob")} == {
            key: fixture.arguments[key] for key in ("workspace_id", "path", "pattern", "file_glob")
        }
        assert metadata["complete"] is True
        assert metadata["total_matches"] == 2 and metadata["files_searched"] == 1
        assert metadata["skipped_binary"] == metadata["skipped_unreadable"] == 0
        text = await retained(client, sid, metadata)
        assert [json.loads(line) for line in text.splitlines()] == [
            {"path": "src/文.txt", "line": 2, "text": long_line},
            {"path": "src/文.txt", "line": 3, "text": literal},
        ]
        assert result["content"] == text[17:154]
        assert metadata["next_offset"] == 154
        assert metadata["returned_chars"] == 137
        assert metadata["total_chars"] == len(text) > 34000
        scan.assert_called_once()
        assert scan.call_args.args[1].model_dump() == fixture.arguments
        evidence = json.loads(json.loads(fixture.calls[1].content)["input"][0]["content"])
        assert evidence["approved_plan"] == run["plan"]
        assert evidence["results"] == completed["results"]
        records = owner.store.records[sid]
        intent = next(r for r in records if r.event.type == "tool.intent")
        assert intent.audit["arguments"] == fixture.arguments
        committed = next(r for r in records if r.event.type == "tool.result")
        assert committed.audit["artifact"]["artifact_id"] == metadata["artifact_id"]
        fixture.files.read.assert_not_called()  # Matching paths do not authorize file reads.
        assert len(fixture.calls) == 2
    target.unlink()
    fixture.handler = lambda request: pytest.fail("Restart must not replay provider calls")
    async with fixture.opened() as (client, owner, _):
        assert await run_detail(client, sid, run["id"]) == completed
        assert await retained(client, sid, metadata) == text
        retry = await client.post(execute_path(sid, run), json=approval(run, "restart"))
        assert retry.status_code == 409
        scan.assert_called_once()
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 2


async def test_skipped_files_report_incomplete_coverage_without_hidden_reads(grep_api, monkeypatch):
    fixture = grep_api
    (fixture.workspace / "ok.txt").write_text("needle good\nNEEDLE\n")
    (fixture.workspace / "binary.txt").write_bytes(b"needle\n\0")
    (fixture.workspace / "unreadable.txt").write_text("needle private")
    original = os.open

    def denied(name, *args, **kwargs):
        if name == "unreadable.txt":
            raise PermissionError("PRIVATE_OPEN_SECRET")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(os, "open", denied)
    async with fixture.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        response, completed = await execute(client, sid, run)
        result = completed["results"][0]
        assert result["status"] == "completed" and result["truncated"] is True
        metadata = result["grep"]
        assert metadata["complete"] is False
        assert metadata["skipped_binary"] == metadata["skipped_unreadable"] == 1
        assert metadata["files_searched"] == metadata["total_matches"] == 1
        assert metadata["next_offset"] is None
        assert json.loads(result["content"]) == {"path": "ok.txt", "line": 1, "text": "needle good"}
        evidence = json.loads(json.loads(fixture.calls[1].content)["input"][0]["content"])
        assert evidence["results"][0] == result
        instructions = json.loads(fixture.calls[1].content)["instructions"]
        assert "Acknowledge truncation" in instructions
        assert "PRIVATE_OPEN_SECRET" not in response.text + json.dumps(evidence)
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 2


@pytest.mark.parametrize(
    "coverage",
    [
        "skipped",
        "final_page",
        "first_excerpt",
        "complete_matches",
        "complete_no_matches",
    ],
)
async def test_backend_coverage_warning_overrides_confident_claim_and_survives_restart(
    grep_api, coverage
):
    fixture = grep_api
    no_matches = coverage in {"skipped", "complete_no_matches"}
    (fixture.workspace / "ok.txt").write_text("unrelated" if no_matches else "needle found")
    if coverage == "skipped":
        (fixture.workspace / "binary.txt").write_bytes(b"needle hidden\n\0")
    elif coverage == "final_page":
        fixture.arguments["offset"] = 1
    elif coverage == "first_excerpt":
        fixture.arguments["limit"] = 1
    claim = (
        "I searched every file completely. There are no matches anywhere."
        if no_matches
        else "I reviewed all matching evidence in full."
    )
    raw_body = answer_body(claim)
    planning_handler = fixture.handler
    fixture.handler = lambda request: (
        planning_handler(request) if len(fixture.calls) == 1 else httpx.Response(200, json=raw_body)
    )
    warned = coverage in {"skipped", "final_page", "first_excerpt"}
    expected = (COVERAGE_NOTE if warned else "") + claim
    async with fixture.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        response, completed = await execute(client, sid, run)
        final = decode(response)[-1]
        assert final["type"] == "run.completed"
        assert final["answer"] == completed["answer"] == expected
        result = completed["results"][0]
        metadata = result["grep"]
        assert metadata["total_matches"] == (0 if no_matches else 1)
        assert metadata["complete"] is (coverage != "skipped")
        assert metadata["skipped_binary"] == (1 if coverage == "skipped" else 0)
        assert metadata["offset"] == (1 if coverage == "final_page" else 0)
        assert (metadata["next_offset"] is not None) is (coverage == "first_excerpt")
        assert result["truncated"] is (coverage in {"skipped", "first_excerpt"})
        if not warned:
            assert not completed["answer"].startswith("Coverage note:")
        synthesis = audits(owner, sid)[1]
        assert synthesis[0]["data"]["instructions_version"] == "agent-responses-synthesis-v3"
        output = next(row["data"] for row in synthesis if row["kind"] == "output")
        assert output["body"] == raw_body
        assert output["body"]["output"][1]["content"][0]["text"] == claim
        committed = next(r for r in owner.store.records[sid] if r.event.type == "run.completed")
        assert committed.event.session.runs[0].answer == expected
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 2
    fixture.handler = lambda request: pytest.fail("Restart must not synthesize again")
    async with fixture.opened() as (client, owner, _):
        assert await run_detail(client, sid, run["id"]) == completed
        assert audits(owner, sid)[1] == synthesis
        fixture.files.read.assert_not_called()
        assert len(fixture.calls) == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "../outside"),
        ("path", "/absolute"),
        ("path", ".env"),
        ("pattern", ""),
        ("pattern", "a\nb"),
        ("file_glob", "../*.txt"),
        ("workspace_id", "unknown"),
    ],
)
async def test_invalid_scope_fails_planning_without_source_access(
    grep_api, monkeypatch, field, value
):
    grep_api.arguments[field] = value
    scan = Mock(side_effect=AssertionError("Invalid scope must not scan"))
    monkeypatch.setattr(agent_module, "scan_folder", scan)
    async with grep_api.opened() as (client, owner, _):
        sid, run, response = await plan(client)
        assert run["status"] == "failed" and run["plan"] is None
        assert not any(frame["type"] == "run.completed" for frame in decode(response))
        scan.assert_not_called()
        grep_api.files.read.assert_not_called()
        assert len(grep_api.calls) == 1


@pytest.mark.parametrize("path", ["missing", "file.txt", "alias"])
async def test_unavailable_selected_folder_never_zero_match_success(grep_api, path):
    (grep_api.workspace / "file.txt").write_text("needle")
    (grep_api.workspace / "alias").symlink_to(grep_api.workspace, target_is_directory=True)
    grep_api.arguments["path"] = path
    async with grep_api.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        assert run["status"] == "awaiting_approval"
        response, failed = await execute(client, sid, run)
        assert failed["status"] == "failed" and failed["answer"] is None
        assert failed["results"][0]["status"] == "failed"
        assert failed["results"][0].get("grep") is None
        assert decode(response)[-1]["type"] == "run.failed"
        grep_api.files.read.assert_not_called()
        assert len(grep_api.calls) == 1


@pytest.mark.parametrize("ending", ["targeted", "native"])
async def test_cancellation_owns_cooperative_scanner_and_closes_spools(
    grep_api, monkeypatch, ending
):
    (grep_api.workspace / "file.txt").write_text("needle\n" * 5000)
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    spools = []
    original_temp, original_scan = agent_grep.tempfile.TemporaryFile, agent_module.scan_folder

    def temporary(*args, **kwargs):
        spool = original_temp(*args, **kwargs)
        spools.append(spool)
        write = spool.write

        def blocked(text):
            result = write(text)
            entered.set()
            assert release.wait(5), "Scanner barrier was not released"
            return result

        spool.write = blocked
        return spool

    def scan(*args):
        try:
            return original_scan(*args)
        finally:
            finished.set()

    monkeypatch.setattr(agent_grep.tempfile, "TemporaryFile", temporary)
    monkeypatch.setattr(agent_module, "scan_folder", scan)
    async with grep_api.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        pending = asyncio.create_task(client.post(execute_path(sid, run), json=approval(run)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            (op,) = owner.active.values()
            if ending == "native":
                assert op.task.cancel()
                await asyncio.sleep(0)
            else:
                response = await client.post(
                    f"{BASE}/{sid}/runs/{run['id']}/cancel",
                    json={"request_id": "stop", "operation_id": op.id},
                )
                assert response.status_code in (200, 202)
            assert op.stop.is_set()
            assert not finished.is_set() and not pending.done()
            assert owner.active and any(not spool.closed for spool in spools)
            release.set()
            await asyncio.wait_for(asyncio.shield(pending), 3)
            detail = await run_detail(client, sid, run["id"])
            assert detail["status"] == ("interrupted" if ending == "native" else "cancelled")
            assert detail["answer"] is None
            assert finished.is_set() and all(spool.closed for spool in spools)
            assert not owner.active and len(grep_api.calls) == 1
            assert not any(r.event.type == "tool.result" for r in owner.store.records[sid])
            grep_api.files.read.assert_not_called()
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)


@pytest.mark.parametrize("phase", ["artifact", "result"])
async def test_storage_failure_prevents_synthesis_success_and_retry(grep_api, monkeypatch, phase):
    (grep_api.workspace / "file.txt").write_text("needle")
    spools = []
    original_temp = agent_grep.tempfile.TemporaryFile

    def temporary(*args, **kwargs):
        spool = original_temp(*args, **kwargs)
        spools.append(spool)
        return spool

    monkeypatch.setattr(agent_grep.tempfile, "TemporaryFile", temporary)
    async with grep_api.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        write = owner.store._write
        artifact = owner.store.write_artifact
        attempts = []

        def fail_record(record, *args):
            if record.event.type == "tool.result":
                attempts.append("result")
                raise OSError("PRIVATE_RECORD_SECRET")
            return write(record, *args)

        def fail_artifact(*args):
            attempts.append("artifact")
            raise OSError("PRIVATE_ARTIFACT_SECRET")

        monkeypatch.setattr(owner.store, "_write", fail_record if phase == "result" else write)
        monkeypatch.setattr(
            owner.store, "write_artifact", fail_artifact if phase == "artifact" else artifact
        )
        response = await client.post(execute_path(sid, run), json=approval(run))
        assert attempts == [phase]
        assert "PRIVATE_" not in response.text and "run.completed" not in response.text
        assert len(grep_api.calls) == 1
        assert spools and all(spool.closed for spool in spools)
        records = owner.store.records[sid]
        assert not any(r.event.type in {"tool.result", "run.completed"} for r in records)
        assert records[-1].event.session.runs[0].answer is None
        retry = await client.post(execute_path(sid, run), json=approval(run, "retry"))
        assert retry.status_code in (409, 503)
        assert attempts == [phase] and len(grep_api.calls) == 1


async def test_wrong_offset_and_unavailable_artifact_metadata_are_sanitized(grep_api):
    (grep_api.workspace / "file.txt").write_text("needle")
    async with grep_api.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        _, completed = await execute(client, sid, run)
        metadata = completed["results"][0]["grep"]
        url = f"{BASE}/{sid}/artifacts/{metadata['artifact_id']}"
        for query in ("offset=-1", f"offset={metadata['total_chars'] + 1}", "limit=16001"):
            response = await client.get(url + "?" + query)
            assert response.status_code == 422
        path = owner.store.root / sid / "artifacts" / (metadata["artifact_id"] + ".txt")
        path.unlink()
        for suffix in (f"/artifacts/{metadata['artifact_id']}", f"/runs/{run['id']}"):
            response = await client.get(f"{BASE}/{sid}" + suffix)
            assert response.status_code == 503
            assert "needle" not in response.text and str(path) not in response.text
        assert len(grep_api.calls) == 2


@pytest.mark.parametrize(
    "version",
    [
        "agent-tools-v1",
        "agent-tools-v2",
        "agent-tools-v2-exact",
        "file-only",
    ],
)
async def test_legacy_registry_policy_cannot_authorize_grep(grep_api, monkeypatch, version):
    scan = Mock(side_effect=AssertionError("Stale approvals must not scan"))
    monkeypatch.setattr(agent_module, "scan_folder", scan)
    async with grep_api.opened() as (client, owner, _):
        projection = ToolRegistry.projection
        # Issue a correctly digested historical-policy plan, then restore today's registry.
        with monkeypatch.context() as historical:
            historical.setattr(
                ToolRegistry,
                "projection",
                lambda registry: (
                    registry.files.projection()
                    if version == "file-only"
                    else {
                        **projection(registry),
                        "tool_registry_version": version,
                        "enabled_tools": "read_file,web_fetch",
                    }
                ),
            )
            sid, run, _ = await plan(client)
        snapshot = owner.store.get(sid)
        assert not owner.registry.matches(
            snapshot.runs[0].plan.access_policy, snapshot.runs[0].plan.steps
        )
        response = await client.post(execute_path(sid, run), json=approval(run))
        assert response.status_code == 409 and response.json()["error"]["code"] == "policy_changed"
        scan.assert_not_called()
        grep_api.files.read.assert_not_called()
        assert len(grep_api.calls) == 1


async def test_approved_offset_past_matches_fails_without_metadata_or_synthesis(grep_api):
    (grep_api.workspace / "file.txt").write_text("needle")
    grep_api.arguments["offset"] = 100000
    async with grep_api.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        assert run["status"] == "awaiting_approval"
        response, failed = await execute(client, sid, run)
        assert decode(response)[-1]["type"] == "run.failed"
        assert failed["status"] == "failed" and failed["answer"] is None
        result = failed["results"][0]
        assert result["status"] == "failed" and result.get("grep") is None
        assert result["content"] == ""
        assert not owner.store.artifacts[sid]
        grep_api.files.read.assert_not_called()
        assert len(grep_api.calls) == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("path", "other"),
        ("pattern", "different"),
        ("file_glob", "*.py"),
    ],
)
async def test_execute_cannot_replace_reviewed_search_scope(grep_api, monkeypatch, field, value):
    scan = Mock(side_effect=AssertionError("Unapproved arguments must not scan"))
    monkeypatch.setattr(agent_module, "scan_folder", scan)
    async with grep_api.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        body = {**approval(run), "arguments": {**grep_api.arguments, field: value}}
        response = await client.post(execute_path(sid, run), json=body)
        assert response.status_code == 422
        assert (await run_detail(client, sid, run["id"]))["plan"] == run["plan"]
        scan.assert_not_called()
        grep_api.files.read.assert_not_called()
        assert len(grep_api.calls) == 1


async def test_policy_change_after_durable_intent_blocks_scanner(grep_api, monkeypatch):
    scan = Mock(side_effect=AssertionError("Changed policy must not scan"))
    monkeypatch.setattr(agent_module, "scan_folder", scan)
    async with grep_api.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        append = owner.store.append

        def changed(snapshot, kind, *args, **kwargs):
            result = append(snapshot, kind, *args, **kwargs)
            if kind == "tool.intent":
                grep_api.fetcher.policy_version = "changed"
            return result

        monkeypatch.setattr(owner.store, "append", changed)
        response, failed = await execute(client, sid, run)
        assert decode(response)[-1]["error"]["code"] == "policy_changed"
        assert failed["status"] == "failed" and failed["answer"] is None
        scan.assert_not_called()
        grep_api.files.read.assert_not_called()
        assert len(grep_api.calls) == 1
