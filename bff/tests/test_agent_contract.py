"""Public BFF wire fixtures consumed by Angular, with no live services.

From bff/, explicitly regenerate only the checked-in public JSON with:
    UPDATE_AGENT_CONTRACT_FIXTURE=1 uv run --offline --frozen pytest tests/test_agent_contract.py -q
Normal runs compare all snapshots and parsed SSE fields without rewriting files.
"""

import asyncio
import copy
import itertools
import json
import os
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import UUID

import httpx
import pytest

import app.agent as agent_module
import app.sessions as sessions_module
from app.agent import AgentService
from app.agent_files import FilePolicy, canonical_digest
from app.agent_llm import CanonicalRequest, Observation, RecordedPlanner
from app.agent_schemas import Proposal, StepResult
from app.agent_views import EventV2
from app.main import create_app
from app.sessions import SessionStore
from app.tools import FetchDocument, WebFetcher

FIXTURE = Path(__file__).resolve().parents[2] / "ui/src/app/agent-contract.fixture.json"
BASE = "/api/agent/sessions"
NOW = "2026-10-16T09:00:00.123456Z"
EARLIER = "2026-10-16T08:59:59.123456Z"
CONTENT = "Read the public fixture"
PRIVATE_CANARIES = (
    "SYNTHETIC-private-user-message",
    "SYNTHETIC-private-instructions",
    "SYNTHETIC-private-output",
    "SYNTHETIC-private-continuation",
    "SYNTHETIC-private-usage",
    "SYNTHETIC-client-credential",
    "SYNTHETIC-provider-model",
)


def proposal():
    return Proposal.model_validate(
        {
            "objective": CONTENT,
            "assumptions": ["The public file is unverified."],
            "limits": {"max_steps": 1, "max_tool_calls": 1, "timeout_seconds": 30},
            "steps": [
                {
                    "title": "Read public text",
                    "description": "Read the first two lines only.",
                    "tool": "read_file",
                    "arguments": {
                        "workspace_id": "public",
                        "path": "public.txt",
                        "offset": 1,
                        "limit": 2,
                    },
                }
            ],
        }
    )


