"""Default planner/synthesizer composition with offline HTTP and temporary storage."""

import asyncio
import json
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

from app.agent import SEARCH_PROVENANCE_NOTE
from app.llm_schemas import LLMRecord
from app.tools import FetchDocument
from tests import test_agent_responses_api as responses_api
from tests.test_agent import BASE, approval, decode, run_detail
from tests.test_agent_responses_api import (
    COMMAND,
    PRIVATE,
    TOKEN,
    URL,
    audits,
    plan,
    provider_body,
)

wired = responses_api.wired

ANSWER = "结论🌱: synthetic evidence only; the recorded excerpt is truncated."


def answer_body(text=ANSWER):
    body = provider_body()
    body["output"][1]["content"] = [{"type": "output_text", "text": text}]
    return body


def route(wired, synthesis):
    wired.handler = lambda request: (
        httpx.Response(200, json=provider_body()) if len(wired.calls) == 1 else synthesis(request)
    )


def execute_path(sid, run):
    return f"{BASE}/{sid}/runs/{run['id']}/execute"


def assert_retained(run, status):
    assert run["status"] == status
    assert run["answer"] is None
    assert len(run["results"]) == len(run["plan"]["steps"])
    assert all(result["status"] == "completed" for result in run["results"])
    assert run["results"][0]["content"] == "Synthetic public evidence"


@pytest.mark.parametrize("scenario", ["search", "search_and_fetch", "empty_search", "fetch"])
async def test_search_provenance_note_is_deterministic_with_synthesis(wired, scenario):
    misleading = "I fetched and read every source page in full."
    proposed = json.loads(provider_body()["output"][1]["content"][0]["text"])
    search_step = {
        "title": "Search",
        "description": "Find sources",
        "tool": "web_search",
        "arguments": {"query": "synthetic query", "max_results": 2},
    }
    if scenario != "fetch":
        proposed["steps"] = [search_step] + (
            proposed["steps"] if scenario == "search_and_fetch" else []
        )
    response = provider_body()
    response["output"][1]["content"][0]["text"] = json.dumps(proposed)
    hit = {
        "title": "Source",
        "url": "https://example.org/",
        "snippet": "Snippet",
        "published_date": None,
    }
    search = AsyncMock(return_value=[] if scenario == "empty_search" else [hit])

    async with wired.opened() as (client, owner, _):
        owner.searcher = SimpleNamespace(search=search)
        wired.handler = lambda request: httpx.Response(
            200, json=response if len(wired.calls) == 1 else answer_body(misleading)
        )
        sid, run, _ = await plan(client)
        frames = decode(await client.post(execute_path(sid, run), json=approval(run)))
        completed = await run_detail(client, sid, run["id"])
        expected = (
            SEARCH_PROVENANCE_NOTE + "\n\n" + misleading
            if scenario in {"search", "search_and_fetch"}
            else misleading
        )
        assert completed["status"] == "completed" and completed["answer"] == expected
        assert frames[-1]["answer"] == expected
        assert len(wired.calls) == 2
        assert search.await_count == (scenario != "fetch")
        assert wired.fetcher.fetch.await_count == (scenario in {"fetch", "search_and_fetch"})
        assert SEARCH_PROVENANCE_NOTE == (
            "Provenance note: Web search results are snippets provided by the search service; "
            "the search itself does not independently verify their contents, and the results "
            "do not establish that their source pages were fetched."
        )
    async with wired.opened() as (client, _, _):
        assert (await run_detail(client, sid, run["id"]))["answer"] == expected


