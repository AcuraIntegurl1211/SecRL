from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = "failure_attribution_v1"


class AnalysisError(Exception):
    exit_code = 2


class InputError(AnalysisError):
    exit_code = 2


class MappingError(AnalysisError):
    exit_code = 3


class OutputCollisionError(AnalysisError):
    exit_code = 4


class ReviewError(AnalysisError):
    exit_code = 5


@dataclass(frozen=True)
class QuestionIdentity:
    incident: str
    question_index: int
    question_fingerprint_sha256: str
    question_text_fingerprint_sha256: str


@dataclass(frozen=True)
class Evidence:
    kind: str
    step: int | None
    source: str
    field: str
    excerpt: str
    excerpt_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MappedQuestion:
    identity: QuestionIdentity
    question: dict[str, Any]
    agent: dict[str, Any]
    env: dict[str, Any]
    agent_source_index: int
    env_source_index: int


@dataclass
class FeatureRecord:
    mapped: MappedQuestion
    reward_official: float
    submitted_answer: str
    sql_total: int
    sql_success: int
    sql_failure: int
    empty_result_count: int
    duplicate_query_count: int
    steps: int
    max_steps: int
    submitted: bool
    submitted_at_step_limit: bool
    gold_evidence_match: str
    gold_evidence_steps: list[int] = field(default_factory=list)
    evaluator_fields_complete: bool = False
    agent_prompt_tokens: int | None = None
    agent_completion_tokens: int | None = None
    agent_total_tokens: int | None = None
    evaluator_tokens: int | None = None
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Attribution:
    primary_cause_candidate: str | None
    primary_cause_status: str
    secondary_cause_candidates: list[str]
    confidence: str
    needs_human_review: bool
    human_review_reasons: list[str]
    reviewed_primary: str | None = None
    reviewed_secondary: list[str] = field(default_factory=list)
    review_status: str = "unreviewed"
    review_notes: str = ""
    explanation: str = ""