@pytest.fixture
def contract_setup(tmp_path, monkeypatch, settings_factory, mock_client):
    ids = itertools.count(1)
    monkeypatch.setattr(agent_module, "uuid4", lambda: UUID(int=next(ids)))
    monkeypatch.setattr(sessions_module, "uuid4", lambda: UUID(int=next(ids)))
    real_projection = FilePolicy.projection

    def stable_projection(policy):
        # Only host-path-dependent opaque hashes are stabilized. Real file access,
        # policy version/rules, plan digests and public serialization remain intact.
        return {
            **real_projection(policy),
            "roots_digest": canonical_digest("contract-public-workspace"),
            "storage_digest": canonical_digest("contract-temporary-store"),
        }

    monkeypatch.setattr(FilePolicy, "projection", stable_projection)

    @asynccontextmanager
    async def opened(
        suffix,
        *,
        blocked=False,
        backwards=False,
        enabled=True,
        fetched=False,
        recorded=False,
        journal="journal",
    ):
        directory = tmp_path / suffix
        workspace = directory / "workspace"
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / "public.txt").write_text("alpha 🌱\nbeta\nbeyond the page\n", encoding="utf-8")
        clock = SimpleNamespace(value=NOW)

        async def plan(context):
            if backwards:
                clock.value = EARLIER
            if fetched:
                return Proposal.model_validate(
                    {
                        "objective": "Read the supplied public URL",
                        "assumptions": [],
                        "steps": [
                            {
                                "title": "Fetch public evidence",
                                "description": "Preserve full text; return the first page.",
                                "tool": "web_fetch",
                                "arguments": {
                                    "url": "https://example.com/public",
                                    "offset": 0,
                                    "limit": 18,
                                },
                            }
                        ],
                    }
                )
            return proposal()

        planner = SimpleNamespace(plan=AsyncMock(side_effect=plan))
        transport = SimpleNamespace(credential=PRIVATE_CANARIES[5])
        requests = []

        def build_request(context):
            request = CanonicalRequest(
                {
                    "model": context.model,
                    "reasoning": {"effort": context.reasoning_effort},
                    "instructions": context.instructions + PRIVATE_CANARIES[1],
                    "input": [
                        {"role": "user", "content": context.request + PRIVATE_CANARIES[0]},
                        {"type": "opaque_continuation", "body": PRIVATE_CANARIES[3]},
                    ],
                    "store": False,
                    "stream": True,
                },
                instructions_version="contract-instructions-v1",
            )
            requests.append(request)
            return request

        async def send_once(raw, observe):
            assert raw is requests[-1].body_bytes
            assert json.loads(raw) == requests[-1].body
            await observe(Observation("event", {"delta": PRIVATE_CANARIES[2]}))
            return Observation(
                "response",
                {"proposal": proposal().model_dump(), "opaque": PRIVATE_CANARIES[3]},
                reported_model=PRIVATE_CANARIES[6],
                provider_status="completed",
                usage={"input_tokens": 11, "output_tokens": 23, "opaque": PRIVATE_CANARIES[4]},
            )

        transport.send_once = AsyncMock(side_effect=send_once)
        decode = Mock(
            side_effect=lambda result: Proposal.model_validate(result.outputs[-1].body["proposal"])
        )
        if recorded:
            planner = RecordedPlanner(
                build_request=build_request, transport=transport, decode=decode
            )
        files = FilePolicy({"public": workspace})
        entered, release = threading.Event(), threading.Event()
        real_read = files.read

        def read(*args):
            if blocked:
                entered.set()
                assert release.wait(5), "Contract test did not release its mocked worker"
            return real_read(*args)

        files.read = Mock(side_effect=read)
        store = SessionStore(
            directory / journal,
            clock=lambda: clock.value,
            id_factory=lambda: f"20261016T090000123456Z-{suffix}",
        )
        owner = AgentService(
            store,
            planner=planner if enabled else None,
            files=files,
            models=("deployment-a",),
            fetcher=SimpleNamespace(
                policy_version=WebFetcher.policy_version,
                validate_url=WebFetcher().validate_url,
                fetch=AsyncMock(
                    return_value=FetchDocument(
                        requested_url="https://example.com/public",
                        final_url="https://example.com/public-text",
                        title="Public evidence",
                        status_code=200,
                        content_type="text/html",
                        fetched_at=NOW,
                        redirects=("https://example.com/public-text",),
                        text="Public evidence 🌱\nContinuation from durable storage.",
                    )
                ),
            ),
        )
        application = create_app(
            settings_factory(),
            credential_factory=Mock(return_value=SimpleNamespace(close=AsyncMock())),
            client_factory=Mock(return_value=mock_client),
            agent_service_factory=lambda settings: owner,
        )
        async with application.router.lifespan_context(application):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=application), base_url="http://contract.test"
            ) as client:
                try:
                    yield SimpleNamespace(
                        client=client,
                        owner=owner,
                        files=files,
                        planner=planner,
                        entered=entered,
                        release=release,
                        transport=transport,
                        decode=decode,
                    )
                finally:
                    release.set()
        mock_client.responses.create.assert_not_awaited()

    return opened


def events(raw):
    assert raw.endswith("\n\n")
    result = []
    for block in raw.removesuffix("\n\n").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        assert set(fields) == {"id", "event", "data"}
        assert fields["event"] == "agent"
        payload = json.loads(fields["data"])
        EventV2.model_validate(payload)
        assert payload["schema_version"] == 2
        assert payload["session"]["projection_version"] == 2
        assert "runs" not in payload["session"]
        assert fields["id"] == str(payload["sequence"])
        assert payload["session"]["sequence"] == payload["sequence"]
        result.append({**fields, "data": payload})
    return result


async def get(client, sid):
    response = await client.get(f"{BASE}/{sid}")
    assert response.status_code == 200
    return response.json()


async def create(client):
    response = await client.post(
        BASE,
        json={"request_id": "create", "model": "deployment-a", "reasoning_effort": "medium"},
    )
    assert response.status_code == 200
    return response.json()


async def detail(client, snapshot):
    rid = snapshot["latest_run_id"]
    response = await client.get(f"{BASE}/{snapshot['id']}/runs/{rid}")
    assert response.status_code == 200
    return response.json()


