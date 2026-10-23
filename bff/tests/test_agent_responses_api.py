"""Default application wiring exercised offline, with exclusively synthetic storage."""

import asyncio
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from azure.core.credentials import AccessToken

import app.main as main
from app.agent import AgentService, PlanningInput
from app.agent_files import FilePolicy
from app.agent_responses import build_request
from app.auth import TOKEN_SCOPE
from app.sessions import SessionStore
from app.tools import FetchDocument, WebFetcher
from tests.helpers import FakeStream, delta, terminal
from tests.test_agent import BASE, approval, decode, run_detail

URL = "https://example.com/synthetic-article"
CREATE = {"request_id": "create", "model": "deployment-b", "reasoning_effort": "xhigh"}
COMMAND = {"request_id": "plan", "content": f"Read {URL} 原文🌱"}
PRIVATE = "PRIVATE_PROVIDER_EVIDENCE🌱"
TOKEN = "SYNTHETIC_TOKEN"


def provider_body():
    proposal = {
        "objective": "Read the supplied article",
        "assumptions": [],
        "steps": [
            {
                "title": "Fetch",
                "description": "Read static public evidence.",
                "tool": "web_fetch",
                "arguments": {"url": URL, "offset": 0, "limit": 100},
            }
        ],
    }
    return {
        "object": "response",
        "id": "synthetic-provider-id",
        "status": "completed",
        "model": "provider-reported-model",
        "usage": {"input_tokens": 17, "output_tokens": 29},
        "output": [
            {"type": "reasoning", "encrypted_content": PRIVATE, "summary": []},
            {
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": [{"type": "output_text", "text": json.dumps(proposal)}],
            },
        ],
        "error": None,
        "incomplete_details": None,
        "opaque_extra": PRIVATE * 10000,
    }


@pytest.fixture
def wired(tmp_path, monkeypatch, settings_factory, mock_client):
    root = tmp_path / "synthetic-journal"
    settings = settings_factory(provider_timeout_seconds=13, max_output_tokens=1234)
    closed, calls, owners = [], [], []
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken(TOKEN, 4102444800)),
        close=AsyncMock(side_effect=lambda: closed.append("credential")),
    )
    mock_client.close.side_effect = lambda: closed.append("sdk")
    sdk_factory = Mock(return_value=mock_client)
    fetcher = WebFetcher()
    fetcher.fetch = AsyncMock(
        side_effect=lambda url: FetchDocument(
            requested_url=url,
            final_url=url,
            title="Synthetic article",
            status_code=200,
            content_type="text/plain",
            fetched_at="2026-10-23T00:00:00Z",
            redirects=(),
            text="Synthetic public evidence",
        )
    )
    files = FilePolicy({})
    files.read = Mock(side_effect=AssertionError("No source file reads are expected"))
    result = SimpleNamespace(
        root=root,
        settings=settings,
        credential=credential,
        sdk=mock_client,
        sdk_factory=sdk_factory,
        calls=calls,
        closed=closed,
        owners=owners,
        fetcher=fetcher,
        files=files,
        handler=lambda request: httpx.Response(200, json=provider_body()),
    )

    def service(**kwargs):
        owner = AgentService(store=SessionStore(root), files=files, fetcher=fetcher, **kwargs)
        original_close = owner.close

        async def close():
            await original_close()
            closed.append("agent")

        owner.close = AsyncMock(side_effect=close)
        owners.append(owner)
        return owner

    result.service_factory = Mock(side_effect=service)
    monkeypatch.setattr(main, "AgentService", result.service_factory)

    def http_factory(*, timeout):
        def dispatch(request):
            calls.append(request)
            return result.handler(request)

        client = httpx.AsyncClient(
            transport=httpx.MockTransport(dispatch),
            timeout=timeout,
            trust_env=False,
        )
        original_close = client.aclose

        async def close():
            await original_close()
            closed.append("http")

        client.aclose = AsyncMock(side_effect=close)
        result.http = client
        return client

    result.http_factory = Mock(side_effect=http_factory)

    def application(**overrides):
        return main.create_app(
            settings,
            credential_factory=Mock(return_value=credential),
            client_factory=overrides.pop("client_factory", sdk_factory),
            agent_http_client_factory=result.http_factory,
            **overrides,
        )

    @asynccontextmanager
    async def opened():
        app = application()
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://test",
                trust_env=False,
            ) as client:
                yield client, app.state.agent_service, app

    result.application, result.opened = application, opened
    return result


