from __future__ import annotations

from typing import Literal


ScopeMode = Literal["CASES", "INCIDENTS", "ALL_BENCHMARK"]


class AmbiguousScopeError(ValueError):
    """Raised when more than one scope source is supplied."""


class InvalidScopeError(ValueError):
    """Raised when a scope mode does not match its selected fields."""


def canonical_scope_mode(
    *,
    scope_mode: ScopeMode | None,
    case_ids: tuple[str, ...],
    incident_ids: tuple[str, ...],
    all_cases: bool,
    allow_empty_all_benchmark: bool = False,
) -> ScopeMode:
    if (case_ids and incident_ids) or (all_cases and (case_ids or incident_ids)):
        raise AmbiguousScopeError(
            "ambiguous scope: choose exactly one of case_ids, incident_ids, or the full benchmark"
        )

    if scope_mode is None:
        if case_ids:
            return "CASES"
        if incident_ids:
            return "INCIDENTS"
        if all_cases or allow_empty_all_benchmark:
            return "ALL_BENCHMARK"
        raise InvalidScopeError("a scope mode and selection are required")

    if scope_mode == "CASES":
        if not case_ids or incident_ids or all_cases:
            raise InvalidScopeError("CASES scope requires case_ids only")
    elif scope_mode == "INCIDENTS":
        if not incident_ids or case_ids or all_cases:
            raise InvalidScopeError("INCIDENTS scope requires incident_ids only")
    elif scope_mode == "ALL_BENCHMARK":
        if case_ids or incident_ids:
            raise InvalidScopeError("ALL_BENCHMARK scope cannot include Case or Incident fields")
    else:  # pragma: no cover - Literal validation protects API callers.
        raise InvalidScopeError("unsupported scope mode")
    return scope_mode
