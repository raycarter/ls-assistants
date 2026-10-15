import asyncio
import gc
import json
import shutil
import time
import weakref
from unittest.mock import AsyncMock, Mock

import anyio
import httpx
import pytest
from azure.identity.aio import AzureCliCredential
from openai import AsyncOpenAI

from app.auth import ManagedCliCredential, create_bearer_provider
from app.main import create_app
from tests.test_api import decode_frames, request_scope

# Capture the public entry point before the autouse credential guard replaces it.
SDK_GET_TOKEN = AzureCliCredential.get_token


class MockCliProcess:
    def __init__(self, harness):
        self.harness = harness
        self.returncode = None
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.reaped = asyncio.Event()
        self.kills = 0
        self.communication_cancelled = False
        self.wait = AsyncMock(side_effect=self._wait)

    def _reap(self, returncode):
        self.returncode = returncode
        self.harness.active -= 1
        self.reaped.set()

    async def communicate(self):
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            self.communication_cancelled = True
            raise
        self._reap(0)
        token = {"accessToken": "fake-cli-token", "expires_on": int(time.time()) + 3600}
        return json.dumps(token).encode(), b""

    def kill(self):
        self.kills += 1
        # Model asyncio's transport exit callback separately from the SDK's kill call.
        asyncio.get_running_loop().call_later(0.001, self._reap, -9)

    async def _wait(self):
        await self.reaped.wait()
        return self.returncode


class MockCliSubprocesses:
    def __init__(self):
        self.processes = []
        self.spawned = asyncio.Queue()
        self.sdk_tasks = []
        self.active = 0
        self.spawn = AsyncMock(side_effect=self._spawn)

    async def _spawn(self, *args, **kwargs):
        assert args[0] == "/mock/az"
        # SDK calls serialize even though the previous killed child's exit
        # notification may be delivered after its token coroutine has returned.
        assert all(reference() is None or reference().done() for reference in self.sdk_tasks)
        self.sdk_tasks.append(weakref.ref(asyncio.current_task()))
        process = MockCliProcess(self)
        self.processes.append(process)
        self.active += 1
        self.spawned.put_nowait(process)
        return process

    async def next_process(self):
        process = await self.spawned.get()
        await process.started.wait()
        return process


@pytest.fixture
def cli_subprocesses(monkeypatch):
    harness = MockCliSubprocesses()
    monkeypatch.setattr(shutil, "which", lambda *args, **kwargs: "/mock/az")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", harness.spawn)
    monkeypatch.setattr(AzureCliCredential, "get_token", SDK_GET_TOKEN)
    return harness


@pytest.fixture
async def loop_errors():
    loop = asyncio.get_running_loop()
    previous = loop.get_exception_handler()
    errors = []
    loop.set_exception_handler(lambda loop, context: errors.append(context))
    yield errors
    loop.set_exception_handler(previous)


def cli_app(settings_factory, *, process_timeout=0.08, request_timeout=0.02):
    credential = ManagedCliCredential(AzureCliCredential(process_timeout=process_timeout))
    transport = httpx.MockTransport(Mock(side_effect=AssertionError("Unexpected HTTP request")))

    def client_factory(**kwargs):
        return AsyncOpenAI(**kwargs, http_client=httpx.AsyncClient(transport=transport))

    app = create_app(
        settings_factory(request_timeout_seconds=request_timeout),
        credential_factory=Mock(return_value=credential),
        client_factory=client_factory,
    )
    return app, credential


async def assert_drained(harness, baseline, errors):
    for process in harness.processes:
        await process.reaped.wait()
    await asyncio.sleep(0)
    gc.collect()
    await asyncio.sleep(0)
    assert harness.active == 0
    assert not asyncio.all_tasks() - baseline
    # SDK warning records may retain completed tasks through exception tracebacks.
    assert all(
        reference() is None or (reference().done() and not reference().cancelled())
        for reference in harness.sdk_tasks
    )
    assert errors == []


@pytest.mark.parametrize("outcome", ["sdk_timeout", "success"])
async def test_real_sdk_callback_survives_external_cancellation_and_lifespan_drains(
    settings_factory, cli_subprocesses, loop_errors, outcome
):
    baseline = asyncio.all_tasks()
    app, _ = cli_app(settings_factory)
    async with app.router.lifespan_context(app), asyncio.timeout(1):
        client = app.state.chat_service.client
        request = asyncio.create_task(
            client.responses.create(model="deployment-a", input="Question", stream=True)
        )
        process = await cli_subprocesses.next_process()
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        assert not process.communication_cancelled
        assert process.kills == 0
        sdk_task = cli_subprocesses.sdk_tasks[0]()
        assert sdk_task is not None and not sdk_task.done()
        del sdk_task, request
        if outcome == "success":
            process.release.set()
    assert process.kills == (1 if outcome == "sdk_timeout" else 0)
    assert process.communication_cancelled == (outcome == "sdk_timeout")
    process.wait.assert_not_awaited()
    await assert_drained(cli_subprocesses, baseline, loop_errors)


