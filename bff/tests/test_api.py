import asyncio
import json
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from starlette.requests import ClientDisconnect

from app.main import create_app
from tests.helpers import FakeStream, delta, terminal
from tests.test_responses import BlockingStream, status_error


def decode_frames(text):
    frames = []
    for block in text.strip().split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines())
        frames.append({**fields, "data": json.loads(fields["data"])})
    return frames


async def test_models_health_and_lifespan_do_not_infer_or_fetch_tokens(app_factory, mock_client):
    app, credential, client_factory = app_factory(
        available_models=" deployment-b, deployment-a, deployment-b ", default_model="deployment-a"
    )
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.get("/health")).json() == {"status": "ok"}
            response = await client.get("/api/models")
            assert response.status_code == 200
            assert response.json() == {
                "models": ["deployment-b", "deployment-a"],
                "default_model": "deployment-a",
            }
        arguments = client_factory.call_args.kwargs
        assert arguments["base_url"] == "https://example.openai.azure.com/openai/v1/"
        assert arguments["timeout"] == 60
        assert arguments["max_retries"] == 0
        assert callable(arguments["api_key"])
        credential.get_token.assert_not_awaited()
        mock_client.responses.create.assert_not_awaited()
    mock_client.close.assert_awaited_once()
    credential.close.assert_awaited_once()


async def test_sse_contract_and_ids(app_factory, mock_client, chat_request):
    stream = FakeStream(delta("Hello\n"), delta("世界"), terminal())
    mock_client.responses.create.return_value = stream
    app, _, _ = app_factory()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/chat", json=chat_request.model_dump())
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    frames = decode_frames(response.text)
    assert [frame["event"] for frame in frames] == ["start", "delta", "delta", "done"]
    common = {"session_id", "request_id", "message_id"}
    assert set(frames[0]["data"]) == common | {"time"}
    assert set(frames[1]["data"]) == common | {"text"}
    assert set(frames[-1]["data"]) == common | {"time", "status"}
    message_id = frames[0]["data"]["message_id"]
    for sequence, frame in enumerate(frames):
        assert frame["id"] == f"{message_id}:{sequence}"
        assert frame["data"]["session_id"] == "session-1"
        assert frame["data"]["request_id"] == "request-1"
        assert frame["data"]["message_id"] == message_id
    assert frames[0]["data"]["time"] == frames[-1]["data"]["time"]
    assert frames[0]["data"]["time"].endswith("Z")
    assert (
        "".join(frame["data"]["text"] for frame in frames if frame["event"] == "delta")
        == "Hello\n世界"
    )
    stream.close.assert_awaited_once()


@pytest.mark.parametrize(
    "update",
    [
        {"model": "unlisted"},
        {"reasoning_effort": "none"},
        {"reasoning_effort": "XHIGH"},
        {"session_id": ""},
        {"request_id": 42},
        {"messages": []},
        {"messages": [{"role": "system", "content": "No"}]},
        {"messages": [{"role": "user", "content": ""}]},
        {"messages": [{"role": "user", "content": "Text", "extra": True}]},
        {"requestId": "camelCase is not accepted"},
        {"extra": "not accepted"},
    ],
)
async def test_validation_errors_are_json_before_stream(
    app_factory, mock_client, chat_request, update
):
    app, _, _ = app_factory()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/chat", json={**chat_request.model_dump(), **update})
    assert response.status_code == 422
    assert response.headers["content-type"] == "application/json"
    assert "detail" in response.json()
    mock_client.responses.create.assert_not_awaited()


@pytest.mark.parametrize(
    "missing",
    ["session_id", "request_id", "model", "reasoning_effort", "system_prompt", "messages"],
)
async def test_all_request_fields_are_explicitly_required(app_factory, chat_request, missing):
    app, _, _ = app_factory()
    body = chat_request.model_dump()
    del body[missing]
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            assert (await client.post("/api/chat", json=body)).status_code == 422


async def test_runtime_error_has_exact_terminal_contract(app_factory, mock_client, chat_request):
    mock_client.responses.create.side_effect = status_error(
        400, {"message": "Unsupported reasoning effort: xhigh; fake-secret"}
    )
    app, _, _ = app_factory()
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://test"
        ) as client:
            response = await client.post("/api/chat", json=chat_request.model_dump())
    assert response.status_code == 200
    frames = decode_frames(response.text)
    assert [frame["event"] for frame in frames] == ["start", "error"]
    assert set(frames[-1]["data"]) == {"session_id", "request_id", "message_id", "code", "message"}
    assert frames[-1]["data"]["code"] == "unsupported_reasoning_effort"
    assert "fake-secret" not in response.text
    assert frames[1]["id"].endswith(":1")


def request_scope(spec_version="2.0"):
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": spec_version},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/api/chat",
        "raw_path": b"/api/chat",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("test", 123),
        "server": ("test", 80),
        "root_path": "",
    }


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_asgi_disconnect_cancels_provider_read_and_closes_stream(
    app_factory, mock_client, chat_request, spec_version
):
    app, _, _ = app_factory()
    stream = BlockingStream()
    mock_client.responses.create.return_value = stream
    body_received = False
    sent = []
    cleanup_completed = False

    async def delayed_close():
        nonlocal cleanup_completed
        await asyncio.sleep(0.01)
        cleanup_completed = True

    stream.close.side_effect = delayed_close

    async def receive():
        nonlocal body_received
        if not body_received:
            body_received = True
            return {
                "type": "http.request",
                "body": chat_request.model_dump_json().encode(),
                "more_body": False,
            }
        await stream.waiting.wait()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async with app.router.lifespan_context(app), asyncio.timeout(1):
        await app(request_scope(spec_version), receive, send)
    stream.close.assert_awaited_once()
    assert cleanup_completed
    frames = decode_frames(b"".join(message.get("body", b"") for message in sent).decode())
    assert [frame["event"] for frame in frames] == ["start"]


