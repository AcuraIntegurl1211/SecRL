from functools import cached_property
from pathlib import Path

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


DEFAULT_AGENT_SERVICE_ALLOWLIST = ("agent-service-reference",)
DEFAULT_MODEL_PROVIDER_ALLOWLIST = ("api.openai.com",)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SECRL_", env_file=".env")

    data_dir: Path = Path("/data")
    master_key: str = Field(min_length=64, max_length=64)
    session_secret: str = Field(min_length=32)
    host: str = "0.0.0.0"
    port: int = 8080
    runner_poll_seconds: float = 1.0
    agent_service_allowlist: tuple[str, ...] = DEFAULT_AGENT_SERVICE_ALLOWLIST
    agent_service_capability_secret: SecretStr | None = None
    model_provider_allowlist: tuple[str, ...] = DEFAULT_MODEL_PROVIDER_ALLOWLIST
    secrl_runtime_enabled: bool = False
    secrl_mysql_user: str = "benchmark_ro"
    secrl_mysql_password: SecretStr | None = None
    secrl_mysql_database: str = "env_monitor_db"

    @field_validator("master_key")
    @classmethod
    def validate_hex_key(cls, value: str) -> str:
        if any(character not in "0123456789abcdefABCDEF" for character in value):
            raise ValueError("master_key must contain exactly 64 hexadecimal characters")
        bytes.fromhex(value)
        return value

    @field_validator("agent_service_capability_secret", mode="before")
    @classmethod
    def validate_agent_service_capability_secret(cls, value):
        if value is None or value == "":
            return None
        encoded = value.get_secret_value() if isinstance(value, SecretStr) else value
        try:
            secret = bytes.fromhex(encoded)
        except (TypeError, ValueError) as exc:
            raise ValueError(
                "agent_service_capability_secret must be hexadecimal"
            ) from exc
        if len(secret) < 32:
            raise ValueError(
                "agent_service_capability_secret must contain at least 32 bytes"
            )
        return value

    @cached_property
    def database_path(self) -> Path:
        return self.data_dir / "secrl-lite.sqlite3"

    @cached_property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"
