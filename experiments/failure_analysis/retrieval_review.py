"""Validate and apply completed SQL-retrieval review overlays."""

from __future__ import annotations

import csv
import json
import math
import re
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any

from .models import ReviewError
from .retrieval_models import (
    QueryStep,
    RetrievalDecision,
    RetrievalEvidenceBundle,
    thaw_json_value,
)


TAXONOMY_VERSION = "sql_retrieval_taxonomy_v1"
OVERLAY_SCHEMA_VERSION = "sql_retrieval_subtyping_v1"

_TAXONOMY_FIELDS = (
    "primary_subtypes",
    "auxiliary_tags",
    "outcomes",
    "boundary_flags",
    "confidence",
    "decision_statuses",
)
_FROZEN_TAXONOMY = {
    "primary_subtypes": (
        "SOURCE_SELECTION",
        "ENTITY_RESOLUTION",
        "TEMPORAL_SCOPE",
        "PREDICATE_FILTER",
        "RELATIONAL_PATH",
        "PROJECTION",
        "AGGREGATION_RANKING",
        "SEARCH_COVERAGE",
        "RESULT_SELECTION",
        "INDETERMINATE",
    ),
    "auxiliary_tags": (
        "EMPTY_RESULT",
        "PARTIAL_EVIDENCE",
        "NOISY_RESULT",
        "WRONG_TABLE",
        "WRONG_COLUMN",
        "WRONG_ENTITY",
        "WRONG_TIME",
        "OVER_FILTER",
        "UNDER_FILTER",
        "MISSING_JOIN",
        "WRONG_JOIN",
        "MISSING_ORDER",
        "WRONG_ORDER",
        "WRONG_LIMIT",
        "REPEATED_QUERY",
        "NO_ADAPTATION",
        "STEP_LIMIT",
        "SQL_ERROR_PRESENT",
        "GOLD_IN_RESULT",
        "GOLD_NOT_IN_RESULT",
    ),
    "outcomes": ("EMPTY", "PARTIAL", "NOISY", "WRONG_ROW", "MIXED", "UNOBSERVED"),
    "boundary_flags": ("NONE", "SQL_EXEC_POSSIBLE", "REASONING_POSSIBLE", "DATA_GOLD_POSSIBLE"),
    "confidence": ("high", "medium", "low", "indeterminate"),
    "decision_statuses": ("reviewed", "needs_review"),
}
_EVIDENCE_FIELDS = tuple(field.name for field in fields(RetrievalEvidenceBundle))
_DECISION_FIELDS = tuple(field.name for field in fields(RetrievalDecision))
_REVIEW_FIELDS = _EVIDENCE_FIELDS + _DECISION_FIELDS
_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_INCIDENT_RE = re.compile(r"incident_([0-9]+)\Z")
_CANONICAL_NONNEGATIVE_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")


def _error(message: str) -> ReviewError:
    return ReviewError(message)


def _validate_taxonomy(value: object) -> dict[str, object]:
    if type(value) is not dict:
        raise _error("overlay taxonomy must be a JSON object")
    if value.get("version") != TAXONOMY_VERSION:
        raise _error("overlay taxonomy has an unsupported version")
    for field in _TAXONOMY_FIELDS:
        items = value.get(field)
        if type(items) is not list or not items:
            raise _error(f"overlay taxonomy field {field} must be a non-empty list")
        if any(type(item) is not str or not item for item in items):
            raise _error(f"overlay taxonomy field {field} must contain strings")
        if len(set(items)) != len(items):
            raise _error(f"overlay taxonomy field {field} contains duplicates")
        if items != list(_FROZEN_TAXONOMY[field]):
            raise _error(f"overlay taxonomy field {field} does not match frozen v1")
    return value


def load_overlay_taxonomy(path: Path) -> dict[str, object]:
    """Load and structurally validate the fixed SQL retrieval taxonomy."""
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise _error(f"cannot load overlay taxonomy {path}: {exc}") from exc

    return _validate_taxonomy(value)


def _incident_number(value: object) -> int:
    if type(value) is not str:
        raise _error("incident must be an exact string")
    match = _INCIDENT_RE.fullmatch(value)
    if match is None:
        raise _error(f"invalid incident identity: {value!r}")
    try:
        return int(match.group(1))
    except (ValueError, OverflowError) as exc:
        raise _error("incident identity is too large") from exc


