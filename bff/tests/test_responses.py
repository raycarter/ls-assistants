import asyncio
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APIStatusError, APITimeoutError

from app.responses import ChatService
from app.schemas import DoneEvent, ErrorEvent
from tests.helpers import FakeStream, delta, terminal


def status_error(status, body=None):
    request = httpx.Request("POST", "https://example.openai.azure.com/openai/v1/responses")
    return APIStatusError(
        "provider details must not be printed",
        response=httpx.Response(status, request=request),
        body=body,
    )


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh"])
@pytest.mark.parametrize("prompt", [None, "", "Keep the exact prompt.\nSecond line."])
async def test_exact_request_mapping(settings_factory, mock_client, chat_request, effort, prompt):
    chat = chat_request.model_copy(update={"reasoning_effort": effort, "system_prompt": prompt})
    stream = FakeStream(delta(), terminal())
    mock_client.responses.create.return_value = stream
    service = ChatService(mock_client, settings_factory(max_output_tokens=1234))
    events = [event async for event in service.events(chat)]
    arguments = mock_client.responses.create.await_args.kwargs
    expected = {
        "model": "deployment-b",
        "reasoning": {"effort": effort},
        "input": [message.model_dump() for message in chat.messages],
        "store": False,
        "stream": True,
        "max_output_tokens": 1234,
    }
    if prompt is not None:
        expected["instructions"] = prompt
    assert arguments == expected
    assert isinstance(events[-1], DoneEvent)
    stream.close.assert_awaited_once()


async def test_deltas_are_immediate_and_terminal_identity_is_stable(
    settings_factory, mock_client, chat_request
):
    stream = FakeStream(delta("First"), delta(" second"), terminal(), delta("must not be read"))
    mock_client.responses.create.return_value = stream
    events = ChatService(mock_client, settings_factory()).events(chat_request)
    start = await anext(events)
    mock_client.responses.create.assert_not_awaited()
    first = await anext(events)
    assert first.text == "First"
    assert stream.reads == 1
    second = await anext(events)
    assert second.text == " second"
    assert stream.reads == 2
    done = await anext(events)
    assert done.message_id == first.message_id == second.message_id == start.message_id
    assert done.time == start.time
    assert done.status == "complete"
    assert done.session_id == start.session_id == chat_request.session_id
    assert done.request_id == start.request_id == chat_request.request_id
    with pytest.raises(StopAsyncIteration):
        await anext(events)
    assert stream.reads == 3
    stream.close.assert_awaited_once()


async def test_reasoning_and_final_snapshots_are_not_emitted(
    settings_factory, mock_client, chat_request
):
    stream = FakeStream(
        SimpleNamespace(type="response.reasoning_text.delta", delta="private reasoning"),
        SimpleNamespace(type="response.reasoning_summary_text.delta", delta="private summary"),
        delta("Visible text"),
        SimpleNamespace(type="response.output_text.done", text="Visible text"),
        terminal(
            output=[
                SimpleNamespace(content=[SimpleNamespace(type="output_text", text="Visible text")])
            ]
        ),
    )
    mock_client.responses.create.return_value = stream
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert [event.event for event in events] == ["start", "delta", "done"]
    assert events[1].text == "Visible text"


@pytest.mark.parametrize(
    ("provider_event", "expected_code"),
    [
        (
            terminal("failed", error=SimpleNamespace(code="server_error", message="secret")),
            "provider_error",
        ),
        (terminal("incomplete", reason="max_output_tokens"), "output_limit"),
        (terminal("incomplete", reason="content_filter"), "content_filtered"),
        (terminal("incomplete", reason="unknown"), "incomplete_response"),
        (SimpleNamespace(type="response.refusal.delta", delta="secret refusal"), "refusal"),
        (SimpleNamespace(type="response.refusal.done", refusal="secret refusal"), "refusal"),
        (
            SimpleNamespace(type="error", code="content_filter", message="secret"),
            "content_filtered",
        ),
        (
            SimpleNamespace(
                type="error",
                code="unsupported_value",
                message="Unsupported value",
                param="reasoning.effort",
            ),
            "unsupported_reasoning_effort",
        ),
    ],
)
async def test_provider_terminal_failures(
    settings_factory, mock_client, chat_request, provider_event, expected_code
):
    stream = FakeStream(delta("Partial"), provider_event, terminal())
    mock_client.responses.create.return_value = stream
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert [event.event for event in events] == ["start", "delta", "error"]
    assert events[-1].code == expected_code
    assert "secret" not in events[-1].message
    assert stream.reads == 2
    stream.close.assert_awaited_once()


