from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter


class ProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class BenchmarkManifest(ProtocolModel):
    benchmark_id: str
    name: str
    version: str
    protocol_version: str = "1"


class DatasetManifest(ProtocolModel):
    dataset_id: str
    version: str
    sha256: str
    case_count: int = Field(ge=0)


class DatasetRef(ProtocolModel):
    dataset_id: str
    version: str
    sha256: str
    source: Path | None = None


class ValidationReport(ProtocolModel):
    valid: bool
    errors: tuple[str, ...] = ()


class ScenarioRef(ProtocolModel):
    id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class CaseRef(ProtocolModel):
    id: str
    scenario: ScenarioRef
    public_input: dict[str, Any]


class Scope(ProtocolModel):
    case_ids: tuple[str, ...] | None = None

    @classmethod
    def all(cls) -> "Scope":
        return cls()


class EpisodeRef(ProtocolModel):
    id: str
    case_id: str


class Submission(ProtocolModel):
    answer: str


class ToolDefinition(ProtocolModel):
    name: str
    description: str
    parameters: dict[str, Any]


class ToolCallAction(ProtocolModel):
    type: Literal["tool_call"]
    tool: str
    arguments: dict[str, Any]


class SubmitAction(ProtocolModel):
    type: Literal["submit"]
    answer: str


class YieldAction(ProtocolModel):
    type: Literal["yield"]
    reason: str = ""


AgentAction = Annotated[
    ToolCallAction | SubmitAction | YieldAction,
    Field(discriminator="type"),
]
_AGENT_ACTION_ADAPTER = TypeAdapter(AgentAction)


def parse_agent_action(payload: Any) -> AgentAction:
    return _AGENT_ACTION_ADAPTER.validate_python(payload)


class Observation(ProtocolModel):
    type: str
    content: dict[str, Any]
    truncated: bool = False
    ref: EpisodeRef | None = None
    terminal: bool = False


class EnvironmentLease(ProtocolModel):
    id: str
    scenario: ScenarioRef


class EvaluationResult(ProtocolModel):
    reward: float
    metrics: dict[str, float] = Field(default_factory=dict)
    correct: bool = False


class MetricDefinition(ProtocolModel):
    name: str
    description: str
    higher_is_better: bool = True


class BenchmarkAdapterProtocol(Protocol):
    def manifest(self) -> BenchmarkManifest: ...

    def validate_dataset(self, source: Path) -> ValidationReport: ...

    def enumerate_cases(self, dataset: DatasetRef, scope: Scope) -> list[CaseRef]: ...

    def tool_definitions(self) -> list[ToolDefinition]: ...

    def prepare_scenario(self, scenario: ScenarioRef) -> EnvironmentLease: ...

    def start_episode(self, case: CaseRef, lease: EnvironmentLease) -> Observation: ...

    def execute_action(
        self,
        episode: EpisodeRef,
        action: AgentAction,
    ) -> Observation: ...

    def evaluate(
        self,
        episode: EpisodeRef,
        submission: Submission,
    ) -> EvaluationResult: ...

    def close_episode(self, episode: EpisodeRef) -> None: ...

    def release_scenario(self, lease: EnvironmentLease) -> None: ...
