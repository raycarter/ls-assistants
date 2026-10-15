import asyncio
from collections.abc import Awaitable, Callable
from contextlib import suppress
from types import TracebackType
from typing import Any, Self

import anyio
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.identity.aio import (
    AzureCliCredential,
    ClientSecretCredential,
    get_bearer_token_provider,
)

from app.settings import Settings

TOKEN_SCOPE = "https://ai.azure.com/.default"
CLI_PROCESS_TIMEOUT_SECONDS = 10
BearerProvider = Callable[[], Awaitable[str]]


async def _await_token(task: asyncio.Task[AccessToken]) -> AccessToken:
    # wait() cancels only the waiter, never the SDK task. Unlike shield(), it also
    # avoids reporting a later SDK exception to the loop when the waiter was cancelled.
    await asyncio.wait({task})
    return task.result()


class ManagedCliCredential:
    """Keep CLI acquisition alive through its SDK timeout when a caller cancels."""

    def __init__(self, credential: AsyncTokenCredential) -> None:
        self._credential = credential
        self._lock = asyncio.Lock()
        self._pending: asyncio.Task[AccessToken] | None = None
        self._closed = False

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None = None,
        exc_value: BaseException | None = None,
        traceback: TracebackType | None = None,
    ) -> None:
        await self.close()

    def _settled(self, task: asyncio.Task[AccessToken]) -> None:
        # An aborted caller may never await the result. Always observe its exception.
        if not task.cancelled():
            task.exception()
        if self._pending is task:
            self._pending = None

    async def get_token(self, *scopes: str, **kwargs: Any) -> AccessToken:
        async with self._lock:
            if self._closed:
                raise RuntimeError("Credential is closed")
            # Cancellation releases the caller's lock, but must not start a second CLI
            # acquisition while the preceding SDK call is still finishing.
            if self._pending is not None:
                with suppress(Exception):
                    await _await_token(self._pending)
            if self._closed:
                raise RuntimeError("Credential is closed")
            task = asyncio.create_task(self._credential.get_token(*scopes, **kwargs))
            self._pending = task
            task.add_done_callback(self._settled)
            return await _await_token(task)

    async def close(self) -> None:
        self._closed = True
        with anyio.CancelScope(shield=True):
            if self._pending is not None:
                with suppress(Exception):
                    await _await_token(self._pending)
            await self._credential.close()


def create_credential(settings: Settings) -> AsyncTokenCredential:
    """Select exactly one credential; failures never change the selected mechanism."""
    if settings.azure_client_secret is not None:
        return ClientSecretCredential(
            tenant_id=settings.azure_tenant_id,
            client_id=settings.azure_client_id,
            client_secret=settings.azure_client_secret.get_secret_value(),
        )
    return ManagedCliCredential(AzureCliCredential(process_timeout=CLI_PROCESS_TIMEOUT_SECONDS))


def create_bearer_provider(credential: AsyncTokenCredential) -> BearerProvider:
    # Azure's async policy caches and refreshes tokens. Serialize refreshes across requests.
    provider = get_bearer_token_provider(credential, TOKEN_SCOPE)
    lock = asyncio.Lock()

    async def bearer_token() -> str:
        async with lock:
            return await provider()

    return bearer_token