def _parse_nonnegative_int(cell: str, field: str) -> int:
    if type(cell) is not str or _CANONICAL_NONNEGATIVE_RE.fullmatch(cell) is None:
        raise _error(f"{field} must be a canonical non-negative integer")
    try:
        return int(cell)
    except (ValueError, OverflowError) as exc:
        raise _error(f"{field} is too large") from exc


def _parse_positive_int(cell: object, field: str) -> int:
    if type(cell) is not int or cell <= 0:
        raise _error(f"{field} must be a canonical positive integer")
    return cell


def _validate_hash(value: object, field: str) -> None:
    if type(value) is not str or _FINGERPRINT_RE.fullmatch(value) is None:
        raise _error(f"invalid {field}")


def _strict_equal(left: object, right: object) -> bool:
    """Compare thawed JSON values without Python's bool/int equality trap."""
    if type(left) is not type(right):
        return False
    if isinstance(left, dict):
        if set(left) != set(right):  # type: ignore[arg-type]
            return False
        return all(_strict_equal(left[key], right[key]) for key in left)  # type: ignore[index]
    if isinstance(left, list):
        return len(left) == len(right) and all(  # type: ignore[arg-type]
            _strict_equal(item_left, item_right)
            for item_left, item_right in zip(left, right)  # type: ignore[arg-type]
        )
    if isinstance(left, float) and not math.isfinite(left):
        return False
    return left == right


def _validate_json_value(value: object, field: str) -> None:
    if value is None or type(value) in (str, bool, int):
        return
    if type(value) is float:
        if not math.isfinite(value):
            raise _error(f"{field} contains a non-finite float")
        return
    if type(value) is dict:
        for key, item in value.items():
            if type(key) is not str:
                raise _error(f"{field} contains a non-string object key")
            _validate_json_value(item, field)
        return
    if type(value) is list:
        for item in value:
            _validate_json_value(item, field)
        return
    raise _error(f"{field} is not a JSON value")


def _validate_bundle(bundle: RetrievalEvidenceBundle) -> None:
    if type(bundle) is not RetrievalEvidenceBundle:
        raise _error("bundles must contain RetrievalEvidenceBundle values")
    _incident_number(bundle.incident)
    for field in ("question_index", "agent_source_index", "env_source_index"):
        value = getattr(bundle, field)
        if type(value) is not int or value < 0:
            raise _error(f"invalid bundle {field}")
    if type(bundle.trajectory_steps) is not int or bundle.trajectory_steps < 0:
        raise _error("invalid bundle trajectory_steps")
    for field in ("submitted", "submitted_at_step_limit"):
        if type(getattr(bundle, field)) is not bool:
            raise _error(f"invalid bundle {field}")
    if bundle.submitted and bundle.trajectory_steps <= 0:
        raise _error("invalid bundle submitted contract")
    if bundle.submitted_at_step_limit and (
        not bundle.submitted or bundle.trajectory_steps <= 0
    ):
        raise _error("invalid bundle submitted_at_step_limit contract")
    for field in (
        "question_fingerprint_sha256",
        "question_text_fingerprint_sha256",
        "agent_source_sha256",
        "env_source_sha256",
        "question_source_sha256",
    ):
        _validate_hash(getattr(bundle, field), field)
    for field in (
        "question",
        "context",
        "submitted_answer",
        "reviewed_primary_original",
        "review_notes_original",
    ):
        if type(getattr(bundle, field)) is not str:
            raise _error(f"invalid bundle {field}")
    if type(bundle.reward_official) is not float or not math.isfinite(
        bundle.reward_official
    ):
        raise _error("invalid bundle reward_official")
    try:
        golden_answer = thaw_json_value(bundle.golden_answer)
        golden_solution = thaw_json_value(bundle.golden_solution)
    except TypeError as exc:
        raise _error("invalid nested golden JSON value") from exc
    _validate_json_value(golden_answer, "golden_answer")
    _validate_json_value(golden_solution, "golden_solution")
    if type(bundle.query_steps) is not tuple:
        raise _error("invalid bundle query_steps")
    previous_step = 0
    for query_step in bundle.query_steps:
        if type(query_step) is not QueryStep:
            raise _error("invalid bundle query_steps item")
        if type(query_step.step) is not int or query_step.step <= previous_step:
            raise _error("invalid bundle query_steps step")
        if query_step.step > bundle.trajectory_steps:
            raise _error("invalid bundle query_steps range")
        if type(query_step.sql) is not str or type(query_step.observation) is not str:
            raise _error("invalid bundle query_steps text")
        if query_step.query_success is not None and type(query_step.query_success) is not bool:
            raise _error("invalid bundle query_steps query_success")
        previous_step = query_step.step


