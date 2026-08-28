from __future__ import annotations

from decimal import Decimal
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from secrl_platform.api.scope import ScopeMode


class ApiModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BudgetSpec(ApiModel):
    max_cases: int | None = Field(default=None, ge=1)
    max_tokens: int | None = Field(default=None, ge=0)
    max_cost: Decimal | None = Field(default=None, ge=0)


class TaskCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=256)
    benchmark_id: str
    agent_revision_id: str
    model_config_revision_id: str | None = None
    evaluator_model_config_revision_id: str | None = None
    scope_mode: ScopeMode | None = None
    case_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=589)
    incident_ids: tuple[str, ...] = Field(default_factory=tuple, max_length=8)
    all_cases: bool = False
    budget: BudgetSpec = Field(default_factory=BudgetSpec)
    max_steps: int = Field(default=32, ge=1, le=10_000)
    max_str_len: int = Field(default=100_000, ge=1, le=10_000_000)
    max_entry_return: int = Field(default=15, ge=1, le=1_000_000)
    agent_parameters: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_selection(self) -> "TaskCreateRequest":
        if len(self.case_ids) != len(set(self.case_ids)):
            raise ValueError("case_ids must not contain duplicates")
        if len(self.incident_ids) != len(set(self.incident_ids)):
            raise ValueError("incident_ids must not contain duplicates")
        if not self.all_cases and not self.case_ids and not self.incident_ids:
            if self.scope_mode != "ALL_BENCHMARK":
                raise ValueError("select at least one Case, Incident, or the full Benchmark")
        return self


class TaskCreateResponse(ApiModel):
    id: str
    run_id: str
    status: str
    task_spec_sha256: str


class ModelParameters(ApiModel):
    temperature: float | None = None
    top_p: float | None = None
    max_output_tokens: int | None = Field(default=None, ge=0)
    max_tokens: int | None = Field(default=None, ge=0)
    timeout_seconds: float | None = Field(default=None, ge=1, le=600)
    seed: int | None = Field(default=None, ge=0)
    stop: str | tuple[str, ...] | None = None
    frequency_penalty: float | None = None
    presence_penalty: float | None = None


class ModelPricing(ApiModel):
    input_per_million: Decimal | None = Field(default=None, ge=0)
    output_per_million: Decimal | None = Field(default=None, ge=0)


class ModelCreateRequest(ApiModel):
    name: str = Field(min_length=1, max_length=128)
    provider: str = Field(min_length=1, max_length=64)
    endpoint: str = Field(min_length=1, max_length=2048)
    model: str = Field(min_length=1, max_length=256)
    parameters: ModelParameters = Field(default_factory=ModelParameters)
    pricing: ModelPricing = Field(default_factory=ModelPricing)


class AgentCreateRequest(ApiModel):
    kind: Literal["BUILT_IN", "SERVICE"] = "BUILT_IN"
    revision_id: str
    endpoint: str | None = Field(default=None, max_length=2048)
    manifest_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_kind_fields(self) -> "AgentCreateRequest":
        if self.kind == "BUILT_IN":
            if self.endpoint is not None or self.manifest_sha256 is not None:
                raise ValueError("built-in agent registration has service fields")
        elif self.endpoint is None or self.manifest_sha256 is None:
            raise ValueError("service agent registration requires endpoint and manifest hash")
        return self


class ReviewCreateRequest(ApiModel):
    primary: str = Field(min_length=1, max_length=128)
    secondary: tuple[str, ...] = Field(default_factory=tuple, max_length=32)
    confidence: Literal["low", "medium", "high"]
    evidence: tuple[str, ...] = Field(default_factory=tuple, max_length=128)
    notes: str = Field(default="", max_length=4096)