async def test_real_sdk_callback_survives_request_deadline_and_lifespan_drains(
    settings_factory, chat_request, cli_subprocesses, loop_errors
):
    baseline = asyncio.all_tasks()
    app, _ = cli_app(settings_factory)
    async with app.router.lifespan_context(app), asyncio.timeout(1):
        events = [event async for event in app.state.chat_service.events(chat_request)]
        assert [event.event for event in events] == ["start", "error"]
        assert events[-1].code == "timeout"
        process = await cli_subprocesses.next_process()
        assert process.kills == 0
        assert not process.communication_cancelled
    assert process.kills == 1
    assert process.communication_cancelled
    await assert_drained(cli_subprocesses, baseline, loop_errors)


async def test_aborted_callback_exception_is_retrieved_before_lifespan_close(
    settings_factory, cli_subprocesses, loop_errors, monkeypatch
):
    baseline = asyncio.all_tasks()
    app, _ = cli_app(settings_factory, process_timeout=0.04)
    async with app.router.lifespan_context(app), asyncio.timeout(1):
        request = asyncio.create_task(
            app.state.chat_service.client.responses.create(
                model="deployment-a", input="Question", stream=True
            )
        )
        process = await cli_subprocesses.next_process()
        sdk_task = cli_subprocesses.sdk_tasks[0]()
        observed_exception = Mock(wraps=sdk_task.exception)
        monkeypatch.setattr(sdk_task, "exception", observed_exception)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request
        del request
        # No foreground observer remains, and shutdown has not started draining.
        await process.reaped.wait()
        await asyncio.sleep(0)
        # Observe public exception retrieval without awaiting the failed token result.
        assert sdk_task.done() and not sdk_task.cancelled()
        assert observed_exception.called
        assert loop_errors == []
    assert process.kills == 1
    await assert_drained(cli_subprocesses, baseline, loop_errors)


@pytest.mark.parametrize("spec_version", ["2.0", "2.4"])
async def test_real_sdk_callback_survives_disconnect_and_lifespan_drains(
    settings_factory, chat_request, cli_subprocesses, loop_errors, spec_version
):
    baseline = asyncio.all_tasks()
    app, _ = cli_app(settings_factory, request_timeout=1)
    body_received = False
    sent = []

    async def receive():
        nonlocal body_received
        if not body_received:
            body_received = True
            return {
                "type": "http.request",
                "body": chat_request.model_dump_json().encode(),
                "more_body": False,
            }
        await cli_subprocesses.next_process()
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message)

    async with app.router.lifespan_context(app), asyncio.timeout(1):
        await app(request_scope(spec_version), receive, send)
        process = cli_subprocesses.processes[0]
        assert process.kills == 0
        assert not process.communication_cancelled
    assert process.kills == 1
    assert process.communication_cancelled
    frames = decode_frames(b"".join(message.get("body", b"") for message in sent).decode())
    assert [frame["event"] for frame in frames] == ["start"]
    await assert_drained(cli_subprocesses, baseline, loop_errors)


async def test_cancelled_waiters_never_spawn_overlapping_cli_acquisitions(
    cli_subprocesses, loop_errors
):
    baseline = asyncio.all_tasks()
    credential = ManagedCliCredential(AzureCliCredential(process_timeout=0.08))
    provider = create_bearer_provider(credential)
    first = asyncio.create_task(provider())
    process = await cli_subprocesses.next_process()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    del first
    for _ in range(20):
        waiters = [asyncio.create_task(provider()) for _ in range(4)]
        await asyncio.sleep(0)
        for task in waiters:
            task.cancel()
        results = await asyncio.gather(*waiters, return_exceptions=True)
        assert all(isinstance(result, asyncio.CancelledError) for result in results)
    await credential.close()
    assert cli_subprocesses.spawn.await_count == 1
    assert process.kills == 1
    await assert_drained(cli_subprocesses, baseline, loop_errors)


async def test_refresh_after_cancel_waits_for_sdk_cleanup_and_then_caches_new_token(
    cli_subprocesses, loop_errors
):
    baseline = asyncio.all_tasks()
    credential = ManagedCliCredential(AzureCliCredential(process_timeout=0.04))
    provider = create_bearer_provider(credential)
    first = asyncio.create_task(provider())
    first_process = await cli_subprocesses.next_process()
    first.cancel()
    with pytest.raises(asyncio.CancelledError):
        await first
    del first
    second = asyncio.create_task(provider())
    second_process = await cli_subprocesses.next_process()
    assert first_process.kills == 1
    second_process.release.set()
    assert await second == "fake-cli-token"
    assert await asyncio.gather(*(provider() for _ in range(10))) == ["fake-cli-token"] * 10
    assert cli_subprocesses.spawn.await_count == 2
    await credential.close()
    with pytest.raises(RuntimeError, match="closed"):
        await credential.get_token("https://ai.azure.com/.default")
    await assert_drained(cli_subprocesses, baseline, loop_errors)


async def test_close_drains_sdk_acquisition_inside_cancelled_scope(cli_subprocesses, loop_errors):
    baseline = asyncio.all_tasks()
    credential = ManagedCliCredential(AzureCliCredential(process_timeout=0.04))
    caller = asyncio.create_task(create_bearer_provider(credential)())
    process = await cli_subprocesses.next_process()
    caller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await caller
    del caller
    with anyio.CancelScope() as cancellation:
        cancellation.cancel()
        async with credential:
            pass
    assert process.kills == 1
    assert process.communication_cancelled
    await assert_drained(cli_subprocesses, baseline, loop_errors)
