from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from azure.core.credentials_async import AsyncTokenCredential
from fastapi import FastAPI
from openai import AsyncOpenAI

from app.auth import create_bearer_provider, create_credential
from app.responses import ChatService
from app.routes import router
from app.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    credential_factory: Callable[[Settings], AsyncTokenCredential] = create_credential,
    client_factory: Callable[..., AsyncOpenAI] = AsyncOpenAI,
) -> FastAPI:
    @asynccontextmanager
    async def lifespan(application: FastAPI):
        configuration = settings if settings is not None else Settings()
        stack = AsyncExitStack()
        try:
            credential = credential_factory(configuration)
            stack.push_async_callback(credential.close)
            bearer_provider = create_bearer_provider(credential)
            client = client_factory(
                base_url=configuration.openai_base_url,
                api_key=bearer_provider,
                timeout=configuration.provider_timeout_seconds,
                max_retries=0,
            )
            stack.push_async_callback(client.close)
            application.state.settings = configuration
            application.state.chat_service = ChatService(client, configuration)
            yield
        finally:
            with anyio.CancelScope(shield=True):
                await stack.aclose()

    application = FastAPI(title="Assistant Responses API", lifespan=lifespan)
    application.include_router(router)
    return application


app = create_app()