async def identity(client, snapshot):
    run = await detail(client, snapshot)
    plan = run["plan"]
    return {
        "run_id": run["id"],
        "plan_id": plan["id"],
        "revision": plan["revision"],
        "digest": plan["digest"],
    }


def exchange(name, baseline, key, command):
    fields = dict(command)
    action = fields.pop("action")
    rid = fields.pop("run_id", None)
    url = f"{BASE}/{baseline['id']}/runs"
    if rid is not None:
        url += f"/{rid}/{action}"
    return {
        "name": name,
        "baseline": baseline,
        "command": command,
        "http": {"method": "POST", "url": url, "body": {"request_id": key, **fields}},
    }


async def finish(client, case, response):
    assert response.status_code == 200, response.text
    assert response.headers["content-type"].startswith("text/event-stream")
    case["sse"] = response.text
    case["terminal"] = await get(client, case["baseline"]["id"])
    case["terminal_run"] = await detail(client, case["terminal"])
    assert events(case["sse"])[-1]["data"]["session"] == case["terminal"]
    return case


async def stream(client, name, baseline, key, command):
    case = exchange(name, baseline, key, command)
    response = await client.post(case["http"]["url"], json=case["http"]["body"])
    return await finish(client, case, response)


async def control(client, method, url, body):
    response = await client.request(method, url, json=body)
    assert response.status_code in {200, 202}
    return {
        "http": {"method": method, "url": url, "body": body},
        "status": response.status_code,
        "session": response.json(),
    }


async def startup_recovery(opened):
    """Persist an unmatched tool intent, then exercise the real store startup recovery."""
    async with opened("ct07") as setup:
        planning = await stream(
            setup.client,
            "setup",
            await create(setup.client),
            "plan",
            {"action": "runs", "content": CONTENT},
        )
        sid = planning["terminal"]["id"]
        snapshot = await setup.owner.get(sid)
        run = snapshot.runs[0]
        rid = run.id
        run.status, run.operation_id = "approved", "execution-before-restart"
        # Seed the durable pre-crash prefix without starting a worker whose graceful
        # shutdown would settle it. The recovery event itself is never synthesized.
        for kind in ("run.approved", "run.executing", "tool.intent"):
            run = snapshot.runs[0]
            if kind == "run.executing":
                run.status = "executing"
            if kind == "tool.intent":
                run.results.append(
                    StepResult(
                        step_id=run.plan.steps[0].id,
                        status="running",
                        content="",
                        time=NOW,
                    )
                )
            snapshot = (
                await setup.owner.commit(
                    snapshot,
                    kind,
                    "execute-before-restart",
                    run_id=rid,
                    operation_id=run.operation_id,
                )
            ).session
        baseline = await get(setup.client, sid)
        baseline_run = await detail(setup.client, baseline)
        setup.files.read.assert_not_called()

    # Reopening the same temporary journal calls SessionStore.start/_recover. Its
    # original recovery envelope has null run/operation IDs even for an affected run.
    async with opened("ct07") as setup:
        url = f"{BASE}/{sid}/events?after={baseline['sequence']}&limit=50"
        response = await setup.client.get(url)
        assert response.status_code == 200
        terminal = await get(setup.client, sid)
        terminal_run = await detail(setup.client, terminal)
        runs = await setup.client.get(f"{BASE}/{sid}/runs?limit=10")
        results = await setup.client.get(f"{BASE}/{sid}/runs/{rid}/results?limit=20")
        assert runs.status_code == results.status_code == 200
        setup.planner.plan.assert_not_awaited()
        setup.files.read.assert_not_called()
        setup.owner.fetcher.fetch.assert_not_awaited()
        return {
            "name": "startup_recovery",
            "baseline": baseline,
            "baseline_run": baseline_run,
            "http": {"method": "GET", "url": url},
            "status": response.status_code,
            "body": response.json(),
            "terminal": terminal,
            "terminal_run": terminal_run,
            "runs_page": runs.json(),
            "results_page": results.json(),
        }


