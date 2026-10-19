from collections.abc import Callable
from contextlib import AsyncExitStack, asynccontextmanager

import anyio
from azure.core.credentials_async import AsyncTokenCredential
from fastapi import FastAPI
from openai import AsyncOpenAI
from starlette.responses import JSONResponse

from app.agent import AgentService
from app.agent_files import AgentFault
from app.agent_routes import router as agent_router
from app.auth import create_bearer_provider, create_credential
from app.responses import ChatService
from app.routes import router
from app.settings import Settings


def create_app(
    settings: Settings | None = None,
    *,
    credential_factory: Callable[[Settings], AsyncTokenCredential] = create_credential,
    client_factory: Callable[..., AsyncOpenAI] = AsyncOpenAI,
    agent_service_factory: Callable[[Settings], AgentService] | None = None,
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
            agent_service = (
                agent_service_factory(configuration)
                if agent_service_factory is not None
                else AgentService(models=configuration.available_models, eager=False)
            )
            application.state.agent_service = agent_service
            stack.push_async_callback(agent_service.close)
            await agent_service.start()
            yield
        finally:
            with anyio.CancelScope(shield=True):
                await stack.aclose()

    application = FastAPI(title="Assistant Responses API", lifespan=lifespan)
    application.include_router(router)
    application.include_router(agent_router)

    @application.exception_handler(AgentFault)
    async def agent_error(request, exc: AgentFault):
        return JSONResponse(
            status_code=exc.status, content={"error": {"code": exc.code, "message": exc.message}}
        )

    return application


app = create_app()