async def test_asgi_forwards_delta_while_provider_has_not_completed(
    app_factory, mock_client, chat_request
):
    release_provider = asyncio.Event()
    delta_sent = asyncio.Event()

    class GatedStream(FakeStream):
        async def __anext__(self):
            if self.reads == 1:
                await release_provider.wait()
            return await super().__anext__()

    stream = GatedStream(delta("Immediate"), terminal())
    mock_client.responses.create.return_value = stream
    app, _, _ = app_factory()
    body_received = False
    sent = []

    async def receive():
        nonlocal body_received
        if body_received:
            await asyncio.Event().wait()
        body_received = True
        return {
            "type": "http.request",
            "body": chat_request.model_dump_json().encode(),
            "more_body": False,
        }

    async def send(message):
        sent.append(message)
        if b"event: delta" in message.get("body", b""):
            delta_sent.set()

    async with app.router.lifespan_context(app), asyncio.timeout(1):
        task = asyncio.create_task(app(request_scope(), receive, send))
        try:
            await delta_sent.wait()
            assert not task.done()
            frames = decode_frames(b"".join(message.get("body", b"") for message in sent).decode())
            assert [frame["event"] for frame in frames] == ["start", "delta"]
            assert frames[-1]["data"]["text"] == "Immediate"
        finally:
            release_provider.set()
            await task
    stream.close.assert_awaited_once()


async def test_failed_asgi_send_closes_suspended_generator(app_factory, mock_client, chat_request):
    app, _, _ = app_factory()
    stream = FakeStream(delta("First"), delta("Never sent"), terminal())
    mock_client.responses.create.return_value = stream
    sent = []
    body_received = False

    async def receive():
        nonlocal body_received
        if body_received:
            await asyncio.Event().wait()
        body_received = True
        return {
            "type": "http.request",
            "body": chat_request.model_dump_json().encode(),
            "more_body": False,
        }

    async def send(message):
        sent.append(message)
        if b"event: delta" in message.get("body", b""):
            raise OSError("Disconnected client")

    async with app.router.lifespan_context(app), asyncio.timeout(1):
        with pytest.raises(ClientDisconnect):
            await app(request_scope("2.4"), receive, send)
    assert stream.reads == 1
    stream.close.assert_awaited_once()


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
@pytest.mark.parametrize("start_delay", [0, 0.08])
async def test_blocked_delta_send_uses_remaining_deadline_and_unwinds(
    app_factory, mock_client, chat_request, spec_version, start_delay
):
    timeout = 0.12
    app, _, _ = app_factory(request_timeout_seconds=timeout)
    stream = FakeStream(delta("First"), delta("Must not be read"), terminal())
    mock_client.responses.create.return_value = stream
    body_received = False
    sent = []
    delta_blocked_at = None
    delta_cancelled_at = None
    cleanup_completed = False

    async def delayed_close():
        nonlocal cleanup_completed
        await asyncio.sleep(0.01)
        cleanup_completed = True

    stream.close.side_effect = delayed_close

    async def receive():
        nonlocal body_received
        if body_received:
            await asyncio.Event().wait()
        body_received = True
        return {
            "type": "http.request",
            "body": chat_request.model_dump_json().encode(),
            "more_body": False,
        }

    async def send(message):
        nonlocal delta_blocked_at, delta_cancelled_at
        sent.append(message)
        if b"event: start" in message.get("body", b""):
            await asyncio.sleep(start_delay)
        if b"event: delta" in message.get("body", b""):
            delta_blocked_at = asyncio.get_running_loop().time()
            try:
                await asyncio.Event().wait()
            finally:
                delta_cancelled_at = asyncio.get_running_loop().time()

    async with app.router.lifespan_context(app), asyncio.timeout(1):
        await app(request_scope(spec_version), receive, send)
    assert delta_blocked_at is not None and delta_cancelled_at is not None
    assert delta_cancelled_at - delta_blocked_at < timeout - start_delay + 0.04
    assert cleanup_completed
    assert stream.reads == 1
    stream.close.assert_awaited_once()
    frames = decode_frames(b"".join(message.get("body", b"") for message in sent).decode())
    assert [frame["event"] for frame in frames] == ["start", "delta"]
    assert not any(message.get("more_body") is False for message in sent)


async def test_lifespan_closes_credential_when_client_creation_fails(settings_factory):
    credential = Mock(close=AsyncMock())
    app = create_app(
        settings_factory(),
        credential_factory=Mock(return_value=credential),
        client_factory=Mock(side_effect=RuntimeError("fake client failure")),
    )
    with pytest.raises(RuntimeError, match="fake client failure"):
        async with app.router.lifespan_context(app):
            pytest.fail("Startup should fail")
    credential.close.assert_awaited_once()


async def test_lifespan_closes_credential_even_if_client_close_fails(app_factory, mock_client):
    app, credential, _ = app_factory()
    mock_client.close.side_effect = RuntimeError("fake close failure")
    with pytest.raises(RuntimeError, match="fake close failure"):
        async with app.router.lifespan_context(app):
            pass
    credential.close.assert_awaited_once()