@pytest.mark.parametrize("kind", ["completed", "output_item.added", "output_item.done"])
async def test_refusal_in_output_is_detected(settings_factory, mock_client, chat_request, kind):
    item = SimpleNamespace(content=[SimpleNamespace(type="refusal", refusal="not forwarded")])
    event = (
        terminal(output=[item])
        if kind == "completed"
        else SimpleNamespace(type=f"response.{kind}", item=item)
    )
    mock_client.responses.create.return_value = FakeStream(event)
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == "refusal"


@pytest.mark.parametrize("prefix", [[], [delta("")]])
async def test_completed_without_text_is_an_error(
    settings_factory, mock_client, chat_request, prefix
):
    mock_client.responses.create.return_value = FakeStream(*prefix, terminal())
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == "no_output"


@pytest.mark.parametrize("prefix", [[], [delta("Partial")]])
async def test_eof_without_terminal_is_an_error(
    settings_factory, mock_client, chat_request, prefix
):
    stream = FakeStream(*prefix)
    mock_client.responses.create.return_value = stream
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert isinstance(events[-1], ErrorEvent)
    assert events[-1].code == "stream_interrupted"
    stream.close.assert_awaited_once()


@pytest.mark.parametrize(
    ("failure", "expected_code"),
    [
        (
            status_error(
                400,
                {"code": "unsupported_value", "message": "reasoning effort not supported; secret"},
            ),
            "unsupported_reasoning_effort",
        ),
        (
            status_error(400, {"error": {"code": "context_length_exceeded", "message": "secret"}}),
            "context_length_exceeded",
        ),
        (status_error(400, {"message": "secret"}), "invalid_provider_request"),
        (status_error(401), "provider_authentication_error"),
        (status_error(403), "provider_permission_denied"),
        (status_error(404), "model_unavailable"),
        (status_error(429), "rate_limited"),
        (status_error(500), "provider_error"),
        (
            APIConnectionError(request=httpx.Request("POST", "https://example.com")),
            "provider_unavailable",
        ),
        (APITimeoutError(request=httpx.Request("POST", "https://example.com")), "provider_timeout"),
        (ValueError("secret must not leak"), "internal_error"),
    ],
)
async def test_runtime_errors_are_sanitized(
    settings_factory, mock_client, chat_request, failure, expected_code
):
    mock_client.responses.create.side_effect = failure
    events = [
        event
        async for event in ChatService(mock_client, settings_factory(max_retries=0)).events(
            chat_request
        )
    ]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == expected_code
    assert "secret" not in events[-1].message
    assert "Traceback" not in events[-1].model_dump_json()


async def test_transient_open_failures_retry_with_one_start(
    settings_factory, mock_client, chat_request
):
    stream = FakeStream(delta(), terminal())
    mock_client.responses.create.side_effect = [status_error(429), status_error(503), stream]
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert [event.event for event in events] == ["start", "delta", "done"]
    assert mock_client.responses.create.await_count == 3


async def test_retry_budget_is_bounded(settings_factory, mock_client, chat_request):
    mock_client.responses.create.side_effect = status_error(503)
    events = [
        event
        async for event in ChatService(mock_client, settings_factory(max_retries=1)).events(
            chat_request
        )
    ]
    assert mock_client.responses.create.await_count == 2
    assert events[-1].code == "provider_error"


