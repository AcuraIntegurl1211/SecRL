from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from secrl_platform.benchmarks.protocol import AgentAction, Observation, ToolDefinition


class AgentProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FrozenDict(dict[str, Any]):
    def _immutable(self, *_args, **_kwargs):
        raise TypeError("frozen JSON object cannot be modified")

    __setitem__ = _immutable
    __delitem__ = _immutable
    clear = _immutable
    pop = _immutable
    popitem = _immutable
    setdefault = _immutable
    update = _immutable
    __ior__ = _immutable

    def __deepcopy__(self, _memo):
        return self


def freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        frozen = {key: freeze_json(item) for key, item in value.items()}
        result = FrozenDict()
        dict.update(result, frozen)
        return result
    if isinstance(value, (list, tuple)):
        return tuple(freeze_json(item) for item in value)
    return value


class AgentToolDefinition(ToolDefinition):
    @field_validator("parameters", mode="after")
    @classmethod
    def freeze_parameters(cls, value: dict[str, Any]) -> dict[str, Any]:
        return freeze_json(value)


class AgentManifest(AgentProtocolModel):
    agent_id: str = Field(min_length=1)
    name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    runtime: Literal["built_in", "service"]
    protocol_version: Literal["1"] = "1"
    parameter_schema: dict[str, Any] = Field(default_factory=dict)

    @field_validator("parameter_schema", mode="after")
    @classmethod
    def freeze_parameter_schema(cls, value: dict[str, Any]) -> dict[str, Any]:
        return freeze_json(value)

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
    tools: tuple[AgentToolDefinition, ...]
    max_steps: int = Field(ge=1)

    @field_validator("public_input", mode="after")
    @classmethod
    def freeze_public_input(cls, value: dict[str, Any]) -> dict[str, Any]:
        return freeze_json(value)

    @field_validator("tools", mode="before")
    @classmethod
    def normalize_tools(cls, value: Any) -> Any:
        return tuple(
            item.model_dump(mode="python") if isinstance(item, ToolDefinition) else item
            for item in value
        )


class UsageSnapshot(AgentProtocolModel):
    prompt_tokens: int = Field(default=0, ge=0)
    completion_tokens: int = Field(default=0, ge=0)
    cached_tokens: int = Field(default=0, ge=0)
    reasoning_tokens: int = Field(default=0, ge=0)
    estimated_cost: Decimal = Field(default=Decimal(0), ge=0)

    @property
    def total(self) -> int:
        return self.prompt_tokens + self.completion_tokens


class AgentRuntime(Protocol):
    @property
    def model_access(self) -> Literal["none", "platform_gateway"]: ...

    @property
    def model_gateway_binding(self) -> str | None: ...

    @property
    def name(self) -> str: ...

    async def reset(self, episode: EpisodeContext) -> None: ...

    async def act(self, observation: Observation) -> AgentAction: ...

    def usage(self) -> UsageSnapshot: ...

    async def close(self) -> None: ...