async def llm_startup_recovery(opened, prefix):
    """Replay an actual coordinator-generated synthetic prefix into a fresh tmp store."""
    async with opened("ct08", journal="recovery") as setup:
        created = await create(setup.client)
        assert created["id"] == prefix[0].event.session_id
        for record in prefix[1:]:
            event = record.event
            await setup.owner.commit(
                event.session.model_copy(update={"sequence": event.sequence - 1}),
                event.type,
                event.request_id,
                run_id=event.run_id,
                operation_id=event.operation_id,
                claim=record.claim,
                audit=record.audit,
            )
        baseline = await get(setup.client, created["id"])
        baseline_run = await detail(setup.client, baseline)
        assert baseline_run["status"] == "planning" and baseline_run["plan"] is None

    async with opened("ct08", journal="recovery") as setup:
        sid, rid = baseline["id"], baseline_run["id"]
        url = f"{BASE}/{sid}/events?after={baseline['sequence']}&limit=50"
        response = await setup.client.get(url)
        assert response.status_code == 200
        terminal = await get(setup.client, sid)
        runs = await setup.client.get(f"{BASE}/{sid}/runs?limit=10")
        results = await setup.client.get(f"{BASE}/{sid}/runs/{rid}/results?limit=20")
        assert runs.status_code == results.status_code == 200
        setup.planner.plan.assert_not_awaited()
        setup.transport.send_once.assert_not_awaited()
        setup.files.read.assert_not_called()
        setup.owner.fetcher.fetch.assert_not_awaited()
        return {
            "name": "llm_startup_recovery",
            "baseline": baseline,
            "baseline_run": baseline_run,
            "http": {"method": "GET", "url": url},
            "status": response.status_code,
            "body": response.json(),
            "terminal": terminal,
            "terminal_run": await detail(setup.client, terminal),
            "runs_page": runs.json(),
            "results_page": results.json(),
        }


