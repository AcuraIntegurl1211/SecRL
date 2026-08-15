from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


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
    case_ids: tuple[str, ...] = Field(min_length=1)
    budget: BudgetSpec = Field(default_factory=BudgetSpec)


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
    revision_id: str


class ReviewCreateRequest(ApiModel):
    label: str = Field(min_length=1, max_length=128)
    notes: str = Field(default="", max_length=4096)