def audits(owner, sid):
    intents = [r.audit for r in owner.store.records[sid] if r.event.type == "llm.intent"]
    return [
        [
            row["record"]
            for row in owner.store.llm_records(
                sid,
                intent["identity"]["attempt_id"],
            )["records"]
        ]
        for intent in intents
    ]


async def plan(client):
    created = await client.post(BASE, json=CREATE)
    assert created.status_code == 200
    sid = created.json()["id"]
    response = await client.post(f"{BASE}/{sid}/runs", json=COMMAND)
    frames = decode(response)
    return sid, await run_detail(client, sid, frames[-1]["run_id"]), response


async def test_lazy_startup_shared_bearer_chat_and_ordered_shutdown(
    wired,
    monkeypatch,
    chat_request,
):
    start = Mock(side_effect=AssertionError("Startup must not open session storage"))
    monkeypatch.setattr(SessionStore, "start", start)
    async with wired.opened() as (client, owner, _):
        assert (await client.get("/health")).json() == {"status": "ok"}
        assert (await client.get("/api/models")).json() == {
            "models": ["deployment-a", "deployment-b"],
            "default_model": "deployment-a",
        }
        assert owner.models == ("deployment-a", "deployment-b")
        assert owner.eager is False
        assert not wired.root.exists()
        start.assert_not_called()
        wired.credential.get_token.assert_not_awaited()
        wired.sdk.responses.create.assert_not_awaited()
        assert wired.calls == []
        wired.http_factory.assert_called_once_with(timeout=13)
        sdk_args = wired.sdk_factory.call_args.kwargs
        assert sdk_args["max_retries"] == 0 and sdk_args["timeout"] == 13
        assert sdk_args["base_url"] == wired.settings.openai_base_url
        assert owner.planner.transport.bearer_provider is sdk_args["api_key"]
        assert await sdk_args["api_key"]() == TOKEN
        assert await owner.planner.transport.bearer_provider() == TOKEN
        wired.credential.get_token.assert_awaited_once_with(TOKEN_SCOPE)
        wired.sdk.responses.create.return_value = FakeStream(delta("Chat preserved"), terminal())
        response = await client.post("/api/chat", json=chat_request.model_dump())
        assert response.status_code == 200 and "Chat preserved" in response.text
        wired.sdk.responses.create.assert_awaited_once()
        assert wired.calls == []
    assert wired.closed == ["agent", "http", "sdk", "credential"]
    assert wired.http.is_closed


