"""Settings from the environment / .env, and groups and presets from config.yaml."""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, SecretStr, model_validator
from pydantic import ValidationError as PydanticValidationError
from pydantic_settings import BaseSettings, SettingsConfigDict

from .errors import ConfigError


class Settings(BaseSettings):
    """Read from GOVEE_* environment variables, falling back to .env."""

    model_config = SettingsConfigDict(env_file=".env", env_prefix="GOVEE_", extra="ignore")

    # SecretStr keeps the key out of reprs, logs and tracebacks.
    api_key: SecretStr | None = None
    base_url: str = "https://openapi.api.govee.com"
    config_file: Path = Path("config.yaml")
    data_dir: Path = Path("data")
    web_host: str = "127.0.0.1"
    web_port: int = 8000

    def require_api_key(self) -> str:
        key = self.api_key.get_secret_value().strip() if self.api_key else ""
        if not key:
            raise ConfigError(
                "GOVEE_API_KEY is not set. Copy .env.example to .env and paste your Govee API key into it."
            )
        return key


class Preset(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rgb: tuple[int, int, int] | None = None
    kelvin: int | None = None
    brightness: int | None = None

    @model_validator(mode="after")
    def _one_colour(self) -> Preset:
        if self.rgb is not None and self.kelvin is not None:
            raise ValueError("use rgb or kelvin, not both")
        if self.rgb is None and self.kelvin is None and self.brightness is None:
            raise ValueError("needs at least one of rgb, kelvin or brightness")
        return self


class AppConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    poll_seconds: int = 60
    groups: dict[str, str | list[str]] = {"all": "*"}
    presets: dict[str, Preset] = {}


def load_config(path: Path) -> AppConfig:
    if not path.exists():
        return AppConfig()
    try:
        raw = yaml.safe_load(path.read_text()) or {}
        return AppConfig.model_validate(raw)
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path} is not valid YAML: {exc}") from None
    except PydanticValidationError as exc:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()
        )
        raise ConfigError(f"{path} has a problem: {problems}") from None
