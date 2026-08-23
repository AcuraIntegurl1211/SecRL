"""SecRL dataset adapter.

The adapter deliberately keeps answers and solutions out of the protocol
objects handed to an agent.  Gold is only available through an opaque,
process-local capability held by the evaluator.
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping

from pydantic import Field

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


SECRL_EXPECTED_SCENARIO_COUNTS: dict[str, int] = {
    "incident_5": 98,
    "incident_34": 82,
    "incident_38": 11,
    "incident_39": 98,
    "incident_55": 100,
    "incident_134": 57,
    "incident_166": 87,
    "incident_322": 56,
}
SECRL_EXPECTED_CASE_COUNT = sum(SECRL_EXPECTED_SCENARIO_COUNTS.values())
SECRL_DATASET_SHA256 = "cc1fd79db8627768611b8b230c23d5cb11c19b50ad25f3810dba3fe8adef8e8f"
_DEFAULT_DATASET = Path(__file__).resolve().parents[2] / "secgym" / "questions" / "o1" / "test"


class UnsafeSQL(ValueError):
    """Raised when a proposed query is not read-only and single-statement."""


class _RestrictedAccess:
    __slots__ = ()


@dataclass(frozen=True)
class SourceArtifactRef:
    case_id: str
    sha256: str
    size: int


@dataclass(frozen=True)
class _Case:
    case_id: str
    scenario_id: str
    ordinal: int
    public_input: dict[str, Any]
    source: dict[str, Any]

    @property
    def answer(self) -> str:
        return str(self.source["answer"])


class SecRLManifest(BenchmarkManifest):
    dataset_version: str
    dataset_sha256: str


class SecRLValidationReport(ValidationReport):
    case_count: int = Field(default=0, ge=0)
    scenario_counts: dict[str, int] = Field(default_factory=dict)


class SecRLRunSpec:
    """Frozen execution limits shared by environment and runner adapters."""

    __slots__ = ("max_steps", "max_str_len", "max_entry_return")

    def __init__(self, max_steps: int = 15, max_str_len: int = 100_000, max_entry_return: int = 15) -> None:
        for name, value in (("max_steps", max_steps), ("max_str_len", max_str_len), ("max_entry_return", max_entry_return)):
            if not isinstance(value, int) or value < 1:
                raise ValueError(f"{name} must be a positive integer")
            object.__setattr__(self, name, value)

    def __setattr__(self, _name: str, _value: Any) -> None:
        raise TypeError("RunSpec is frozen")


@dataclass
class _Episode:
    ref: EpisodeRef
    case: _Case
    lease_id: str
    steps: int = 0


class SecRLAdapter:
    """Read-only, deterministic import of the checked-in SecRL questions."""

    def __init__(
        self,
        source: Path | None = None,
        *,
        query_executor: Callable[[str, str], Any] | Callable[[str], Any] | None = None,
        run_spec: SecRLRunSpec | None = None,
    ) -> None:
        self._source = Path(source or _DEFAULT_DATASET)
        self._query_executor = query_executor
        self.run_spec = run_spec or SecRLRunSpec()
        self._access = _RestrictedAccess()
        self._leases: dict[str, str] = {}
        self._episodes: dict[str, _Episode] = {}
        self._cases, self._source_sha256 = self._load(self._source)
        self._case_order = tuple(self._cases)

    @staticmethod
    def _canonical(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @classmethod
    def _load(cls, source: Path) -> tuple[dict[str, _Case], str]:
        if not source.exists() or not source.is_dir():
            raise ValueError(f"SecRL dataset directory does not exist: {source}")
        records: dict[str, _Case] = {}
        source_payload: dict[str, list[dict[str, Any]]] = {}
        for path in sorted(source.glob("*.json")):
            incident = re.match(r"(incident_[0-9]+)", path.name)
            if incident is None:
                continue
            rows = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(rows, list):
                raise ValueError(f"dataset file must contain a list: {path}")
            source_payload[incident.group(1)] = rows
            for ordinal, row in enumerate(rows):
                if not isinstance(row, dict) or not isinstance(row.get("question"), str):
                    raise ValueError(f"invalid question record: {path}:{ordinal}")
                question_sha = hashlib.sha256(cls._canonical(row["question"]).encode("utf-8")).hexdigest()
                case_id = f"{incident.group(1)}:{ordinal}:{question_sha}"
                if case_id in records:
                    raise ValueError(f"duplicate case identity: {case_id}")
                public = {
                    "incident": incident.group(1),
                    "ordinal": ordinal,
                    "context": row.get("context", ""),
                    "question": row["question"],
                    "question_sha256": question_sha,
                }
                records[case_id] = _Case(
                    case_id=case_id,
                    scenario_id=incident.group(1),
                    ordinal=ordinal,
                    public_input=public,
                    source=dict(row),
                )
        digest = hashlib.sha256(cls._canonical(source_payload).encode("utf-8")).hexdigest()
        return records, digest

    def manifest(self) -> SecRLManifest:
        return SecRLManifest(
            benchmark_id="secrl",
            name="SecRL",
            version="1.0.0",
            dataset_version="o1-test",
            dataset_sha256=self._source_sha256,
        )

    def dataset_ref(self) -> DatasetRef:
        return DatasetRef(
            dataset_id="secrl-o1-test",
            version="o1-test",
            sha256=self._source_sha256,
            source=self._source,
        )

    @staticmethod
    def scope_all() -> Scope:
        return Scope.all()

    def validate_dataset(self, source: Path) -> SecRLValidationReport:
        try:
            cases, digest = self._load(Path(source))
        except (OSError, ValueError, json.JSONDecodeError) as error:
            return SecRLValidationReport(valid=False, errors=(str(error),))
        counts: dict[str, int] = {}
        for case in cases.values():
            counts[case.scenario_id] = counts.get(case.scenario_id, 0) + 1
        errors: list[str] = []
        if len(cases) != SECRL_EXPECTED_CASE_COUNT:
            errors.append(f"expected {SECRL_EXPECTED_CASE_COUNT} cases, got {len(cases)}")
        if counts != SECRL_EXPECTED_SCENARIO_COUNTS:
            errors.append(f"scenario counts mismatch: {counts}")
        if digest != SECRL_DATASET_SHA256:
            errors.append("dataset SHA-256 does not match the frozen SecRL o1-test baseline")
        return SecRLValidationReport(
            valid=not errors,
            errors=tuple(errors),
            case_count=len(cases),
            scenario_counts=counts,
        )

    def enumerate_cases(self, dataset: DatasetRef, scope: Scope) -> list[CaseRef]:
        if dataset != self.dataset_ref():
            raise ValueError("dataset reference does not match SecRL o1-test")
        selected = set(scope.case_ids) if scope.case_ids is not None else None
        unknown = (selected or set()).difference(self._cases)
        if unknown:
            raise KeyError(sorted(unknown)[0])
        return [
            CaseRef(
                id=case.case_id,
                scenario=ScenarioRef(id=case.scenario_id, metadata={"benchmark": "secrl"}),
                public_input=dict(case.public_input),
            )
            for case in self._cases.values()
            if selected is None or case.case_id in selected
        ]

    def source_artifact(self, case_id: str) -> SourceArtifactRef:
        case = self._case(case_id)
        payload = self._canonical(case.source).encode("utf-8")
        return SourceArtifactRef(case_id=case_id, sha256=hashlib.sha256(payload).hexdigest(), size=len(payload))

    def restricted_access(self) -> _RestrictedAccess:
        return self._access

    def read_source_artifact(self, case_id: str, access: _RestrictedAccess | None = None) -> dict[str, Any]:
        if access is not self._access:
            raise PermissionError("restricted source artifact")
        return dict(self._case(case_id).source)

    def gold_for(self, case_id: str, access: _RestrictedAccess) -> Mapping[str, Any]:
        if access is not self._access:
            raise PermissionError("restricted gold payload")
        source = self._case(case_id).source
        return {"answer": source["answer"], "solution": tuple(source.get("solution", []))}

    def tool_definitions(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="sql_query",
                description="Execute one read-only SQL query in the leased SecRL environment.",
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["query"],
                    "properties": {"query": {"type": "string", "minLength": 1}},
                },
            ),
            ToolDefinition(
                name="submit",
                description="Submit the final answer for the current question.",
                parameters={
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                },
            ),
        ]

    def prepare_scenario(self, scenario: ScenarioRef) -> EnvironmentLease:
        if scenario.id not in SECRL_EXPECTED_SCENARIO_COUNTS:
            raise KeyError(scenario.id)
        lease = EnvironmentLease(id=str(uuid.uuid4()), scenario=ScenarioRef(id=scenario.id))
        self._leases[lease.id] = scenario.id
        return lease

    def start_episode(self, case: CaseRef, lease: EnvironmentLease) -> Observation:
        internal = self._case(case.id)
        if self._leases.get(lease.id) != internal.scenario_id or lease.scenario.id != internal.scenario_id:
            raise ValueError("scenario lease is not active for this case")
        ref = EpisodeRef(id=str(uuid.uuid4()), case_id=case.id)
        self._episodes[ref.id] = _Episode(ref=ref, case=internal, lease_id=lease.id)
        return Observation(
            type="episode_start",
            content=dict(case.public_input),
            ref=ref,
        )

    def execute_action(self, episode: EpisodeRef, action: AgentAction) -> Observation:
        state = self._episodes.get(episode.id)
        if state is None or state.ref != episode:
            raise KeyError(episode.id)
        parsed = parse_agent_action(action)
        if state.steps >= self.run_spec.max_steps:
            return self._error(state, "max_steps_exceeded", terminal=True)
        state.steps += 1
        if isinstance(parsed, YieldAction):
            return Observation(type="yield", content={"reason": parsed.reason}, ref=episode)
        if isinstance(parsed, SubmitAction):
            return Observation(type="submission", content={"answer": parsed.answer}, ref=episode, terminal=True)
        if not isinstance(parsed, ToolCallAction) or parsed.tool != "sql_query":
            return self._error(state, "unknown_tool")
        query = parsed.arguments.get("query")
        if not isinstance(query, str):
            return self._error(state, "invalid_tool_arguments")
        try:
            self.validate_sql(query)
        except UnsafeSQL:
            return self._error(state, "unsafe_sql")
        if self._query_executor is None:
            return self._error(state, "environment_unavailable")
        try:
            try:
                result = self._query_executor(state.case.scenario_id, query)
            except TypeError:
                result = self._query_executor(query)  # type: ignore[misc]
        except Exception:
            return self._error(state, "environment_error")
        raw = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        original_length = len(raw)
        truncated = original_length > self.run_spec.max_str_len
        if truncated:
            raw = raw[: self.run_spec.max_str_len]
        return Observation(
            type="tool_result",
            content={"result": raw, "original_length": original_length},
            truncated=truncated,
            ref=episode,
        )

    def evaluate(self, episode: EpisodeRef, submission: Submission) -> EvaluationResult:
        state = self._episodes.get(episode.id)
        if state is None:
            raise KeyError(episode.id)
        correct = _normalize(submission.answer) == _normalize(state.case.answer)
        reward = 1.0 if correct else 0.0
        return EvaluationResult(reward=reward, correct=correct, metrics={"exact_match": reward})

    def close_episode(self, episode: EpisodeRef) -> None:
        if episode.id not in self._episodes:
            raise KeyError(episode.id)
        del self._episodes[episode.id]

    def release_scenario(self, lease: EnvironmentLease) -> None:
        self._leases.pop(lease.id, None)

    @staticmethod
    def validate_sql(query: str) -> bool:
        candidate = query.strip()
        if not candidate:
            raise UnsafeSQL("empty SQL query")
        if ";" in candidate.rstrip(";") or "--" in candidate or "/*" in candidate or "*/" in candidate:
            raise UnsafeSQL("multiple statements and SQL comments are not allowed")
        if not re.match(r"^(?:SELECT|SHOW|EXPLAIN|WITH)\b", candidate, flags=re.IGNORECASE):
            raise UnsafeSQL("only read-only SQL is allowed")
        forbidden = re.compile(
            r"\b(?:INSERT|UPDATE|DELETE|DROP|ALTER|CREATE|TRUNCATE|REPLACE|GRANT|REVOKE|CALL|SET|USE|LOAD_FILE|OUTFILE|DUMPFILE|SHUTDOWN|INSTALL|UNINSTALL|HANDLER|LOCK|UNLOCK|FLUSH|RESET)\b",
            flags=re.IGNORECASE,
        )
        if forbidden.search(candidate):
            raise UnsafeSQL("forbidden SQL operation")
        return True

    def _case(self, case_id: str) -> _Case:
        try:
            return self._cases[case_id]
        except KeyError as exc:
            raise KeyError(case_id) from exc

    @staticmethod
    def _error(state: _Episode, code: str, *, terminal: bool = False) -> Observation:
        return Observation(type="error", content={"error": code}, ref=state.ref, terminal=terminal)


def _normalize(value: str) -> str:
    return " ".join(value.casefold().strip().split())
