import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from azure.core.credentials import AccessToken
from openai import AsyncOpenAI

from app.auth import create_bearer_provider
from app.responses import ChatService


class WireStream(httpx.AsyncByteStream):
    def __init__(self):
        self.closed = False

    async def __aiter__(self):
        events = [
            {
                "type": "response.output_text.delta",
                "delta": "Mocked SDK reply",
                "item_id": "item-1",
                "output_index": 0,
                "content_index": 0,
                "sequence_number": 0,
            },
            {
                "type": "response.completed",
                "sequence_number": 1,
                "response": {
                    "id": "provider-id",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            },
        ]
        for event in events:
            yield f"event: {event['type']}\ndata: {json.dumps(event)}\n\n".encode()
        yield b"data: [DONE]\n\n"

    async def aclose(self):
        self.closed = True


async def test_real_sdk_with_mock_http_transport_and_async_bearer_provider(
    settings_factory, chat_request
):
    settings = settings_factory()
    credential = SimpleNamespace(
        get_token=AsyncMock(return_value=AccessToken("fake-bearer-token", int(time.time()) + 3600))
    )
    received = []
    wire = WireStream()

    def handle(request):
        received.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client,
        AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key=create_bearer_provider(credential),
            http_client=http_client,
            max_retries=0,
        ) as client,
    ):
        events = [event async for event in ChatService(client, settings).events(chat_request)]
    assert [event.event for event in events] == ["start", "delta", "done"]
    assert events[1].text == "Mocked SDK reply"
    assert len(received) == 1
    request = received[0]
    assert str(request.url) == "https://example.openai.azure.com/openai/v1/responses"
    assert request.headers["authorization"] == "Bearer fake-bearer-token"
    body = json.loads(request.content)
    assert body["model"] == "deployment-b"
    assert body["reasoning"] == {"effort": "xhigh"}
    assert body["store"] is False and body["stream"] is True
    assert body["instructions"] == chat_request.system_prompt
    assert body["input"] == [message.model_dump() for message in chat_request.messages]
    assert "previous_response_id" not in body
    assert wire.closed
    credential.get_token.assert_awaited_once()


async def test_real_sdk_http_error_is_sanitized_and_not_retried(settings_factory, chat_request):
    requests = []

    def handle(request):
        requests.append(request)
        return httpx.Response(
            400,
            json={
                "error": {
                    "code": "unsupported_value",
                    "param": "reasoning.effort",
                    "message": "Unsupported value: 'xhigh'; fake-secret and internal details",
                }
            },
        )

    settings = settings_factory()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client,
        AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key="fake-token",
            http_client=http_client,
            max_retries=0,
        ) as client,
    ):
        events = [event async for event in ChatService(client, settings).events(chat_request)]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == "unsupported_reasoning_effort"
    assert "fake-secret" not in events[-1].message
    assert len(requests) == 1


@pytest.mark.parametrize("status", ["failed", "incomplete"])
async def test_real_sdk_terminal_event_shapes(settings_factory, chat_request, status):
    event = {
        "type": f"response.{status}",
        "sequence_number": 0,
        "response": {
            "id": "provider-id",
            "status": status,
            "output": [],
            "error": {"code": "server_error", "message": "hidden"},
            "incomplete_details": {"reason": "max_output_tokens"},
        },
    }
    wire = f"data: {json.dumps(event)}\n\n".encode()

    def handle(request):
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, content=wire)

    settings = settings_factory()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client,
        AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key="fake-token",
            http_client=http_client,
            max_retries=0,
        ) as client,
    ):
        events = [event async for event in ChatService(client, settings).events(chat_request)]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == ("provider_error" if status == "failed" else "output_limit")


async def test_sdk_stream_error_envelope_is_sanitized_and_terminal(settings_factory, chat_request):
    error = {
        "error": {
            "code": "unsupported_value",
            "param": "reasoning.effort",
            "message": "Unsupported value; fake-secret",
        }
    }

    def handle(request):
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"event: error\ndata: {json.dumps(error)}\n\n".encode(),
        )

    settings = settings_factory()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client,
        AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key="fake-token",
            http_client=http_client,
            max_retries=0,
        ) as client,
    ):
        events = [event async for event in ChatService(client, settings).events(chat_request)]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == "unsupported_reasoning_effort"
    assert "fake-secret" not in events[-1].message


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (httpx.ReadError("fake-secret"), "provider_unavailable"),
        (httpx.ReadTimeout("fake-secret"), "provider_timeout"),
    ],
)
async def test_sdk_read_failure_closes_transport_without_retry(
    settings_factory, chat_request, failure, expected_code
):
    class BrokenWireStream(WireStream):
        async def __aiter__(self):
            async for chunk in super().__aiter__():
                yield chunk
                raise failure

    requests = []
    wire = BrokenWireStream()

    def handle(request):
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=wire)

    settings = settings_factory()
    async with (
        httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client,
        AsyncOpenAI(
            base_url=settings.openai_base_url,
            api_key="fake-token",
            http_client=http_client,
            max_retries=0,
        ) as client,
    ):
        events = [event async for event in ChatService(client, settings).events(chat_request)]
    assert [event.event for event in events] == ["start", "delta", "error"]
    assert events[-1].code == expected_code
    assert "fake-secret" not in events[-1].message
    assert len(requests) == 1
    assert wire.closed
