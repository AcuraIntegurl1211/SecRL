from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from types import MappingProxyType
from typing import Any


OVERLAY_SCHEMA_VERSION = 'sql_retrieval_subtyping_v1'


def _freeze_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise TypeError('non-finite floats are not valid JSON values')
        return value
    if isinstance(value, Mapping):
        frozen: dict[str, object] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError('JSON object keys must be strings')
            frozen[key] = _freeze_json_value(item)
        return MappingProxyType(frozen)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json_value(item) for item in value)
    raise TypeError(f'unsupported non-JSON value: {type(value).__name__}')


def thaw_json_value(value: object) -> object:
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Mapping):
        return {key: thaw_json_value(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [thaw_json_value(item) for item in value]
    raise TypeError(f'unsupported frozen JSON value: {type(value).__name__}')


@dataclass(frozen=True)
class QueryStep:
    step: int
    sql: str
    observation: str
    query_success: bool | None


@dataclass(frozen=True)
class RetrievalEvidenceBundle:
    incident: str
    question_index: int
    question_fingerprint_sha256: str
    question_text_fingerprint_sha256: str
    question: str
    context: str
    golden_answer: object
    golden_solution: object
    submitted_answer: str
    reward_official: float
    reviewed_primary_original: str
    review_notes_original: str
    agent_source_index: int
    env_source_index: int
    agent_source_sha256: str
    env_source_sha256: str
    question_source_sha256: str
    query_steps: tuple[QueryStep, ...]

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            'golden_answer',
            _freeze_json_value(self.golden_answer),
        )
        object.__setattr__(
            self,
            'golden_solution',
            _freeze_json_value(self.golden_solution),
        )

    @staticmethod
    def fixture_for_test() -> 'RetrievalEvidenceBundle':
        return RetrievalEvidenceBundle(
            incident='incident_fixture',
            question_index=0,
            question_fingerprint_sha256='a' * 64,
            question_text_fingerprint_sha256='b' * 64,
            question='Which service failed?',
            context='Fixture context.',
            golden_answer={'answer': 'example-service'},
            golden_solution={'solution': 'Inspect the service event table.'},
            submitted_answer='example-service',
            reward_official=1.0,
            reviewed_primary_original='SQL_RETRIEVAL',
            review_notes_original='',
            agent_source_index=0,
            env_source_index=0,
            agent_source_sha256='c' * 64,
            env_source_sha256='d' * 64,
            question_source_sha256='e' * 64,
            query_steps=(
                QueryStep(
                    step=1,
                    sql='SELECT service FROM events LIMIT 1',
                    observation='example-service',
                    query_success=True,
                ),
            ),
        )


@dataclass(frozen=True)
class RetrievalDecision:
    retrieval_primary_subtype: str
    auxiliary_tags: tuple[str, ...]
    retrieval_outcome: str
    boundary_flag: str
    confidence: str
    decision_status: str
    first_divergence_step: int | None
    relevant_sql_steps: tuple[int, ...]
    sql_evidence: str
    observation_evidence: str
    gold_evidence_basis: str
    rationale: str

    def as_dict(self) -> dict[str, Any]:
        result = asdict(self)
        result['auxiliary_tags'] = sorted(set(self.auxiliary_tags))
        result['relevant_sql_steps'] = sorted(set(self.relevant_sql_steps))
        return result