async def test_plan_revision_exact_wire_private_retention_and_approval(wired):
    async with wired.opened() as (client, owner, _):
        sid, run, response = await plan(client)
        assert run["status"] == "awaiting_approval"
        wired.fetcher.fetch.assert_not_awaited()
        wired.files.read.assert_not_called()
        path = f"{BASE}/{sid}/runs/{run['id']}"
        assert (await client.post(f"{BASE}/{sid}/runs", json=COMMAND)).status_code == 409
        assert len(wired.calls) == 1
        revision = {**approval(run, "revise"), "kind": "feedback", "content": "Keep it short 🌱"}
        revised_response = await client.post(path + "/revise", json=revision)
        frames = decode(revised_response)
        revised = await run_detail(client, sid, frames[-1]["run_id"])
        assert revised["id"] == run["id"]
        assert revised["plan"]["id"] == run["plan"]["id"]
        assert revised["plan"]["revision"] == 2
        assert revised["plan"]["digest"] != run["plan"]["digest"]
        assert revised["plan"]["steps"][0]["id"] != run["plan"]["steps"][0]["id"]
        assert (await client.post(path + "/revise", json=revision)).status_code == 409
        assert (await client.post(path + "/execute", json=approval(run))).status_code == 409
        assert len(wired.calls) == 2
        wired.fetcher.fetch.assert_not_awaited()
        groups = audits(owner, sid)
        assert len(groups) == 2
        for index, (request, records) in enumerate(zip(wired.calls, groups, strict=True)):
            context = PlanningInput(
                request=COMMAND["content"],
                model="deployment-b",
                reasoning_effort="xhigh",
                workspaces=(),
                available_tools=owner.registry.available(),
                feedback=revision["content"] if index else None,
            )
            canonical = build_request(context, 1234)
            assert request.content == canonical.body_bytes
            assert str(request.url) == wired.settings.openai_base_url + "responses"
            assert request.method == "POST"
            assert request.headers["authorization"] == "Bearer " + TOKEN
            assert request.headers["content-type"] == "application/json"
            assert canonical.body["store"] is canonical.body["stream"] is False
            assert records[0]["data"]["request"] == canonical.body
            assert [r["kind"] for r in records] == [
                "intent",
                "dispatched",
                "output",
                "outcome",
                "validation",
            ]
            output = records[2]["data"]
            assert output["body"] == provider_body()
            assert output["reported_model"] == "provider-reported-model"
            assert output["usage"] == {"input_tokens": 17, "output_tokens": 29}
            assert records[-1]["data"]["status"] == "valid"
            identity = records[0]["identity"]
            assert all(r["identity"] == identity for r in records)
            assert identity["session_id"] == sid and identity["run_id"] == run["id"]
            assert identity["plan_id"] == run["plan"]["id"]
            assert identity["revision"] == index + 1
            assert identity["phase"] == ("revision" if index else "planning")
            assert identity["attempt"] == 1 and identity["retry_of"] is None
        for key in ("operation_id", "stage_id", "interaction_id", "attempt_id"):
            assert groups[0][0]["identity"][key] != groups[1][0]["identity"][key]
        public = response.text + revised_response.text
        for suffix in ("", "/runs", "/events?after=0&limit=100", f"/runs/{run['id']}"):
            public += (await client.get(f"{BASE}/{sid}" + suffix)).text
        assert PRIVATE not in public and '"audit"' not in public
        assert TOKEN not in json.dumps(groups)
        executed = decode(await client.post(path + "/execute", json=approval(revised)))
        assert executed[-1]["type"] == "run.completed"
        wired.fetcher.fetch.assert_awaited_once_with(URL)
        for key in ("execute", "another-execute"):
            duplicate = await client.post(path + "/execute", json=approval(revised, key))
            assert duplicate.status_code == 409
        assert len(wired.calls) == 2
        wired.fetcher.fetch.assert_awaited_once()
        wired.sdk.responses.create.assert_not_awaited()
    # Reopen only the synthetic store: full private bodies survive application shutdown.
    recovered = SessionStore(wired.root)
    recovered.start()
    try:
        assert audits(SimpleNamespace(store=recovered), sid) == groups
    finally:
        recovered.close()


@pytest.mark.parametrize("failure", ["http", "refusal", "read"])
async def test_provider_failures_retain_evidence_without_retry_or_tools(wired, failure):
    full = provider_body()
    if failure == "refusal":
        full["output"][1]["content"] = [{"type": "refusal", "refusal": PRIVATE}]

    class BrokenStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"synthetic_partial":'
            raise httpx.ReadError("PRIVATE_READ_EXCEPTION")

        async def aclose(self):
            closed.set()

    closed = asyncio.Event()
    wired.handler = lambda request: (
        httpx.Response(200, stream=BrokenStream())
        if failure == "read"
        else httpx.Response(503 if failure == "http" else 200, json=full)
    )
    async with wired.opened() as (client, owner, _):
        sid, run, response = await plan(client)
        assert run["status"] == "failed" and run["plan"] is None
        assert run["error"]["code"] == "planner_unavailable"
        assert len(wired.calls) == 1
        assert (await client.post(f"{BASE}/{sid}/runs", json=COMMAND)).status_code == 409
        assert len(wired.calls) == 1
        (records,) = audits(owner, sid)
        (output,) = [r["data"] for r in records if r["kind"] == "output"]
        assert output["failed"] is True
        if failure == "read":
            assert closed.is_set()
            assert output["body"]["partial"] is True
            assert output["body"]["data"] == '{"synthetic_partial":'
        else:
            assert output["body"] == full
        assert PRIVATE not in response.text and "PRIVATE_READ_EXCEPTION" not in response.text
        assert "PRIVATE_READ_EXCEPTION" not in json.dumps(records)
        wired.fetcher.fetch.assert_not_awaited()
        wired.files.read.assert_not_called()
        wired.sdk.responses.create.assert_not_awaited()


@pytest.mark.parametrize("failure", ["sdk", "http", "planner", "service", "start"])
async def test_construction_failure_closes_previously_opened_resources(wired, monkeypatch, failure):
    error = RuntimeError("Synthetic construction failure")
    overrides = {}
    expected = ["credential"]
    if failure == "sdk":
        overrides["client_factory"] = Mock(side_effect=error)
    else:
        expected.insert(0, "sdk")
        if failure == "http":
            wired.http_factory.side_effect = error
        else:
            expected.insert(0, "http")
            if failure == "planner":
                monkeypatch.setattr(main, "build_recorded_planner", Mock(side_effect=error))
            elif failure == "service":
                wired.service_factory.side_effect = error
            else:
                monkeypatch.setattr(AgentService, "start", AsyncMock(side_effect=error))
                expected.insert(0, "agent")
    app = wired.application(**overrides)
    with pytest.raises(RuntimeError, match="Synthetic construction failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup must fail")
    assert wired.closed == expected
    assert not wired.root.exists()
    assert wired.calls == []
    wired.credential.get_token.assert_not_awaited()


