from pathlib import Path
from typing import Annotated, Self
from urllib.parse import urlsplit

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

BFF_ROOT = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BFF_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        hide_input_in_errors=True,
    )

    azure_openai_endpoint: str
    available_models: Annotated[tuple[str, ...], NoDecode]
    default_model: str | None = None
    azure_tenant_id: str | None = None
    azure_client_id: str | None = None
    azure_client_secret: SecretStr | None = None
    tavily_api_key: SecretStr | None = None
    request_timeout_seconds: float = Field(default=120, gt=0, le=3600, allow_inf_nan=False)
    provider_timeout_seconds: float = Field(default=60, gt=0, le=3600, allow_inf_nan=False)
    title_timeout_seconds: float = Field(default=10, gt=0, le=3600, allow_inf_nan=False)
    max_output_tokens: int = Field(default=4096, ge=1)
    max_retries: int = Field(default=2, ge=0, le=5)
    retry_delay_seconds: float = Field(default=0.5, ge=0, le=10, allow_inf_nan=False)

    @field_validator("azure_openai_endpoint")
    @classmethod
    def validate_endpoint(cls, value: str) -> str:
        value = value.strip()
        url = urlsplit(value)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.query
            or url.fragment
            or url.path.rstrip("/") not in ("", "/openai/v1")
        ):
            raise ValueError("AZURE_OPENAI_ENDPOINT must be an HTTPS resource or /openai/v1 URL")
        # Validate malformed ports without retaining credentials or arbitrary paths.
        _ = url.port
        return f"https://{url.netloc}"

    @field_validator("available_models", mode="before")
    @classmethod
    def parse_models(cls, value: object) -> tuple[str, ...]:
        if isinstance(value, str):
            value = value.split(",")
        if not isinstance(value, (list, tuple)) or any(
            not isinstance(model, str) for model in value
        ):
            raise ValueError("AVAILABLE_MODELS must be a comma-separated list of deployment names")
        models = tuple(dict.fromkeys(model.strip() for model in value if model.strip()))
        if not models:
            raise ValueError("AVAILABLE_MODELS must contain at least one deployment name")
        return models

    @field_validator("default_model", "azure_tenant_id", "azure_client_id", mode="before")
    @classmethod
    def trim_optional(cls, value: object) -> object:
        return (value.strip() or None) if isinstance(value, str) else value

    @field_validator("azure_client_secret", "tavily_api_key", mode="before")
    @classmethod
    def empty_secret(cls, value: object) -> object:
        raw = value.get_secret_value() if isinstance(value, SecretStr) else value
        return None if isinstance(raw, str) and not raw.strip() else value

    @model_validator(mode="after")
    def validate_configuration(self) -> Self:
        credentials = (self.azure_tenant_id, self.azure_client_id, self.azure_client_secret)
        if any(value is not None for value in credentials) and not all(
            value is not None for value in credentials
        ):
            raise ValueError(
                "Set all of AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET, or none"
            )
        if self.default_model is None:
            self.default_model = self.available_models[0]
        elif self.default_model not in self.available_models:
            raise ValueError("DEFAULT_MODEL must be included in AVAILABLE_MODELS")
        return self

    @property
    def openai_base_url(self) -> str:
        return f"{self.azure_openai_endpoint}/openai/v1/"