async def test_default_synthesis_exact_durable_evidence_and_restart(wired, tmp_path):
    wired.files.roots["fixture"] = tmp_path
    file_result = {
        "content": "4: 原文🌱",
        "path": "fixture/文档.txt",
        "offset": 4,
        "returned_lines": 1,
        "truncated": True,
    }

    def read_file(args, stop, deadline):
        assert not stop.is_set() and deadline > 0
        return dict(file_result)

    wired.files.read.side_effect = read_file
    wired.fetcher.fetch.side_effect = lambda url: FetchDocument(
        requested_url=url,
        final_url="https://example.com/final",
        title="Synthetic title",
        status_code=200,
        content_type="text/plain",
        fetched_at="2026-10-23T00:00:00Z",
        redirects=("https://example.com/final",),
        text="原文🌱" * 100,
    )
    proposal = provider_body()
    proposed = json.loads(proposal["output"][1]["content"][0]["text"])
    proposed["steps"].append(
        {
            "title": "Read",
            "description": "Read the approved lines",
            "tool": "read_file",
            "arguments": {"workspace_id": "fixture", "path": "文档.txt", "offset": 4, "limit": 1},
        }
    )
    proposal["output"][1]["content"][0]["text"] = json.dumps(proposed)
    sent = []
    async with wired.opened() as (client, owner, _):
        assert owner.planner.transport.client is owner.synthesizer.transport.client
        assert (
            owner.planner.transport.bearer_provider is owner.synthesizer.transport.bearer_provider
        )

        def handler(request):
            if len(wired.calls) == 1:
                return httpx.Response(200, json=proposal)
            records = owner.store.records[sid]
            results = [r for r in records if r.event.type == "tool.result"]
            assert len(results) == 2
            durable_run = results[-1].event.session.runs[0]
            assert all(r.status == "completed" for r in durable_run.results)
            wire = json.loads(request.content)
            evidence = json.loads(wire["input"][0]["content"])
            assert evidence == {
                "request": COMMAND["content"],
                "approved_plan": run["plan"],
                "results": [r.model_dump() for r in durable_run.results],
            }
            sent.append(evidence)
            assert wire["model"] == "deployment-b"
            assert wire["reasoning"] == {"effort": "xhigh"}
            assert wire["max_output_tokens"] == 1234
            assert wire["text"] == {"format": {"type": "text"}}
            assert wire["store"] is wire["stream"] is False
            assert not {"tools", "tool_choice", "previous_response_id"} & wire.keys()
            assert request.headers["authorization"] == "Bearer " + TOKEN
            return httpx.Response(200, json=answer_body())

        wired.handler = handler
        sid, run, _ = await plan(client)
        assert run["status"] == "awaiting_approval" and len(wired.calls) == 1
        wired.files.read.assert_not_called()
        wired.fetcher.fetch.assert_not_awaited()
        frames = decode(await client.post(execute_path(sid, run), json=approval(run)))
        completed = await run_detail(client, sid, run["id"])
        assert completed["status"] == "completed" and completed["answer"] == ANSWER
        assert frames[-1]["type"] == "run.completed" and frames[-1]["answer"] == ANSWER
        assert len(sent) == 1 and len(wired.calls) == 2
        fetch, read = sent[0]["results"]
        assert fetch["content"] == ("原文🌱" * 100)[:100]
        assert fetch["fetch"]["requested_url"] == URL
        assert fetch["fetch"]["final_url"] == "https://example.com/final"
        assert fetch["fetch"]["redirects"] == ["https://example.com/final"]
        assert fetch["fetch"]["total_chars"] == 300
        assert fetch["fetch"]["next_offset"] == 100
        assert fetch["fetch"]["returned_chars"] == 100
        assert {key: read[key] for key in file_result} == file_result
        wired.fetcher.fetch.assert_awaited_once_with(URL)
        args, _, _ = wired.files.read.call_args.args
        assert args.model_dump() == proposed["steps"][1]["arguments"]
        wired.files.read.assert_called_once()
        records = owner.store.records[sid]
        types = [r.event.type for r in records]
        assert types.count("run.approved") == 1
        assert types.count("tool.intent") == types.count("tool.result") == 2
        groups = audits(owner, sid)
        assert len(groups) == 2
        for phase, group in zip(("planning", "synthesis"), groups, strict=True):
            assert [r["kind"] for r in group] == [
                "intent",
                "dispatched",
                "output",
                "outcome",
                "validation",
            ]
            for row in group:
                LLMRecord.model_validate(row)
                assert row["identity"] == group[0]["identity"]
            identity = group[0]["identity"]
            assert identity["phase"] == phase and identity["session_id"] == sid
            assert identity["run_id"] == run["id"]
            assert (identity["plan_id"], identity["revision"]) == (run["plan"]["id"], 1)
            assert identity["attempt"] == 1 and identity["retry_of"] is None
        planning, synthesis = [g[0]["identity"] for g in groups]
        approved = next(r for r in records if r.event.type == "run.approved")
        assert synthesis["operation_id"] == approved.event.operation_id
        assert synthesis["stage_id"] == approved.audit["stage_id"]
        for key in ("operation_id", "stage_id", "interaction_id", "attempt_id"):
            assert synthesis[key] != planning[key]
        assert groups[1][2]["data"]["body"] == answer_body()
        assert groups[1][-1]["data"]["status"] == "valid"
        for key in ("execute", "duplicate"):
            duplicate = await client.post(execute_path(sid, run), json=approval(run, key))
            assert duplicate.status_code == 409
        assert PRIVATE not in json.dumps(completed) + json.dumps(frames)
        assert TOKEN not in json.dumps(groups)
        assert len(wired.calls) == 2
    wired.handler = lambda request: pytest.fail("Restart must not replay provider requests")
    async with wired.opened() as (client, owner, _):
        assert await run_detail(client, sid, run["id"]) == completed
        assert audits(owner, sid) == groups
        duplicate = await client.post(execute_path(sid, run), json=approval(run, "restart"))
        assert duplicate.status_code == 409
        assert len(wired.calls) == 2


