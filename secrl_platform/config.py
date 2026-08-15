from functools import cached_property
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="SECRL_", env_file=".env")

    data_dir: Path = Path("/data")
    master_key: str = Field(min_length=64, max_length=64)
    session_secret: str = Field(min_length=32)
    host: str = "0.0.0.0"
    port: int = 8080
    runner_poll_seconds: float = 1.0
    agent_service_allowlist: tuple[str, ...] = ("agent-service-reference",)

    @field_validator("master_key")
    @classmethod
    def validate_hex_key(cls, value: str) -> str:
        if any(character not in "0123456789abcdefABCDEF" for character in value):
            raise ValueError("master_key must contain exactly 64 hexadecimal characters")
        bytes.fromhex(value)
        return value

    @cached_property
    def database_path(self) -> Path:
        return self.data_dir / "secrl-lite.sqlite3"

    @cached_property
    def artifact_dir(self) -> Path:
        return self.data_dir / "artifacts"