async def test_targeted_stop_retains_partial_body_and_closes_provider_stream(wired):
    entered, closed = asyncio.Event(), asyncio.Event()

    class BlockedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"synthetic_partial":'
            entered.set()
            await asyncio.Event().wait()

        async def aclose(self):
            closed.set()

    wired.handler = lambda request: httpx.Response(200, stream=BlockedStream())
    async with wired.opened() as (client, owner, _):
        sid = (await client.post(BASE, json=CREATE)).json()["id"]
        pending = asyncio.create_task(client.post(f"{BASE}/{sid}/runs", json=COMMAND))
        try:
            await asyncio.wait_for(entered.wait(), 3)
            (op,) = owner.active.values()
            response = await client.post(
                f"{BASE}/{sid}/runs/{op.rid}/cancel",
                json={"request_id": "stop", "operation_id": op.id},
            )
            assert response.status_code in (200, 202)
            frames = decode(await asyncio.wait_for(asyncio.shield(pending), 3))
            assert frames[-1]["type"] == "run.cancelled"
            assert (await run_detail(client, sid, op.rid))["status"] == "cancelled"
            (records,) = audits(owner, sid)
            (output,) = [r["data"] for r in records if r["kind"] == "output"]
            assert output["body"]["partial"] is True
            assert output["body"]["data"] == '{"synthetic_partial":'
            (outcome,) = [r["data"] for r in records if r["kind"] == "outcome"]
            assert outcome["status"] == "cancelled"
            assert closed.is_set() and not owner.active
            assert len(wired.calls) == 1
            wired.fetcher.fetch.assert_not_awaited()
            wired.files.read.assert_not_called()
        finally:
            if not pending.done():
                pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)


async def test_custom_service_factory_bypasses_agent_http(wired):
    custom = AgentService(SessionStore(wired.root), eager=False)
    custom.close = AsyncMock(wraps=custom.close)
    factory = Mock(return_value=custom)
    wired.http_factory.side_effect = AssertionError("Custom service must bypass agent HTTP")
    app = wired.application(agent_service_factory=factory)
    async with app.router.lifespan_context(app):
        assert app.state.agent_service is custom
        factory.assert_called_once_with(wired.settings)
        wired.service_factory.assert_not_called()
        wired.http_factory.assert_not_called()
        assert not wired.root.exists()
    custom.close.assert_awaited_once()
    assert wired.closed == ["sdk", "credential"]


async def test_shutdown_owns_http_close_through_repeated_native_cancellation(wired, monkeypatch):
    entered, release, completed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    original_factory = wired.http_factory.side_effect

    def http_factory(*, timeout):
        client = original_factory(timeout=timeout)
        transport = client._transport
        original_close = transport.aclose

        async def close():
            entered.set()
            await release.wait()
            await original_close()
            completed.set()

        monkeypatch.setattr(transport, "aclose", AsyncMock(side_effect=close))
        return client

    wired.http_factory.side_effect = http_factory
    app = wired.application()

    async def lifecycle():
        async with app.router.lifespan_context(app):
            pass

    task = asyncio.create_task(lifecycle())
    try:
        await asyncio.wait_for(entered.wait(), 3)
        assert wired.closed == ["agent"]
        for count in (1, 2):
            assert task.cancel()
            # Deliver each native cancellation while transport closure is suspended.
            await asyncio.sleep(0)
            assert task.cancelling() == count
            assert not task.done()
            assert not completed.is_set()
            wired.sdk.close.assert_not_awaited()
            wired.credential.close.assert_not_awaited()
            assert wired.closed == ["agent"]
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.shield(task), 3)
        assert task.cancelled()
        assert completed.is_set()
        assert wired.closed == ["agent", "http", "sdk", "credential"]
        wired.http.aclose.assert_awaited_once()
        wired.sdk.close.assert_awaited_once()
        wired.credential.close.assert_awaited_once()
        wired.credential.get_token.assert_not_awaited()
        assert wired.calls == [] and not wired.root.exists()
    finally:
        release.set()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 3)
