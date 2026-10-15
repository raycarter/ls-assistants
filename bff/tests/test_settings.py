from itertools import product
from pathlib import Path

import pytest
from pydantic import SecretStr, ValidationError

from app.settings import Settings


def test_models_are_trimmed_deduplicated_and_ordered(settings_factory):
    settings = settings_factory(available_models=" deployment-b, ,deployment-a, deployment-b, ")
    assert settings.available_models == ("deployment-b", "deployment-a")
    assert settings.default_model == "deployment-b"


def test_configuration_defaults():
    settings = Settings(
        _env_file=None,
        azure_openai_endpoint="https://example.openai.azure.com",
        available_models="deployment-a",
    )
    assert settings.default_model == "deployment-a"
    assert settings.request_timeout_seconds == 120
    assert settings.provider_timeout_seconds == 60
    assert settings.max_output_tokens == 4096
    assert settings.max_retries == 2
    assert settings.retry_delay_seconds == 0.5


@pytest.mark.parametrize("models", ["", " , , ", [], [1], None])
def test_nonempty_model_list_required(settings_factory, models):
    with pytest.raises(ValidationError, match="AVAILABLE_MODELS|available_models"):
        settings_factory(available_models=models)


def test_required_settings():
    with pytest.raises(ValidationError) as failure:
        Settings(_env_file=None)
    assert {error["loc"][0] for error in failure.value.errors()} == {
        "azure_openai_endpoint",
        "available_models",
    }


def test_explicit_default_must_be_allowlisted(settings_factory):
    assert settings_factory(default_model=" deployment-b ").default_model == "deployment-b"
    assert settings_factory(default_model=" ").default_model == "deployment-a"
    with pytest.raises(ValidationError, match="DEFAULT_MODEL"):
        settings_factory(default_model="other")


@pytest.mark.parametrize("path", ["", "/", "/openai/v1", "/openai/v1/"])
def test_endpoint_normalization(settings_factory, path):
    settings = settings_factory(azure_openai_endpoint=f"https://example.openai.azure.com{path}")
    assert settings.openai_base_url == "https://example.openai.azure.com/openai/v1/"


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.openai.azure.com",
        "https:///missing-host",
        "https://example.openai.azure.com/responses",
        "https://example.openai.azure.com/?secret=hidden",
        "https://example.openai.azure.com/#fragment",
        "https://secret@example.openai.azure.com",
        "https://example.openai.azure.com:invalid",
    ],
)
def test_invalid_endpoints(settings_factory, endpoint):
    with pytest.raises(ValidationError):
        settings_factory(azure_openai_endpoint=endpoint)


@pytest.mark.parametrize(
    "present",
    [combination for combination in product([False, True], repeat=3) if 0 < sum(combination) < 3],
)
def test_every_partial_spn_configuration_is_rejected(settings_factory, present):
    names = ("azure_tenant_id", "azure_client_id", "azure_client_secret")
    with pytest.raises(ValidationError, match="Set all"):
        settings_factory(
            **{name: "example" for name, enabled in zip(names, present, strict=True) if enabled}
        )


def test_empty_credentials_are_absent_and_secrets_are_hidden(settings_factory):
    empty = settings_factory(azure_tenant_id=" ", azure_client_id="", azure_client_secret=" ")
    assert empty.azure_tenant_id is empty.azure_client_id is empty.azure_client_secret is None
    settings = settings_factory(
        azure_tenant_id=" tenant ", azure_client_id=" client ", azure_client_secret="fake-secret"
    )
    assert settings.azure_tenant_id == "tenant"
    assert settings.azure_client_id == "client"
    assert isinstance(settings.azure_client_secret, SecretStr)
    assert "fake-secret" not in repr(settings)
    assert "fake-secret" not in settings.model_dump_json()


@pytest.mark.parametrize(
    "override",
    [
        {"request_timeout_seconds": 0},
        {"provider_timeout_seconds": -1},
        {"request_timeout_seconds": float("inf")},
        {"provider_timeout_seconds": float("nan")},
        {"max_output_tokens": 0},
        {"max_retries": -1},
        {"max_retries": 6},
        {"retry_delay_seconds": -1},
    ],
)
def test_invalid_limits(settings_factory, override):
    with pytest.raises(ValidationError):
        settings_factory(**override)


def test_environment_overrides_dotenv(monkeypatch):
    fixture = Path(__file__).parent / "fixtures" / "settings.env"
    dotenv = Settings(_env_file=fixture)
    assert dotenv.available_models == ("dotenv-first", "dotenv-second")
    assert dotenv.max_output_tokens == 2048
    monkeypatch.setenv("AZURE_OPENAI_ENDPOINT", "https://env-example.openai.azure.com")
    monkeypatch.setenv("AVAILABLE_MODELS", " env-first,env-second,env-first ")
    monkeypatch.setenv("DEFAULT_MODEL", "env-second")
    monkeypatch.setenv("MAX_OUTPUT_TOKENS", "1024")
    settings = Settings(_env_file=fixture)
    assert settings.azure_openai_endpoint == "https://env-example.openai.azure.com"
    assert settings.available_models == ("env-first", "env-second")
    assert settings.default_model == "env-second"
    assert settings.max_output_tokens == 1024


def test_configuration_errors_do_not_print_secret(settings_factory):
    with pytest.raises(ValidationError) as failure:
        settings_factory(azure_client_secret="must-not-be-printed")
    assert "must-not-be-printed" not in str(failure.value)