def _parse_json_cell(cell: str, expected: object, field: str) -> object:
    if cell == "" and expected is None:
        return None
    if cell == "":
        raise _error(f"{field} cannot be empty")
    try:
        parsed = json.loads(cell)
    except (TypeError, json.JSONDecodeError) as exc:
        raise _error(f"invalid JSON in {field}") from exc
    if parsed is None and expected is not None:
        raise _error(f"{field} cannot be null")
    if type(expected) is list:
        expected_thawed = [_thaw_for_compare(item) for item in expected]
    else:
        expected_thawed = _thaw_for_compare(expected)
    if not _strict_equal(parsed, expected_thawed):
        raise _error(f"immutable bundle field drift: {field}")
    return parsed


def _thaw_for_compare(value: object) -> object:
    """Thaw frozen model values while accepting already-serialized lists."""
    if type(value) is list:
        return [_thaw_for_compare(item) for item in value]
    if type(value) is tuple:
        return [_thaw_for_compare(item) for item in value]
    try:
        return thaw_json_value(value)
    except TypeError:
        return value


def _parse_immutable_row(row: dict[str, str], bundle: RetrievalEvidenceBundle) -> None:
    for field in _EVIDENCE_FIELDS:
        cell = row.get(field)
        if cell is None:
            raise _error(f"missing review cell {field}")
        expected = getattr(bundle, field)
        if field in ("golden_answer", "golden_solution"):
            _parse_json_cell(cell, expected, field)
        elif field == "query_steps":
            parsed = _parse_json_cell(
                cell,
                [asdict(query_step) for query_step in bundle.query_steps],
                field,
            )
            if type(parsed) is not list:
                raise _error("query_steps must be a JSON list")
        elif field in ("question_index", "agent_source_index", "env_source_index", "trajectory_steps"):
            parsed = _parse_nonnegative_int(cell, field)
            if type(expected) is not int or parsed != expected:
                raise _error(f"immutable bundle field drift: {field}")
        elif field in ("submitted", "submitted_at_step_limit"):
            if cell not in ("True", "False"):
                raise _error(f"{field} must be exactly True or False")
            parsed = cell == "True"
            if type(expected) is not bool or parsed != expected:
                raise _error(f"immutable bundle field drift: {field}")
        elif field == "reward_official":
            try:
                parsed = float(cell)
            except (TypeError, ValueError) as exc:
                raise _error("reward_official must be a finite float") from exc
            if not math.isfinite(parsed) or type(expected) is not float or parsed != expected:
                raise _error("immutable bundle field drift: reward_official")
        else:
            if type(expected) is not str or cell != expected:
                raise _error(f"immutable bundle field drift: {field}")


