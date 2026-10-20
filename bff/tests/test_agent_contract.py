"""Public BFF wire fixtures consumed by Angular, with no live services.

From bff/, explicitly regenerate only the checked-in public JSON with:
    UPDATE_AGENT_CONTRACT_FIXTURE=1 uv run pytest tests/test_agent_contract.py -q
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
from app.agent_schemas import AgentEvent, Proposal
from app.main import create_app
from app.sessions import SessionStore
from app.tools import FetchDocument, WebFetcher

FIXTURE = Path(__file__).resolve().parents[2] / "ui/src/app/agent-contract.fixture.json"
BASE = "/api/agent/sessions"
NOW = "2026-10-16T09:00:00.123456Z"
EARLIER = "2026-10-16T08:59:59.123456Z"
CONTENT = "Read the public fixture"


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
    async def opened(suffix, *, blocked=False, backwards=False, enabled=True, fetched=False):
        directory = tmp_path / suffix
        workspace = directory / "workspace"
        workspace.mkdir(parents=True)
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
            directory / "journal",
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
        AgentEvent.model_validate(payload)
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


def identity(snapshot):
    run = snapshot["runs"][0]
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
                **identity(planning["terminal"]),
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
            {"action": "execute", **identity(revision["terminal"])},
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
            {"action": "execute", **identity(baseline)},
        )
        case["controls"] = [title]
        streams.append(case)

    async with opened("ct03", blocked=True) as setup:
        client = setup.client
        planning = await stream(
            client, "setup", await create(client), "plan", {"action": "runs", "content": CONTENT}
        )
        baseline = planning["terminal"]
        rid = baseline["runs"][0]["id"]
        case = exchange(
            "cancellation_archive", baseline, "execute", {"action": "execute", **identity(baseline)}
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
            assert old_stop["session"]["runs"][0]["status"] == "executing"
            stop = await control(
                client,
                "POST",
                url,
                {"request_id": "stop", "operation_id": executing["runs"][0]["operation_id"]},
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
            {"action": "execute", **identity(planning["terminal"])},
        )
        metadata = case["terminal"]["runs"][0]["results"][0]["fetch"]
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

    return {"version": 1, "streams": streams, "errors": errors}


def assert_representative(contract):
    cases = {case["name"]: case for case in contract["streams"]}
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
        result = case["terminal"]["runs"][0]["results"][0]
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
    assert cases["revision"]["terminal"]["runs"][0]["plan"]["revision"] == 2
    fetched = cases["fetch_execution"]
    assert [frame["data"]["type"] for frame in events(fetched["sse"])] == execution_types
    result = fetched["terminal"]["runs"][0]["results"][0]
    continuation = fetched["artifact_page"]["body"]
    assert result["fetch"]["returned_chars"] == 18
    assert continuation["offset"] == result["fetch"]["next_offset"] == 18
    assert result["content"] + continuation["content"] == (
        "Public evidence 🌱\nContinuation from durable storage."
    )


def semantic(contract):
    value = copy.deepcopy(contract)
    for case in value["streams"]:
        # Compare every frame/field, ignoring only JSON whitespace/key ordering.
        case["sse"] = events(case["sse"])
    return value


async def test_agent_contract_fixture(contract_setup, tmp_path):
    generated = await generate_contract(contract_setup)
    assert_representative(generated)
    serialized = json.dumps(generated, ensure_ascii=False, indent=2) + "\n"
    assert len(serialized.encode()) < 150_000, "Keep this representative fixture small"
    assert str(tmp_path) not in serialized
    assert "audit" not in serialized
    if os.environ.get("UPDATE_AGENT_CONTRACT_FIXTURE") == "1":
        assert FIXTURE.parent.is_dir(), "Verify the known UI fixture parent before regenerating"
        FIXTURE.write_text(serialized, encoding="utf-8")
    checked = json.loads(FIXTURE.read_text(encoding="utf-8"))
    assert semantic(checked) == semantic(generated), (
        "Public Agent wire fixture drifted. Review the change, then explicitly regenerate "
        "with UPDATE_AGENT_CONTRACT_FIXTURE=1 uv run pytest tests/test_agent_contract.py -q"
    )
