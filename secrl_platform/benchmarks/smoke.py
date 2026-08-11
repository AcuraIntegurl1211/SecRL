from __future__ import annotations

import hashlib
import json
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jsonschema import ValidationError as JsonSchemaValidationError
from jsonschema import validate

from secrl_platform.benchmarks.protocol import (
    AgentAction,
    BenchmarkManifest,
    CaseRef,
    DatasetRef,
    EnvironmentLease,
    EpisodeRef,
    EvaluationResult,
    Observation,
    ScenarioRef,
    Scope,
    Submission,
    SubmitAction,
    ToolCallAction,
    ToolDefinition,
    ValidationReport,
    YieldAction,
    parse_agent_action,
)


PROTOCOL_SMOKE_V1_SHA256 = (
    "0176c8d62309548c0179d8b10f70cfa23774a474f66bfe290ca276fdff55e7c0"
)
_DATASET_PATH = Path(__file__).parent / "data" / "protocol_smoke_v1.json"


class ProtocolSmokeManifest(BenchmarkManifest):
    dataset_version: str
    dataset_sha256: str


@dataclass(frozen=True)
class _SmokeCase:
    id: str
    public_input: dict[str, Any]
    documents: dict[str, str]
    answer: str
    tags: tuple[str, ...]
    max_steps: int
    max_observation_chars: int


@dataclass
class _EpisodeState:
    ref: EpisodeRef
    case: _SmokeCase
    step_count: int = 0


_DATASET_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["dataset_id", "version", "scenario_id", "cases"],
    "properties": {
        "dataset_id": {"const": "protocol-smoke"},
        "version": {"type": "string", "minLength": 1},
        "scenario_id": {"type": "string", "minLength": 1},
        "cases": {
            "type": "array",
            "minItems": 12,
            "maxItems": 12,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "id",
                    "public_input",
                    "documents",
                    "gold",
                    "tags",
                    "max_steps",
                    "max_observation_chars",
                ],
                "properties": {
                    "id": {"type": "string", "pattern": "^smoke-[0-9]{3}$"},
                    "public_input": {
                        "type": "object",
                        "required": ["question"],
                        "properties": {"question": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "documents": {
                        "type": "object",
                        "minProperties": 1,
                        "additionalProperties": {"type": "string"},
                    },
                    "gold": {
                        "type": "object",
                        "required": ["answer"],
                        "properties": {"answer": {"type": "string"}},
                        "additionalProperties": False,
                    },
                    "tags": {
                        "type": "array",
                        "minItems": 1,
                        "items": {"type": "string"},
                    },
                    "max_steps": {"type": "integer", "minimum": 1},
                    "max_observation_chars": {"type": "integer", "minimum": 1},
                },
            },
        },
    },
}