async def generate_contract(opened):
    streams, errors = [], []
    async with opened("ct01") as setup:
        client = setup.client
        baseline = await create(client)
        planning = await stream(
            client, "planning", baseline, "plan", {"action": "runs", "content": CONTENT}
        )
        streams.append(planning)
        assert setup.files.read.call_count == 0
        revision = await stream(
            client,
            "revision",
            planning["terminal"],
            "revise",
            {
                "action": "revise",
                **await identity(client, planning["terminal"]),
                "kind": "feedback",
                "content": "Keep the two-line page.",
            },
        )
        streams.append(revision)
        assert setup.files.read.call_count == 0
        execution = await stream(
            client,
            "execution",
            revision["terminal"],
            "execute",
            {"action": "execute", **await identity(client, revision["terminal"])},
        )
        streams.append(execution)
        assert setup.files.read.call_count == 1
        assert setup.planner.plan.await_count == 2
        response = await client.post(execution["http"]["url"], json=execution["http"]["body"])
        assert response.status_code == 409
        errors.append(
            {
                "name": "duplicate_execution",
                "baseline": execution["terminal"],
                "command": execution["command"],
                "http": execution["http"],
                "status": response.status_code,
                "body": response.json(),
            }
        )
        assert setup.files.read.call_count == 1

    async with opened("ct02") as setup:
        client = setup.client
        planning = await stream(
            client, "setup", await create(client), "plan", {"action": "runs", "content": CONTENT}
        )
        baseline = planning["terminal"]
        title = await control(
            client,
            "PATCH",
            f"{BASE}/{baseline['id']}",
            {"request_id": "title", "title": "A title committed before admission"},
        )
        case = await stream(
            client,
            "stale_baseline",
            baseline,
            "execute",
            {"action": "execute", **await identity(client, baseline)},
        )
        case["controls"] = [title]
        streams.append(case)

    async with opened("ct03", blocked=True) as setup:
        client = setup.client
        planning = await stream(
            client, "setup", await create(client), "plan", {"action": "runs", "content": CONTENT}
        )
        baseline = planning["terminal"]
        rid = baseline["latest_run_id"]
        case = exchange(
            "cancellation_archive",
            baseline,
            "execute",
            {"action": "execute", **await identity(client, baseline)},
        )
        task = asyncio.create_task(client.post(case["http"]["url"], json=case["http"]["body"]))
        try:
            assert await asyncio.to_thread(setup.entered.wait, 3), "Read did not start"
            executing = await get(client, baseline["id"])
            url = f"{BASE}/{baseline['id']}/runs/{rid}/cancel"
            old_stop = await control(
                client,
                "POST",
                url,
                {
                    "request_id": "old-stop",
                    "operation_id": events(planning["sse"])[0]["data"]["operation_id"],
                },
            )
            assert old_stop["status"] == 200
            assert old_stop["session"]["current_run"]["status"] == "executing"
            stop = await control(
                client,
                "POST",
                url,
                {"request_id": "stop", "operation_id": executing["current_run"]["operation_id"]},
            )
            archive = await control(
                client, "POST", f"{BASE}/{baseline['id']}/archive", {"request_id": "archive"}
            )
            assert stop["status"] == archive["status"] == 202
            assert archive["session"]["archive_pending"] is True
            case["controls"] = [old_stop, stop, archive]
        finally:
            setup.release.set()
            response = await task
        streams.append(await finish(client, case, response))
        assert setup.files.read.call_count == 1

    async with opened("ct04", backwards=True) as setup:
        streams.append(
            await stream(
                setup.client,
                "backwards_clock",
                await create(setup.client),
                "plan",
                {"action": "runs", "content": CONTENT},
            )
        )

    async with opened("ct05", enabled=False) as setup:
        case = exchange(
            "agent_not_configured",
            await create(setup.client),
            "plan",
            {"action": "runs", "content": CONTENT},
        )
        response = await setup.client.post(case["http"]["url"], json=case["http"]["body"])
        assert response.status_code == 503
        errors.append({**case, "status": response.status_code, "body": response.json()})
        setup.planner.plan.assert_not_awaited()

    async with opened("ct06", fetched=True) as setup:
        planning = await stream(
            setup.client,
            "setup",
            await create(setup.client),
            "plan",
            {"action": "runs", "content": "Read https://example.com/public"},
        )
        setup.owner.fetcher.fetch.assert_not_awaited()
        case = await stream(
            setup.client,
            "fetch_execution",
            planning["terminal"],
            "execute",
            {"action": "execute", **await identity(setup.client, planning["terminal"])},
        )
        metadata = case["terminal_run"]["results"][0]["fetch"]
        url = (
            f"{BASE}/{case['baseline']['id']}/artifacts/{metadata['artifact_id']}"
            f"?offset={metadata['next_offset']}&limit=12000"
        )
        response = await setup.client.get(url)
        assert response.status_code == 200
        case["artifact_page"] = {
            "http": {"method": "GET", "url": url},
            "status": response.status_code,
            "body": response.json(),
        }
        streams.append(case)
        setup.owner.fetcher.fetch.assert_awaited_once_with("https://example.com/public")
        setup.files.read.assert_not_called()

    async with opened("ct08", recorded=True) as setup:
        case = await stream(
            setup.client,
            "recorded_planning",
            await create(setup.client),
            "plan",
            {"action": "runs", "content": CONTENT},
        )
        sid = case["baseline"]["id"]
        replay = await setup.client.get(
            f"{BASE}/{sid}/events?after={case['baseline']['sequence']}&limit=50"
        )
        assert replay.status_code == 200
        assert replay.json()["events"] == [frame["data"] for frame in events(case["sse"])]
        streams.append(case)
        setup.transport.send_once.assert_awaited_once()
        setup.decode.assert_called_once()
        setup.files.read.assert_not_called()
        setup.owner.fetcher.fetch.assert_not_awaited()
        records = list(setup.owner.store.records[sid])
        intent = next(record for record in records if record.event.type == "llm.intent")
        llm_prefix = records[: intent.event.sequence]
        assert intent.audit["data"]["request"] == json.loads(
            setup.transport.send_once.call_args.args[0]
        )
        validation = next(record for record in records if record.event.type == "llm.validation")
        assert validation.audit["data"] == {"status": "valid", "error_code": None}
        outcome = next(record for record in records if record.event.type == "llm.outcome")
        assert outcome.audit["data"] == {
            "status": "completed",
            "dispatched": True,
            "output_count": 2,
            "reported_model": PRIVATE_CANARIES[6],
            "provider_status": "completed",
            "usage": {"input_tokens": 11, "output_tokens": 23, "opaque": PRIVATE_CANARIES[4]},
            "error_code": None,
        }

    return {
        "version": 2,
        "streams": streams,
        "errors": errors,
        "recoveries": [
            await startup_recovery(opened),
            await llm_startup_recovery(opened, llm_prefix),
        ],
    }


