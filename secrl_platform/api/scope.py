from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal


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


def task_scope_summary(task_spec: Mapping[str, Any]) -> dict[str, object]:
    """Project frozen scope metadata for APIs and legacy RunSpec readers.

    v0.1.0 stored the frozen Case list but did not store the explicit scope
    selection or counts.  The Case list is authoritative for those records;
    this projection never writes back to the historical RunSpec.
    """
    selection = task_spec.get("selection")
    has_frozen_metadata = isinstance(selection, Mapping) and isinstance(
        selection.get("scope_mode"), str
    )
    case_ids = task_spec.get("case_ids")
    frozen_case_ids = tuple(case_ids) if isinstance(case_ids, (list, tuple)) else ()

    if has_frozen_metadata:
        assert isinstance(selection, Mapping)
        mode = str(selection["scope_mode"])
        case_count = _nonnegative_count(
            selection.get("resolved_case_count"),
            task_spec.get("case_count"),
            len(frozen_case_ids),
        )
        incident_count = _nonnegative_count(
            selection.get("resolved_incident_count"),
            task_spec.get("incident_count"),
            len(_legacy_incident_ids(frozen_case_ids)),
        )
        return {
            "mode": mode,
            "case_count": case_count,
            "incident_count": incident_count,
            "legacy": False,
        }

    return {
        "mode": "CASES",
        "case_count": _nonnegative_count(
            task_spec.get("case_count"),
            len(frozen_case_ids),
        ),
        "incident_count": _nonnegative_count(
            task_spec.get("incident_count"),
            len(_legacy_incident_ids(frozen_case_ids)),
        ),
        "legacy": True,
    }


def _nonnegative_count(*values: object) -> int:
    for value in values:
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            return value
    return 0


def _legacy_incident_ids(case_ids: tuple[object, ...]) -> tuple[str, ...]:
    incident_ids: list[str] = []
    seen: set[str] = set()
    for case_id in case_ids:
        if not isinstance(case_id, str) or ":" not in case_id:
            continue
        incident_id = case_id.split(":", 1)[0]
        if incident_id and incident_id not in seen:
            seen.add(incident_id)
            incident_ids.append(incident_id)
    return tuple(incident_ids)
