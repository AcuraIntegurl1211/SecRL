from __future__ import annotations

import hashlib
import json
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from secrl_platform.benchmarks.protocol import AgentAction, Observation, ToolDefinition


class AgentProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AgentManifest(AgentProtocolModel):
    agent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runtime: Literal["built_in", "service"]
    protocol_version: Literal["1"] = "1"
    parameter_schema: dict[str, Any] = Field(default_factory=dict)

    def sha256(self) -> str:
        payload = json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()


class AgentRevisionRef(AgentProtocolModel):
    id: str = Field(min_length=1)
    manifest: AgentManifest
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_manifest_hash(self) -> "AgentRevisionRef":
        if self.manifest_sha256 != self.manifest.sha256():
            raise ValueError("agent manifest SHA-256 does not match revision")
        return self


class EpisodeContext(AgentProtocolModel):
    run_id: str = Field(min_length=1)
    case_id: str = Field(min_length=1)
    attempt_id: str = Field(min_length=1)
    public_input: dict[str, Any]
    tools: tuple[ToolDefinition, ...]
    max_steps: int = Field(ge=1)


class UsageSnapshot(AgentProtocolModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentRuntime(Protocol):
    @property
    def name(self) -> str: ...

    async def reset(self, episode: EpisodeContext) -> None: ...

    async def act(self, observation: Observation) -> AgentAction: ...

    def usage(self) -> UsageSnapshot: ...

    async def close(self) -> None: ...