def assert_representative(contract):
    cases = {case["name"]: case for case in contract["streams"]}
    assert set(cases) == {
        "planning",
        "revision",
        "execution",
        "stale_baseline",
        "cancellation_archive",
        "backwards_clock",
        "fetch_execution",
        "recorded_planning",
    }
    for case in contract["streams"]:
        assert all(frame["data"]["invalidates_runs"] is False for frame in events(case["sse"]))
    planning_types = ["run.planning", "planner.attempt", "planner.result", "run.awaiting_approval"]
    execution_types = [
        "run.approved",
        "run.executing",
        "tool.intent",
        "tool.result",
        "run.completed",
    ]
    for name in ("planning", "revision", "backwards_clock"):
        assert [frame["data"]["type"] for frame in events(cases[name]["sse"])] == planning_types
    for name in ("execution", "stale_baseline"):
        case = cases[name]
        assert [frame["data"]["type"] for frame in events(case["sse"])] == execution_types
        result = case["terminal_run"]["results"][0]
        assert result["content"] == "1: alpha 🌱\n2: beta"
        assert (
            result["path"],
            result["offset"],
            result["returned_lines"],
            result["truncated"],
        ) == ("public.txt", 1, 2, True)
    stale = cases["stale_baseline"]
    assert events(stale["sse"])[0]["data"]["sequence"] == stale["baseline"]["sequence"] + 2
    cancellation = cases["cancellation_archive"]
    frames = events(cancellation["sse"])
    assert [frame["data"]["type"] for frame in frames] == [
        "run.approved",
        "run.executing",
        "tool.intent",
        "session.updated",
        "run.cancelling",
        "session.updated",
        "session.archive_pending",
        "run.cancelled",
    ]
    for index, key in ((3, "old-stop"), (5, "stop"), (6, "archive")):
        payload = frames[index]["data"]
        assert (payload["request_id"], payload["run_id"], payload["operation_id"]) == (
            key,
            None,
            None,
        )
    assert cancellation["terminal"]["archived"] is True
    assert cancellation["terminal"]["archive_pending"] is False
    backwards = events(cases["backwards_clock"]["sse"])
    assert backwards[2]["data"]["time"] < backwards[1]["data"]["time"]
    assert cases["revision"]["terminal_run"]["plan"]["revision"] == 2
    fetched = cases["fetch_execution"]
    assert [frame["data"]["type"] for frame in events(fetched["sse"])] == execution_types
    result = fetched["terminal_run"]["results"][0]
    continuation = fetched["artifact_page"]["body"]
    assert result["fetch"]["returned_chars"] == 18
    assert continuation["offset"] == result["fetch"]["next_offset"] == 18
    assert result["content"] + continuation["content"] == (
        "Public evidence 🌱\nContinuation from durable storage."
    )
    recorded = cases["recorded_planning"]
    frames = events(recorded["sse"])
    assert [frame["data"]["type"] for frame in frames] == [
        "run.planning",
        "llm.intent",
        "llm.dispatched",
        "llm.output",
        "llm.output",
        "llm.outcome",
        "llm.validation",
        "planner.result",
        "run.awaiting_approval",
    ]
    initial = frames[0]["data"]["run"]
    for frame in frames[1:-1]:
        event = frame["data"]
        assert event["run"]["status"] == "planning"
        assert event["run"]["plan_identity"] is None
        assert event["run"]["result_count"] == event["run"]["completed_steps"] == 0
        assert (event["run_id"], event["operation_id"]) == (initial["id"], initial["operation_id"])
        assert all(event[key] is None for key in ("request", "plan", "result", "answer", "error"))
    assert recorded["terminal_run"]["status"] == "awaiting_approval"
    assert recorded["terminal_run"]["plan"]["model"] == "deployment-a"

    recovery, llm_recovery = contract["recoveries"]
    assert recovery["name"] == "startup_recovery"
    page = recovery["body"]
    (event,) = page["events"]
    EventV2.model_validate(event)
    assert event["schema_version"] == 2 and event["invalidates_runs"] is True
    assert (event["type"], event["request_id"], event["run_id"], event["operation_id"]) == (
        "run.interrupted",
        "recovery",
        None,
        None,
    )
    assert event["sequence"] == recovery["baseline"]["sequence"] + 1
    assert page["next_after"] == page["through_sequence"] == event["sequence"]
    assert page["has_more"] is False
    assert event["session"] == recovery["terminal"]
    assert event["session"]["current_run"] is None
    assert event["run"]["id"] == recovery["baseline_run"]["id"]
    assert event["run"]["status"] == "interrupted" and event["run"]["operation_id"] is None
    assert event["error"]["code"] == "outcome_unknown"
    assert event["run"]["error"] == event["error"]
    assert event["result"]["status"] == "failed"
    assert event["result"]["error"] == event["error"]
    assert event["result"]["step_id"] == recovery["baseline_run"]["results"][0]["step_id"]
    assert recovery["baseline_run"]["results"][0]["status"] == "running"
    assert recovery["terminal_run"]["results"] == [event["result"]]
    assert recovery["terminal_run"]["error"] == event["error"]
    assert recovery["terminal_run"]["status"] == "interrupted"
    assert recovery["runs_page"]["runs"] == [{**event["run"], "request_preview": CONTENT}]
    assert recovery["results_page"]["results"] == [event["result"]]
    assert event["request"] is event["plan"] is event["answer"] is None
    outcome, interrupted = llm_recovery["body"]["events"]
    for event in (outcome, interrupted):
        EventV2.model_validate(event)
    assert outcome["type"] == "llm.outcome" and outcome["request_id"] == "recovery"
    assert (outcome["run_id"], outcome["operation_id"]) == (
        llm_recovery["baseline_run"]["id"],
        llm_recovery["baseline_run"]["operation_id"],
    )
    assert outcome["invalidates_runs"] is False and outcome["run"]["status"] == "planning"
    assert all(outcome[key] is None for key in ("request", "plan", "result", "answer", "error"))
    assert interrupted["type"] == "run.interrupted" and interrupted["invalidates_runs"] is True
    assert interrupted["run_id"] is interrupted["operation_id"] is None
    assert interrupted["sequence"] == outcome["sequence"] + 1
    assert interrupted["sequence"] == llm_recovery["baseline"]["sequence"] + 2
    assert interrupted["session"] == llm_recovery["terminal"]
    assert llm_recovery["terminal_run"]["plan"] is None
    assert llm_recovery["terminal_run"]["results"] == []