class ProtocolSmokeAdapter:
    def __init__(self, source: Path, payload: dict[str, Any]) -> None:
        self._source = source
        self._payload = payload
        self._dataset_sha256 = _canonical_sha256(payload)
        self._scenario = ScenarioRef(id=payload["scenario_id"])
        self._cases = {
            record["id"]: _SmokeCase(
                id=record["id"],
                public_input=record["public_input"],
                documents=record["documents"],
                answer=record["gold"]["answer"],
                tags=tuple(record["tags"]),
                max_steps=record["max_steps"],
                max_observation_chars=record["max_observation_chars"],
            )
            for record in payload["cases"]
        }
        self._case_order = tuple(record["id"] for record in payload["cases"])
        self._episodes: dict[str, _EpisodeState] = {}
        self._leases: set[str] = set()
        self._tools = _tool_definitions()
        self._tool_schemas = {tool.name: tool.parameters for tool in self._tools}

    @classmethod
    def load_default(cls) -> "ProtocolSmokeAdapter":
        payload = json.loads(_DATASET_PATH.read_text(encoding="utf-8"))
        report = cls._validate_payload(payload)
        if not report.valid:
            raise ValueError("invalid Protocol-Smoke dataset: " + "; ".join(report.errors))
        digest = _canonical_sha256(payload)
        if digest != PROTOCOL_SMOKE_V1_SHA256:
            raise ValueError(
                "Protocol-Smoke dataset hash mismatch; update the dataset version and hash"
            )
        return cls(_DATASET_PATH, payload)

    def manifest(self) -> ProtocolSmokeManifest:
        return ProtocolSmokeManifest(
            benchmark_id="protocol-smoke",
            name="Protocol-Smoke",
            version="1.0.0",
            dataset_version=self._payload["version"],
            dataset_sha256=self._dataset_sha256,
        )

    def dataset_ref(self) -> DatasetRef:
        return DatasetRef(
            dataset_id=self._payload["dataset_id"],
            version=self._payload["version"],
            sha256=self._dataset_sha256,
            source=self._source,
        )

    def validate_dataset(self, source: Path) -> ValidationReport:
        try:
            payload = json.loads(Path(source).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            return ValidationReport(valid=False, errors=(str(error),))
        return self._validate_payload(payload)

    @staticmethod
    def _validate_payload(payload: Any) -> ValidationReport:
        try:
            validate(instance=payload, schema=_DATASET_SCHEMA)
        except JsonSchemaValidationError as error:
            return ValidationReport(valid=False, errors=(error.message,))
        ids = [record["id"] for record in payload["cases"]]
        if len(ids) != len(set(ids)):
            return ValidationReport(valid=False, errors=("case ids must be unique",))
        return ValidationReport(valid=True)

    def enumerate_cases(self, dataset: DatasetRef, scope: Scope) -> list[CaseRef]:
        if dataset != self.dataset_ref():
            raise ValueError("dataset reference does not match Protocol-Smoke v1")
        selected = set(scope.case_ids) if scope.case_ids is not None else None
        unknown = selected.difference(self._cases) if selected is not None else set()
        if unknown:
            raise KeyError(sorted(unknown)[0])
        return [
            CaseRef(
                id=case.id,
                scenario=self._scenario,
                public_input=case.public_input,
            )
            for case_id in self._case_order
            if selected is None or case_id in selected
            for case in (self._cases[case_id],)
        ]

    def tool_definitions(self) -> list[ToolDefinition]:
        return list(self._tools)

    def prepare_scenario(self, scenario: ScenarioRef) -> EnvironmentLease:
        if scenario != self._scenario:
            raise ValueError(f"unknown scenario: {scenario.id}")
        lease = EnvironmentLease(id=str(uuid.uuid4()), scenario=scenario)
        self._leases.add(lease.id)
        return lease

    def start_episode(self, case: CaseRef, lease: EnvironmentLease) -> Observation:
        if lease.id not in self._leases or lease.scenario != case.scenario:
            raise ValueError("scenario lease is not active for this case")
        smoke_case = self._cases.get(case.id)
        if smoke_case is None:
            raise KeyError(case.id)
        ref = EpisodeRef(id=str(uuid.uuid4()), case_id=case.id)
        self._episodes[ref.id] = _EpisodeState(ref=ref, case=smoke_case)
        return Observation(type="episode_start", content=case.public_input, ref=ref)

    def execute_action(self, episode: EpisodeRef, action: AgentAction) -> Observation:
        state = self._state_for(episode)
        action = parse_agent_action(action)
        if state.step_count >= state.case.max_steps:
            return self._error(state, "max_steps_exceeded", terminal=True)
        state.step_count += 1

        if isinstance(action, YieldAction):
            return Observation(
                type="yield",
                content={"reason": action.reason},
                ref=state.ref,
            )
        if isinstance(action, SubmitAction):
            schema = self._tool_schemas["submit"]
            if error := _schema_error({"answer": action.answer}, schema):
                return self._error(state, "invalid_tool_arguments", detail=error)
            return Observation(
                type="submission",
                content={"answer": action.answer},
                ref=state.ref,
                terminal=True,
            )
        if not isinstance(action, ToolCallAction):
            return self._error(state, "invalid_action")
        schema = self._tool_schemas.get(action.tool)
        if schema is None:
            return self._error(state, "unknown_tool")
        if error := _schema_error(action.arguments, schema):
            return self._error(state, "invalid_tool_arguments", detail=error)
        if action.tool == "search":
            return self._search(state, action.arguments["query"])
        if action.tool == "read":
            return self._read(state, action.arguments["id"])
        return Observation(
            type="submission",
            content={"answer": action.arguments["answer"]},
            ref=state.ref,
            terminal=True,
        )

    def evaluate(self, episode: EpisodeRef, submission: Submission) -> EvaluationResult:
        state = self._state_for(episode)
        correct = _normalized(submission.answer) == _normalized(state.case.answer)
        reward = 1.0 if correct else 0.0
        return EvaluationResult(
            reward=reward,
            correct=correct,
            metrics={"exact_match": reward},
        )

    def close_episode(self, episode: EpisodeRef) -> None:
        state = self._state_for(episode)
        del self._episodes[state.ref.id]

    def release_scenario(self, lease: EnvironmentLease) -> None:
        self._leases.discard(lease.id)

    def _state_for(self, episode: EpisodeRef) -> _EpisodeState:
        state = self._episodes.get(episode.id)
        if state is None or state.ref != episode:
            raise KeyError(episode.id)
        return state

    def _search(self, state: _EpisodeState, query: str) -> Observation:
        folded_query = query.casefold()
        matches = sorted(
            document_id
            for document_id, content in state.case.documents.items()
            if folded_query in document_id.casefold() or folded_query in content.casefold()
        )
        return Observation(
            type="tool_result",
            content={"matches": matches},
            ref=state.ref,
        )

    def _read(self, state: _EpisodeState, document_id: str) -> Observation:
        text = state.case.documents.get(document_id)
        if text is None:
            return self._error(state, "unknown_key")
        limit = state.case.max_observation_chars
        truncated = len(text) > limit
        return Observation(
            type="tool_result",
            content={
                "text": text[:limit],
                "original_length": len(text),
            },
            truncated=truncated,
            ref=state.ref,
        )

    @staticmethod
    def _error(
        state: _EpisodeState,
        code: str,
        *,
        detail: str | None = None,
        terminal: bool = False,
    ) -> Observation:
        content = {"error": code}
        if detail is not None:
            content["detail"] = detail
        return Observation(
            type="error",
            content=content,
            ref=state.ref,
            terminal=terminal,
        )


def _tool_definitions() -> tuple[ToolDefinition, ...]:
    return (
        ToolDefinition(
            name="search",
            description="Find document IDs containing a query.",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["query"],
                "properties": {"query": {"type": "string", "minLength": 1}},
            },
        ),
        ToolDefinition(
            name="read",
            description="Read one document by opaque ID.",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["id"],
                "properties": {"id": {"type": "string", "minLength": 1}},
            },
        ),
        ToolDefinition(
            name="submit",
            description="Submit a final answer.",
            parameters={
                "type": "object",
                "additionalProperties": False,
                "required": ["answer"],
                "properties": {"answer": {"type": "string"}},
            },
        ),
    )


def _schema_error(arguments: dict[str, Any], schema: dict[str, Any]) -> str | None:
    try:
        validate(instance=arguments, schema=schema)
    except JsonSchemaValidationError as error:
        return error.message
    return None


def _canonical_sha256(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()