async def test_invalid_reasoning_is_not_retried(settings_factory, mock_client, chat_request):
    mock_client.responses.create.side_effect = status_error(
        400, {"message": "Unsupported value: xhigh", "param": "reasoning.effort"}
    )
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert events[-1].code == "unsupported_reasoning_effort"
    mock_client.responses.create.assert_awaited_once()


@pytest.mark.parametrize("prefix", [[], [delta("Already emitted")]])
async def test_stream_failures_never_retry(settings_factory, mock_client, chat_request, prefix):
    stream = FakeStream(*prefix, status_error(503))
    mock_client.responses.create.return_value = stream
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert events[-1].code == "provider_error"
    mock_client.responses.create.assert_awaited_once()
    stream.close.assert_awaited_once()


async def test_open_timeout_is_whole_operation_timeout(settings_factory, mock_client, chat_request):
    async def blocked_create(**kwargs):
        await asyncio.Event().wait()

    mock_client.responses.create.side_effect = blocked_create
    events = [
        event
        async for event in ChatService(
            mock_client, settings_factory(request_timeout_seconds=0.02)
        ).events(chat_request)
    ]
    assert events[-1].code == "timeout"
    mock_client.responses.create.assert_awaited_once()


async def test_retry_backoff_obeys_whole_deadline(settings_factory, mock_client, chat_request):
    mock_client.responses.create.side_effect = status_error(503)
    service = ChatService(
        mock_client, settings_factory(request_timeout_seconds=0.02, retry_delay_seconds=1)
    )
    events = [event async for event in service.events(chat_request)]
    assert events[-1].code == "timeout"
    mock_client.responses.create.assert_awaited_once()


async def test_whole_deadline_is_not_reset_by_deltas(settings_factory, mock_client, chat_request):
    stream = FakeStream(delta("First"), delta("Too late"), terminal())
    mock_client.responses.create.return_value = stream
    events = ChatService(mock_client, settings_factory(request_timeout_seconds=0.02)).events(
        chat_request
    )
    await anext(events)
    assert (await anext(events)).text == "First"
    await asyncio.sleep(0.03)
    error = await anext(events)
    assert error.event == "error" and error.code == "timeout"
    with pytest.raises(StopAsyncIteration):
        await anext(events)
    assert stream.reads == 1
    stream.close.assert_awaited_once()


class BlockingStream(FakeStream):
    def __init__(self):
        super().__init__()
        self.waiting = asyncio.Event()

    async def __anext__(self):
        self.waiting.set()
        await asyncio.Event().wait()


async def test_stream_timeout_closes_stream(settings_factory, mock_client, chat_request):
    stream = BlockingStream()
    mock_client.responses.create.return_value = stream
    events = [
        event
        async for event in ChatService(
            mock_client, settings_factory(request_timeout_seconds=0.02)
        ).events(chat_request)
    ]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == "timeout"
    stream.close.assert_awaited_once()


async def test_task_cancellation_closes_without_terminal_event(
    settings_factory, mock_client, chat_request
):
    stream = BlockingStream()
    mock_client.responses.create.return_value = stream
    events = ChatService(mock_client, settings_factory()).events(chat_request)
    assert (await anext(events)).event == "start"
    task = asyncio.create_task(anext(events))
    await stream.waiting.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    stream.close.assert_awaited_once()
    with pytest.raises(StopAsyncIteration):
        await anext(events)


async def test_generator_close_after_delta_releases_stream(
    settings_factory, mock_client, chat_request
):
    stream = FakeStream(delta(), terminal())
    mock_client.responses.create.return_value = stream
    events = ChatService(mock_client, settings_factory()).events(chat_request)
    await anext(events)
    await anext(events)
    await events.aclose()
    assert stream.reads == 1
    stream.close.assert_awaited_once()


async def test_close_failure_cannot_replace_success(settings_factory, mock_client, chat_request):
    stream = FakeStream(delta(), terminal())
    stream.close.side_effect = RuntimeError("secret close failure")
    mock_client.responses.create.return_value = stream
    events = [
        event async for event in ChatService(mock_client, settings_factory()).events(chat_request)
    ]
    assert [event.event for event in events] == ["start", "delta", "done"]