def semantic(contract):
    value = copy.deepcopy(contract)
    for case in value["streams"]:
        # Compare every frame/field, ignoring only JSON whitespace/key ordering.
        case["sse"] = events(case["sse"])
    for case in value["recoveries"]:
        for event in case["body"]["events"]:
            EventV2.model_validate(event)
    return value


async def test_agent_contract_fixture(contract_setup, tmp_path):
    generated = await generate_contract(contract_setup)
    assert_representative(generated)
    serialized = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
    assert len(serialized.encode()) < 150_000, "Keep this representative fixture small"
    assert str(tmp_path) not in serialized
    assert "audit" not in serialized
    for canary in PRIVATE_CANARIES:
        assert canary not in serialized
    if os.environ.get("UPDATE_AGENT_CONTRACT_FIXTURE") == "1":
        assert FIXTURE.parent.is_dir(), "Verify the known UI fixture parent before regenerating"
        FIXTURE.write_text(serialized, encoding="utf-8")
    checked = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert semantic(checked) == semantic(generated), (
        "Public Agent wire fixture drifted. Review the change, then explicitly regenerate "
        "with UPDATE_AGENT_CONTRACT_FIXTURE=1 uv run --offline --frozen pytest "
        "tests/test_agent_contract.py -q"
    )