@pytest.mark.parametrize(
    "failure,code",
    [
        ("http", "synthesis_unavailable"),
        ("failed", "synthesis_unavailable"),
        ("refusal", "synthesis_unavailable"),
        ("blank", "invalid_answer"),
        ("timeout", "synthesis_timeout"),
    ],
)
async def test_synthesis_failure_keeps_tools_without_answer_or_retry(wired, failure, code):
    body = answer_body(" \n\t " if failure == "blank" else ANSWER)
    if failure == "failed":
        body["status"] = "failed"
    if failure == "refusal":
        body["output"][1]["content"] = [{"type": "refusal", "refusal": PRIVATE}]

    def respond(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_TIMEOUT", request=request)
        return httpx.Response(503 if failure == "http" else 200, json=body)

    route(wired, respond)
    async with wired.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        response = await client.post(execute_path(sid, run), json=approval(run))
        frames = decode(response)
        failed = await run_detail(client, sid, run["id"])
        assert_retained(failed, "failed")
        assert failed["error"]["code"] == code
        assert frames[-1]["type"] == "run.failed"
        assert not any(f["type"] == "run.completed" for f in frames)
        group = audits(owner, sid)[1]
        if failure != "timeout":
            assert next(r["data"]["body"] for r in group if r["kind"] == "output") == body
        if failure == "blank":
            assert group[-1]["data"]["status"] == "invalid"
        assert PRIVATE not in response.text and "PRIVATE_TIMEOUT" not in response.text
        duplicate = await client.post(execute_path(sid, run), json=approval(run, "again"))
        assert duplicate.status_code == 409
        assert len(wired.calls) == 2
        wired.fetcher.fetch.assert_awaited_once_with(URL)
        wired.files.read.assert_not_called()


@pytest.mark.parametrize("ending", ["stop", "deadline"])
async def test_stop_or_deadline_during_synthesis_retains_partial_body(wired, ending):
    entered, closed = asyncio.Event(), asyncio.Event()

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"private_partial":'
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    route(wired, lambda request: httpx.Response(200, stream=Stream()))
    async with wired.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        if ending == "deadline":
            owner.planner_timeout_seconds = 0.05
        pending = asyncio.create_task(client.post(execute_path(sid, run), json=approval(run)))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            (op,) = owner.active.values()
            if ending == "stop":
                response = await client.post(
                    f"{BASE}/{sid}/runs/{run['id']}/cancel",
                    json={"request_id": "stop", "operation_id": op.id},
                )
                assert response.status_code in (200, 202)
            frames = decode(await asyncio.wait_for(asyncio.shield(pending), 3))
            status = "cancelled" if ending == "stop" else "failed"
            assert frames[-1]["type"] == "run." + status
            detail = await run_detail(client, sid, run["id"])
            assert_retained(detail, status)
            if ending == "deadline":
                assert detail["error"]["code"] == "synthesis_timeout"
            group = audits(owner, sid)[1]
            output = next(r["data"] for r in group if r["kind"] == "output")
            assert output["body"]["partial"] is True
            assert output["body"]["data"] == '{"private_partial":'
            assert group[-1]["data"]["status"] == ("cancelled" if ending == "stop" else "timed_out")
            assert closed.is_set() and not owner.active
            assert len(wired.calls) == 2
            wired.fetcher.fetch.assert_awaited_once()
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_no_synthesis_dispatch_until_tool_result_commit_finishes(wired, monkeypatch):
    route(wired, lambda request: httpx.Response(200, json=answer_body()))
    entered, release = threading.Event(), threading.Event()
    synthesis_intents = []
    async with wired.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        write = owner.store._write

        def blocked_write(record, *args):
            if (
                record.event.type == "llm.intent"
                and record.audit["identity"]["phase"] == "synthesis"
            ):
                synthesis_intents.append(record)
            if record.event.type == "tool.result":
                entered.set()
                assert release.wait(3), "Tool-result barrier was not released"
            return write(record, *args)

        monkeypatch.setattr(owner.store, "_write", blocked_write)
        pending = asyncio.create_task(client.post(execute_path(sid, run), json=approval(run)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            wired.fetcher.fetch.assert_awaited_once_with(URL)
            assert len(wired.calls) == 1
            assert synthesis_intents == []
            release.set()
            frames = decode(await asyncio.wait_for(asyncio.shield(pending), 3))
            assert frames[-1]["type"] == "run.completed"
            assert frames[-1]["answer"] == ANSWER and len(wired.calls) == 2
            assert len(synthesis_intents) == 1
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)


@pytest.mark.parametrize("kind", ["intent", "output", "validation"])
async def test_synthesis_disk_failure_closes_dispatch_and_publication(wired, monkeypatch, kind):
    route(wired, lambda request: httpx.Response(200, json=answer_body()))
    async with wired.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        write = owner.store._write
        attempted = []

        def failing_write(record, *args):
            if (
                record.event.type == "llm." + kind
                and record.audit["identity"]["phase"] == "synthesis"
            ):
                attempted.append(record)
                raise OSError("PRIVATE_DISK_FAILURE")
            return write(record, *args)

        monkeypatch.setattr(owner.store, "_write", failing_write)
        response = await client.post(execute_path(sid, run), json=approval(run))
        assert len(attempted) == 1 and owner.store.poisoned
        records = owner.store.records[sid]
        durable = records[-1].event.session.runs[0]
        assert durable.answer is None and durable.status != "completed"
        assert durable.results[0].status == "completed"
        assert not any(r.event.type == "run.completed" for r in records)
        assert len(wired.calls) == (1 if kind == "intent" else 2)
        assert "PRIVATE_DISK_FAILURE" not in response.text
        retry = await client.post(execute_path(sid, run), json=approval(run, "again"))
        assert retry.status_code == 503
        assert len(wired.calls) == (1 if kind == "intent" else 2)
        wired.fetcher.fetch.assert_awaited_once()


async def test_native_cancellation_while_valid_answer_is_being_recorded(wired, monkeypatch):
    route(wired, lambda request: httpx.Response(200, json=answer_body()))
    entered, release = threading.Event(), threading.Event()
    async with wired.opened() as (client, owner, _):
        sid, run, _ = await plan(client)
        write = owner.store._write

        def blocked_write(record, *args):
            if (
                record.event.type == "llm.validation"
                and record.audit["identity"]["phase"] == "synthesis"
            ):
                entered.set()
                assert release.wait(3), "Validation barrier was not released"
            return write(record, *args)

        monkeypatch.setattr(owner.store, "_write", blocked_write)
        pending = asyncio.create_task(client.post(execute_path(sid, run), json=approval(run)))
        try:
            assert await asyncio.to_thread(entered.wait, 3)
            (op,) = owner.active.values()
            assert op.task.cancel()
            await asyncio.sleep(0)
            assert op.stop.is_set()
            release.set()
            await asyncio.wait_for(asyncio.shield(pending), 3)
            assert_retained(await run_detail(client, sid, run["id"]), "interrupted")
            group = audits(owner, sid)[1]
            assert group[-1]["kind"] == "validation"
            assert group[-1]["data"]["status"] == "valid"
            assert not any(r.event.type == "run.completed" for r in owner.store.records[sid])
            assert not owner.active and len(wired.calls) == 2
        finally:
            release.set()
            await asyncio.wait_for(asyncio.gather(pending, return_exceptions=True), 3)