def _parse_decision(row: dict[str, str], taxonomy: dict[str, object], bundle: RetrievalEvidenceBundle) -> RetrievalDecision:
    allowed = {field: set(taxonomy[field]) for field in _TAXONOMY_FIELDS}  # type: ignore[arg-type]
    primary = row["retrieval_primary_subtype"]
    if type(primary) is not str or primary not in allowed["primary_subtypes"]:
        raise _error("unknown retrieval_primary_subtype")

    try:
        auxiliary = json.loads(row["auxiliary_tags"])
    except json.JSONDecodeError as exc:
        raise _error("auxiliary_tags must be a JSON list") from exc
    if type(auxiliary) is not list or any(type(item) is not str for item in auxiliary):
        raise _error("auxiliary_tags must be a JSON list of strings")
    if len(set(auxiliary)) != len(auxiliary):
        raise _error("auxiliary_tags must not contain duplicates")
    if any(item not in allowed["auxiliary_tags"] for item in auxiliary):
        raise _error("unknown auxiliary tag")

    outcome = row["retrieval_outcome"]
    boundary = row["boundary_flag"]
    confidence = row["confidence"]
    status = row["decision_status"]
    if outcome not in allowed["outcomes"]:
        raise _error("unknown retrieval_outcome")
    if boundary not in allowed["boundary_flags"]:
        raise _error("unknown boundary_flag")
    if confidence not in allowed["confidence"]:
        raise _error("unknown confidence")
    if status not in allowed["decision_statuses"]:
        raise _error("unknown decision_status")
    if confidence == "indeterminate" and primary != "INDETERMINATE":
        raise _error("indeterminate confidence requires INDETERMINATE primary")
    if primary == "INDETERMINATE" and confidence not in {"low", "medium", "indeterminate"}:
        raise _error("INDETERMINATE primary cannot have high confidence")

    first_cell = row["first_divergence_step"]
    first = None if first_cell == "" else _parse_nonnegative_int(first_cell, "first_divergence_step")
    try:
        relevant = json.loads(row["relevant_sql_steps"])
    except json.JSONDecodeError as exc:
        raise _error("relevant_sql_steps must be a JSON list") from exc
    if type(relevant) is not list:
        raise _error("relevant_sql_steps must be a JSON list")
    parsed_relevant = tuple(_parse_positive_int(item, "relevant_sql_steps") for item in relevant)
    if len(set(parsed_relevant)) != len(parsed_relevant):
        raise _error("relevant_sql_steps must not contain duplicates")

    sql_evidence = row["sql_evidence"]
    observation_evidence = row["observation_evidence"]
    gold_basis = row["gold_evidence_basis"]
    rationale = row["rationale"]
    for field, value in (
        ("sql_evidence", sql_evidence),
        ("observation_evidence", observation_evidence),
        ("gold_evidence_basis", gold_basis),
        ("rationale", rationale),
    ):
        if type(value) is not str:
            raise _error(f"{field} must be an exact string")

    query_steps = {query_step.step for query_step in bundle.query_steps}
    if first is not None and first not in query_steps:
        raise _error("first_divergence_step is absent from bundle.query_steps")
    if any(step not in query_steps for step in parsed_relevant):
        raise _error("relevant_sql_steps contains an absent query step")
    if primary != "INDETERMINATE":
        if first is None or not parsed_relevant:
            raise _error("non-INDETERMINATE decisions require source steps")
        if not sql_evidence and not observation_evidence:
            raise _error("non-INDETERMINATE decisions require relevant evidence")
    elif not rationale:
        raise _error("INDETERMINATE decisions require a rationale")
    if status == "reviewed" and primary != "INDETERMINATE" and not (
        sql_evidence or observation_evidence
    ):
        raise _error("reviewed decisions require relevant evidence")

    return RetrievalDecision(
        retrieval_primary_subtype=primary,
        auxiliary_tags=tuple(sorted(auxiliary)),
        retrieval_outcome=outcome,
        boundary_flag=boundary,
        confidence=confidence,
        decision_status=status,
        first_divergence_step=first,
        relevant_sql_steps=tuple(sorted(parsed_relevant)),
        sql_evidence=sql_evidence,
        observation_evidence=observation_evidence,
        gold_evidence_basis=gold_basis,
        rationale=rationale,
    )


def _serialize_bundle(bundle: RetrievalEvidenceBundle) -> dict[str, object]:
    row: dict[str, object] = {}
    for field in _EVIDENCE_FIELDS:
        value = getattr(bundle, field)
        if field in ("golden_answer", "golden_solution"):
            value = thaw_json_value(value)
        elif field == "query_steps":
            value = [asdict(query_step) for query_step in bundle.query_steps]
        row[field] = value
    return row


