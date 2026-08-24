from __future__ import annotations

from .features import normalize_sql
from .models import MappingError
from .retrieval_models import QueryStep, RetrievalDecision, RetrievalEvidenceBundle


_EVIDENCE_SEGMENT_LIMIT = 240


def _validate_step_limit_contract(bundle: RetrievalEvidenceBundle) -> None:
    if type(bundle.trajectory_steps) is not int or bundle.trajectory_steps < 0:
        raise MappingError('trajectory_steps must be a non-negative integer')
    if type(bundle.submitted) is not bool:
        raise MappingError('submitted must be a boolean')
    if type(bundle.submitted_at_step_limit) is not bool:
        raise MappingError('submitted_at_step_limit must be a boolean')
    if bundle.submitted and bundle.trajectory_steps <= 0:
        raise MappingError('submitted requires trajectory_steps greater than zero')
    if bundle.submitted_at_step_limit and not bundle.submitted:
        raise MappingError('submitted_at_step_limit requires submitted')
    if bundle.submitted_at_step_limit and bundle.trajectory_steps <= 0:
        raise MappingError(
            'submitted_at_step_limit requires trajectory_steps greater than zero'
        )


def _validate_query_steps(bundle: RetrievalEvidenceBundle) -> None:
    if type(bundle.query_steps) is not tuple:
        raise MappingError('query_steps must be a tuple')

    previous_step = 0
    for item in bundle.query_steps:
        if type(item) is not QueryStep:
            raise MappingError('query_steps items must be QueryStep values')
        if type(item.step) is not int or item.step <= previous_step:
            raise MappingError(
                'query_steps step must be an exact positive, strictly increasing integer'
            )
        if item.step > bundle.trajectory_steps:
            raise MappingError('query_steps step must not exceed trajectory_steps')
        if type(item.sql) is not str:
            raise MappingError('query_steps sql must be an exact string')
        if type(item.observation) is not str:
            raise MappingError('query_steps observation must be an exact string')
        if item.query_success is not None and type(item.query_success) is not bool:
            raise MappingError('query_steps query_success must be None or an exact boolean')
        previous_step = item.step


def _evidence_segment(prefix: str, value: str) -> str:
    segment = prefix + ' '.join(value.split())
    if len(segment) <= _EVIDENCE_SEGMENT_LIMIT:
        return segment
    return segment[:_EVIDENCE_SEGMENT_LIMIT - 1] + '…'


def _evidence_text(
    relevant_steps: tuple[int, ...],
    query_by_step: dict[int, QueryStep],
) -> tuple[str, str]:
    sql_segments = []
    observation_segments = []
    for step_number in relevant_steps:
        query_step = query_by_step[step_number]
        sql_segments.append(
            _evidence_segment(f'step={step_number} sql=', query_step.sql)
        )
        observation_kind = (
            'error' if query_step.query_success is False else 'observation'
        )
        observation_segments.append(
            _evidence_segment(
                f'step={step_number} {observation_kind}=',
                query_step.observation,
            )
        )
    return '\n'.join(sql_segments), '\n'.join(observation_segments)


def suggest_prelabel(bundle: RetrievalEvidenceBundle) -> RetrievalDecision:
    _validate_step_limit_contract(bundle)
    _validate_query_steps(bundle)

    tags: set[str] = set()
    relevant_steps: set[int] = set()
    query_by_step: dict[int, QueryStep] = {}
    normalized_sql_seen: set[str] = set()
    empty_success_count = 0
    nonempty_success_count = 0

    for query_step in sorted(bundle.query_steps, key=lambda item: item.step):
        query_by_step[query_step.step] = query_step

        normalized_sql = normalize_sql(query_step.sql)
        if normalized_sql:
            if normalized_sql in normalized_sql_seen:
                tags.add('REPEATED_QUERY')
                relevant_steps.add(query_step.step)
            else:
                normalized_sql_seen.add(normalized_sql)

        observation = query_step.observation.strip()
        if query_step.query_success is True and observation == '[]':
            tags.add('EMPTY_RESULT')
            relevant_steps.add(query_step.step)
            empty_success_count += 1
        elif query_step.query_success is True and observation:
            nonempty_success_count += 1
        elif query_step.query_success is False:
            tags.add('SQL_ERROR_PRESENT')
            relevant_steps.add(query_step.step)

    if (
        bundle.submitted
        and bundle.submitted_at_step_limit
        and bundle.trajectory_steps > 0
    ):
        tags.add('STEP_LIMIT')

    if empty_success_count and not nonempty_success_count:
        retrieval_outcome = 'EMPTY'
    elif empty_success_count and nonempty_success_count:
        retrieval_outcome = 'MIXED'
    else:
        retrieval_outcome = 'UNOBSERVED'

    ordered_relevant_steps = tuple(sorted(relevant_steps))
    sql_evidence, observation_evidence = _evidence_text(
        ordered_relevant_steps,
        query_by_step,
    )

    return RetrievalDecision(
        retrieval_primary_subtype='INDETERMINATE',
        auxiliary_tags=tuple(sorted(tags)),
        retrieval_outcome=retrieval_outcome,
        boundary_flag='NONE',
        confidence='low',
        decision_status='needs_review',
        first_divergence_step=None,
        relevant_sql_steps=ordered_relevant_steps,
        sql_evidence=sql_evidence,
        observation_evidence=observation_evidence,
        gold_evidence_basis='',
        rationale=(
            'Objective prelabel only; semantic review is required before final classification.'
        ),
    )
