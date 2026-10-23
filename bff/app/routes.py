import asyncio
from collections.abc import AsyncIterator

import anyio
from fastapi import APIRouter, HTTPException, Request
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse
from starlette.types import Message, Receive, Scope, Send

from app.responses import ChatService
from app.schemas import ChatRequest, ModelsResponse, encode_sse

router = APIRouter()


class ClosingStreamingResponse(StreamingResponse):
    def __init__(
        self,
        content: AsyncIterator[str],
        *,
        deadline: float | None = None,
        send_timeout_seconds: float = 30,
    ) -> None:
        super().__init__(
            content,
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"},
        )
        self.deadline = deadline
        self.send_timeout_seconds = send_timeout_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        disconnected_send = False
        # Watch receive even on ASGI 2.4, so a disconnect cancels a stalled provider read.
        async with anyio.create_task_group() as group:

            async def stream_and_cancel() -> None:
                nonlocal disconnected_send
                try:
                    await self.stream_response(send)
                except OSError:
                    disconnected_send = True
                finally:
                    group.cancel_scope.cancel()

            group.start_soon(stream_and_cancel)
            await self.listen_for_disconnect(receive)
            group.cancel_scope.cancel()
        if disconnected_send:
            raise ClientDisconnect
        if self.background is not None:
            await self.background()

    async def stream_response(self, send: Send) -> None:
        async def send_before_deadline(message: Message) -> None:
            # Chat retains its provider deadline. Agent gives each ASGI send its
            # own timeout; waiting for another frame consumes no transport lifetime.
            deadline = self.deadline
            if deadline is None:
                deadline = asyncio.get_running_loop().time() + self.send_timeout_seconds
            async with asyncio.timeout_at(deadline):
                await send(message)

        try:
            await super().stream_response(send_before_deadline)
        except TimeoutError:
            # The transport cannot accept this send before its timeout. Do not attempt
            # another error/end frame on it; returning lets the ASGI server end the response.
            return
        finally:
            # Covers both ASGI disconnect cancellation and a failed send (ASGI >= 2.4).
            with anyio.move_on_after(5, shield=True):
                await self.body_iterator.aclose()


@router.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/api/models", response_model=ModelsResponse)
async def models(request: Request) -> ModelsResponse:
    settings = request.app.state.settings
    return ModelsResponse(
        models=list(settings.available_models), default_model=settings.default_model
    )


@router.post(
    "/api/chat",
    response_class=ClosingStreamingResponse,
    responses={200: {"content": {"text/event-stream": {"schema": {"type": "string"}}}}},
)
async def chat(body: ChatRequest, request: Request) -> ClosingStreamingResponse:
    service: ChatService = request.app.state.chat_service
    if body.model not in request.app.state.settings.available_models:
        raise HTTPException(status_code=422, detail="model must be included in AVAILABLE_MODELS")
    deadline = asyncio.get_running_loop().time() + service.settings.request_timeout_seconds

    async def frames() -> AsyncIterator[str]:
        events = service.events(body, deadline=deadline)
        try:
            sequence = 0
            async for event in events:
                yield encode_sse(event, sequence)
                sequence += 1
        finally:
            with anyio.move_on_after(5, shield=True):
                await events.aclose()

    return ClosingStreamingResponse(frames(), deadline=deadline)
