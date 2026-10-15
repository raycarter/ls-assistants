import asyncio
import socket
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.identity.aio import AzureCliCredential, ClientSecretCredential

from app.main import create_app
from app.schemas import ChatRequest
from app.settings import Settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch):
    for name in Settings.model_fields:
        monkeypatch.delenv(name.upper(), raising=False)
        monkeypatch.delenv(name, raising=False)

    def forbidden_network(*args, **kwargs):
        raise AssertionError("Tests must use mocked transports and credentials")

    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden_network)
    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden_network)
    monkeypatch.setattr(subprocess, "Popen", forbidden_network)

    async def forbidden_token(*args, **kwargs):
        raise AssertionError("Tests must mock credential acquisition")

    for credential_type in (AzureCliCredential, ClientSecretCredential):
        monkeypatch.setattr(credential_type, "get_token", forbidden_token)
        monkeypatch.setattr(credential_type, "get_token_info", forbidden_token)


@pytest.fixture
def settings_factory():
    def factory(**overrides):
        return Settings(
            _env_file=None,
            **{
                "azure_openai_endpoint": "https://example.openai.azure.com",
                "available_models": "deployment-a,deployment-b",
                "retry_delay_seconds": 0,
                **overrides,
            },
        )

    return factory


@pytest.fixture
def chat_request():
    return ChatRequest(
        session_id="session-1",
        request_id="request-1",
        model="deployment-b",
        reasoning_effort="xhigh",
        system_prompt="Be concise.",
        messages=[
            {"role": "user", "content": "Earlier question"},
            {"role": "assistant", "content": "Earlier answer"},
            {"role": "user", "content": "New question"},
        ],
    )


@pytest.fixture
def mock_client():
    return SimpleNamespace(
        responses=SimpleNamespace(create=AsyncMock()),
        close=AsyncMock(),
    )


@pytest.fixture
def app_factory(settings_factory, mock_client):
    def factory(**settings_overrides):
        credential = SimpleNamespace(get_token=AsyncMock(), close=AsyncMock())
        client_factory = Mock(return_value=mock_client)
        application = create_app(
            settings_factory(**settings_overrides),
            credential_factory=Mock(return_value=credential),
            client_factory=client_factory,
        )
        return application, credential, client_factory

    return factory
