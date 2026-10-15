import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from azure.core.credentials import AccessToken
from azure.core.credentials_async import AsyncTokenCredential
from azure.core.exceptions import ClientAuthenticationError

from app import auth
from app.responses import ChatService


def test_complete_spn_takes_priority(monkeypatch, settings_factory):
    spn = Mock()
    cli = Mock()
    monkeypatch.setattr(auth, "ClientSecretCredential", spn)
    monkeypatch.setattr(auth, "AzureCliCredential", cli)
    settings = settings_factory(
        azure_tenant_id="tenant", azure_client_id="client", azure_client_secret="fake-secret"
    )
    assert auth.create_credential(settings) is spn.return_value
    spn.assert_called_once_with(tenant_id="tenant", client_id="client", client_secret="fake-secret")
    cli.assert_not_called()


def test_absent_spn_uses_cli(monkeypatch, settings_factory):
    spn = Mock()
    cli = Mock()
    monkeypatch.setattr(auth, "ClientSecretCredential", spn)
    monkeypatch.setattr(auth, "AzureCliCredential", cli)
    credential = auth.create_credential(settings_factory())
    assert isinstance(credential, auth.ManagedCliCredential)
    assert isinstance(credential, AsyncTokenCredential)
    cli.assert_called_once_with(process_timeout=10)
    spn.assert_not_called()


def test_spn_constructor_failure_never_falls_back(monkeypatch, settings_factory):
    spn = Mock(side_effect=ValueError("fake constructor failure"))
    cli = Mock()
    monkeypatch.setattr(auth, "ClientSecretCredential", spn)
    monkeypatch.setattr(auth, "AzureCliCredential", cli)
    with pytest.raises(ValueError):
        auth.create_credential(
            settings_factory(
                azure_tenant_id="tenant",
                azure_client_id="client",
                azure_client_secret="fake-secret",
            )
        )
    cli.assert_not_called()


async def test_spn_authentication_failure_never_falls_back(
    monkeypatch, settings_factory, mock_client, chat_request
):
    credential = SimpleNamespace(
        get_token=AsyncMock(side_effect=ClientAuthenticationError("secret must stay hidden"))
    )
    spn = Mock(return_value=credential)
    cli = Mock()
    monkeypatch.setattr(auth, "ClientSecretCredential", spn)
    monkeypatch.setattr(auth, "AzureCliCredential", cli)
    settings = settings_factory(
        azure_tenant_id="tenant", azure_client_id="client", azure_client_secret="fake-secret"
    )
    bearer = auth.create_bearer_provider(auth.create_credential(settings))

    async def create(**kwargs):
        await bearer()

    mock_client.responses.create.side_effect = create
    events = [event async for event in ChatService(mock_client, settings).events(chat_request)]
    assert [event.event for event in events] == ["start", "error"]
    assert events[-1].code == "provider_authentication_error"
    assert "secret must stay hidden" not in events[-1].message
    cli.assert_not_called()
    assert mock_client.responses.create.await_count == 1


async def test_bearer_provider_is_shared_cached_and_refreshing():
    credential = SimpleNamespace(
        get_token=AsyncMock(
            side_effect=[
                AccessToken("short-lived-fake-token", int(time.time()) + 1),
                AccessToken("refreshed-fake-token", int(time.time()) + 3600),
            ]
        )
    )
    provider = auth.create_bearer_provider(credential)
    assert await provider() == "short-lived-fake-token"
    assert await provider() == "refreshed-fake-token"
    assert await provider() == "refreshed-fake-token"
    assert credential.get_token.await_count == 2
    for call in credential.get_token.await_args_list:
        assert call.args == ("https://ai.azure.com/.default",)


async def test_concurrent_token_requests_are_single_flight():
    async def get_token(*args, **kwargs):
        await asyncio.sleep(0)
        return AccessToken("fake-token", int(time.time()) + 3600)

    credential = SimpleNamespace(get_token=AsyncMock(side_effect=get_token))
    provider = auth.create_bearer_provider(credential)
    assert await asyncio.gather(*(provider() for _ in range(10))) == ["fake-token"] * 10
    credential.get_token.assert_awaited_once()