def apply_completed_review(
    bundles: list[RetrievalEvidenceBundle],
    review_path: Path,
    taxonomy: dict[str, object],
) -> list[dict[str, object]]:
    """Validate an exact review CSV and overlay only its decision fields."""
    # Validate caller-provided taxonomies as well as those loaded from disk.
    taxonomy = _validate_taxonomy(taxonomy)

    if type(bundles) is not list:
        raise _error("bundles must be a list")
    expected: dict[tuple[str, int, str], RetrievalEvidenceBundle] = {}
    for bundle in bundles:
        _validate_bundle(bundle)
        identity = (bundle.incident, bundle.question_index, bundle.question_fingerprint_sha256)
        if identity in expected:
            raise _error(f"duplicate bundle identity: {identity}")
        expected[identity] = bundle

    try:
        with review_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.reader(handle)
            header = next(reader, None)
            if header is None or header != list(_REVIEW_FIELDS):
                raise _error("review CSV header must exactly match bundle and decision fields")
            if len(set(header)) != len(header):
                raise _error("review CSV header contains duplicate columns")
            rows = []
            for row_number, values in enumerate(reader, 2):
                if len(values) != len(header):
                    raise _error(f"invalid review CSV row {row_number}")
                rows.append(dict(zip(header, values)))
    except (OSError, UnicodeError, csv.Error) as exc:
        raise _error(f"cannot read review CSV {review_path}: {exc}") from exc

    seen: set[tuple[str, int, str]] = set()
    parsed: list[tuple[RetrievalEvidenceBundle, RetrievalDecision]] = []
    for row_number, row in enumerate(rows, 2):
        incident = row["incident"]
        _incident_number(incident)
        question_index = _parse_nonnegative_int(row["question_index"], "question_index")
        fingerprint = row["question_fingerprint_sha256"]
        if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise _error(f"invalid question_fingerprint_sha256 at row {row_number}")
        identity = (incident, question_index, fingerprint)
        bundle = expected.get(identity)
        if bundle is None:
            raise _error(f"unknown review identity at row {row_number}: {identity}")
        if identity in seen:
            raise _error(f"duplicate review identity at row {row_number}: {identity}")
        seen.add(identity)
        _parse_immutable_row(row, bundle)
        decision = _parse_decision(row, taxonomy, bundle)
        parsed.append((bundle, decision))

    if seen != set(expected):
        missing = sorted(set(expected) - seen, key=lambda item: (_incident_number(item[0]), item[1], item[2]))
        raise _error(f"review CSV is missing bundle identities: {missing}")

    merged: list[dict[str, object]] = []
    for bundle, decision in sorted(
        parsed, key=lambda item: (_incident_number(item[0].incident), item[0].question_index)
    ):
        row = _serialize_bundle(bundle)
        row.update(decision.as_dict())
        row["schema_version"] = OVERLAY_SCHEMA_VERSION
        row["overlay_taxonomy_version"] = TAXONOMY_VERSION
        merged.append(row)
    return merged


_KNOWN_PRIMARY = {
    "SOURCE_SELECTION",
    "ENTITY_RESOLUTION",
    "TEMPORAL_SCOPE",
    "PREDICATE_FILTER",
    "RELATIONAL_PATH",
    "PROJECTION",
    "AGGREGATION_RANKING",
    "SEARCH_COVERAGE",
    "RESULT_SELECTION",
    "INDETERMINATE",
}
_KNOWN_BOUNDARY = {"NONE", "SQL_EXEC_POSSIBLE", "REASONING_POSSIBLE", "DATA_GOLD_POSSIBLE"}
_KNOWN_CONFIDENCE = {"high", "medium", "low", "indeterminate"}
_KNOWN_STATUS = {"reviewed", "needs_review"}


def select_low_confidence_rows(rows: list[dict[str, object]]) -> list[dict[str, object]]:
    """Select the human-review queue using deterministic identity and ordering."""
    if type(rows) is not list:
        raise _error("rows must be a list")
    selected: dict[tuple[str, int, str], dict[str, object]] = {}
    for row in rows:
        if type(row) is not dict:
            raise _error("rows must contain dictionaries")
        incident = row.get("incident")
        _incident_number(incident)
        question_index = row.get("question_index")
        if type(question_index) is not int or question_index < 0:
            raise _error("invalid row question_index")
        fingerprint = row.get("question_fingerprint_sha256")
        if type(fingerprint) is not str or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
            raise _error("invalid row question_fingerprint_sha256")
        primary = row.get("retrieval_primary_subtype")
        confidence = row.get("confidence")
        status = row.get("decision_status")
        boundary = row.get("boundary_flag")
        if type(primary) is not str or primary not in _KNOWN_PRIMARY:
            raise _error("invalid row retrieval_primary_subtype")
        if type(confidence) is not str or confidence not in _KNOWN_CONFIDENCE:
            raise _error("invalid row confidence")
        if type(status) is not str or status not in _KNOWN_STATUS:
            raise _error("invalid row decision_status")
        if type(boundary) is not str or boundary not in _KNOWN_BOUNDARY:
            raise _error("invalid row boundary_flag")
        if (
            confidence in {"low", "indeterminate"}
            or primary == "INDETERMINATE"
            or status == "needs_review"
            or boundary != "NONE"
        ):
            identity = (incident, question_index, fingerprint)
            selected.setdefault(identity, row)
    return [
        selected[identity]
        for identity in sorted(
            selected,
            key=lambda item: (_incident_number(item[0]), item[1], item[2]),
        )
    ]
