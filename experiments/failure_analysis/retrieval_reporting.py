"""Auditable, atomic reports for the SQL-retrieval subtype overlay."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import shutil
import stat
import tempfile
from collections import Counter
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .models import InputError, OutputCollisionError, ReviewError
from .retrieval_models import OVERLAY_SCHEMA_VERSION
from .retrieval_review import TAXONOMY_VERSION, select_low_confidence_rows


_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_INCIDENT_RE = re.compile(r"incident_([0-9]+)\Z")
_OUTPUT_NAMES = (
    "sql_retrieval_subtypes.csv",
    "sql_retrieval_subtypes.jsonl",
    "sql_retrieval_subtypes_summary.md",
    "low_confidence_review_queue.csv",
    "analysis_manifest.json",
)
_BUNDLE_FIELDS = (
    "incident",
    "question_index",
    "question_fingerprint_sha256",
    "question_text_fingerprint_sha256",
    "question",
    "context",
    "golden_answer",
    "golden_solution",
    "submitted_answer",
    "trajectory_steps",
    "submitted",
    "submitted_at_step_limit",
    "reward_official",
    "reviewed_primary_original",
    "review_notes_original",
    "agent_source_index",
    "env_source_index",
    "agent_source_sha256",
    "env_source_sha256",
    "question_source_sha256",
    "query_steps",
)
_DECISION_FIELDS = (
    "retrieval_primary_subtype",
    "auxiliary_tags",
    "retrieval_outcome",
    "boundary_flag",
    "confidence",
    "decision_status",
    "first_divergence_step",
    "relevant_sql_steps",
    "sql_evidence",
    "observation_evidence",
    "gold_evidence_basis",
    "rationale",
)
_ROW_FIELDS = (*_BUNDLE_FIELDS, *_DECISION_FIELDS, "schema_version", "overlay_taxonomy_version")
_INCIDENT_NUMBERS = (5, 38, 34, 39, 55, 134, 166, 322)
_INPUT_KEYS = frozenset(
    {
        "aggregate_csv",
        "completed_review_csv",
        "taxonomy",
        "evidence_jsonl",
        *(f"manifest_incident_{incident}" for incident in _INCIDENT_NUMBERS),
        *(
            f"{kind}_incident_{incident}"
            for incident in _INCIDENT_NUMBERS
            for kind in ("agent", "env", "question")
        ),
    }
)


def _input_error(message: str, exc: BaseException | None = None) -> InputError:
    error = InputError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _incident_number(value: object) -> int:
    if type(value) is not str:
        raise _input_error("incident must be an exact string")
    match = _INCIDENT_RE.fullmatch(value)
    if match is None:
        raise _input_error(f"invalid incident identity: {value!r}")
    try:
        return int(match.group(1))
    except (ValueError, OverflowError) as exc:
        raise _input_error("incident identity is too large", exc) from exc


def _identity(row: Mapping[str, object]) -> tuple[str, int, str]:
    incident = row.get("incident")
    number = _incident_number(incident)
    question_index = row.get("question_index")
    if type(question_index) is not int or question_index < 0:
        raise _input_error("question_index must be a non-negative exact int")
    fingerprint = row.get("question_fingerprint_sha256")
    if type(fingerprint) is not str or _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise _input_error("question_fingerprint_sha256 must be lower-case SHA-256")
    # Keeping the parsed incident number in this helper catches oversized
    # values while the returned identity remains the caller's exact value.
    _ = number
    return incident, question_index, fingerprint


def _sort_key(identity: tuple[str, int, str]) -> tuple[int, int, str]:
    return (_incident_number(identity[0]), identity[1], identity[2])


def _normalize_json(value: object, field: str = "value") -> object:
    """Validate a JSON value and thaw tuples/mappings without mutating input."""
    if value is None or type(value) in (str, bool, int):
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise _input_error(f"{field} contains a non-finite float")
        return value
    if isinstance(value, Mapping):
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise _input_error(f"{field} contains a non-string object key")
            result[key] = _normalize_json(item, field)
        return result
    if type(value) in (list, tuple):
        return [_normalize_json(item, field) for item in value]
    raise _input_error(f"{field} is not a JSON value")


def _strict_equal(left: object, right: object) -> bool:
    """Deep JSON equality that does not equate bool with int."""
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
    return left == right


def _canonical_json(value: object) -> str:
    return json.dumps(
        _normalize_json(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_bundle_payload(row: dict[str, object], position: int, label: str) -> None:
    prefix = f"{label} row {position}"
    for field in (
        "question_text_fingerprint_sha256",
        "agent_source_sha256",
        "env_source_sha256",
        "question_source_sha256",
    ):
        value = row[field]
        if type(value) is not str or _FINGERPRINT_RE.fullmatch(value) is None:
            raise _input_error(f"{prefix} {field} must be lower-case SHA-256")
    for field in (
        "question",
        "context",
        "submitted_answer",
        "reviewed_primary_original",
        "review_notes_original",
    ):
        if type(row[field]) is not str:
            raise _input_error(f"{prefix} {field} must be an exact string")
    for field in ("question_index", "agent_source_index", "env_source_index", "trajectory_steps"):
        value = row[field]
        if type(value) is not int or value < 0:
            raise _input_error(f"{prefix} {field} must be a non-negative exact int")
    for field in ("submitted", "submitted_at_step_limit"):
        if type(row[field]) is not bool:
            raise _input_error(f"{prefix} {field} must be an exact bool")
    if row["submitted"] and row["trajectory_steps"] <= 0:
        raise _input_error(f"{prefix} submitted requires trajectory_steps>0")
    if row["submitted_at_step_limit"] and (
        not row["submitted"] or row["trajectory_steps"] <= 0
    ):
        raise _input_error(f"{prefix} submitted_at_step_limit requires submitted and steps")
    reward = row["reward_official"]
    if type(reward) is not float or not math.isfinite(reward):
        raise _input_error(f"{prefix} reward_official must be a finite float")
    _normalize_json(row["golden_answer"], f"{prefix} golden_answer")
    _normalize_json(row["golden_solution"], f"{prefix} golden_solution")
    query_steps = row["query_steps"]
    if type(query_steps) is not list:
        raise _input_error(f"{prefix} query_steps must be a list")
    previous_step = 0
    for step_position, query_step in enumerate(query_steps):
        if type(query_step) is not dict or set(query_step) != {
            "step",
            "sql",
            "observation",
            "query_success",
        }:
            raise _input_error(f"{prefix} query_steps[{step_position}] has an invalid schema")
        step = query_step["step"]
        if type(step) is not int or step <= previous_step or step > row["trajectory_steps"]:
            raise _input_error(f"{prefix} query_steps step is not positive, increasing, and in range")
        if type(query_step["sql"]) is not str or type(query_step["observation"]) is not str:
            raise _input_error(f"{prefix} query_steps text fields must be exact strings")
        success = query_step["query_success"]
        if success is not None and type(success) is not bool:
            raise _input_error(f"{prefix} query_steps query_success must be None or bool")
        previous_step = step
        _normalize_json(query_step, f"{prefix} query_steps[{step_position}]")


def _validate_row_list(
    rows: object,
    label: str,
    *,
    require_nonempty: bool,
) -> tuple[list[dict[str, object]], tuple[str, ...], list[tuple[str, int, str]]]:
    if type(rows) is not list:
        raise _input_error(f"{label} must be a list")
    if require_nonempty and not rows:
        raise _input_error(f"{label} must be nonempty")
    if not rows:
        return [], (), []

    validated: list[dict[str, object]] = []
    identities: list[tuple[str, int, str]] = []
    seen_identities: set[tuple[str, int, str]] = set()
    seen_question_keys: set[tuple[str, int]] = set()
    for position, row in enumerate(rows):
        if type(row) is not dict:
            raise _input_error(f"{label} row {position} must be a dictionary")
        if any(type(key) is not str for key in row):
            raise _input_error(f"{label} row {position} has a non-string field")
        if tuple(row) != _ROW_FIELDS:
            raise _input_error(
                f"{label} row {position} fields must exactly match frozen schema"
            )
        if row.get("schema_version") != OVERLAY_SCHEMA_VERSION:
            raise _input_error(f"{label} row {position} has an invalid schema_version")
        if row.get("overlay_taxonomy_version") != TAXONOMY_VERSION:
            raise _input_error(
                f"{label} row {position} has an invalid overlay_taxonomy_version"
            )
        identity = _identity(row)
        question_key = (identity[0], identity[1])
        if identity in seen_identities:
            raise _input_error(f"duplicate {label} identity: {identity}")
        if question_key in seen_question_keys:
            raise _input_error(f"duplicate {label} incident/question_index: {question_key}")
        seen_identities.add(identity)
        seen_question_keys.add(question_key)
        _validate_bundle_payload(row, position, label)
        for field, value in row.items():
            _normalize_json(value, f"{label} row {position} field {field}")
        identities.append(identity)
        validated.append(row)

    # This is the frozen decision validator used by the review selector.  It
    # also enforces taxonomy membership and decision cross-field invariants.
    try:
        select_low_confidence_rows(validated)
    except ReviewError as exc:
        raise _input_error(f"invalid {label} decision fields: {exc}", exc) from exc
    return validated, _ROW_FIELDS, identities


def _validate_review_queue(
    rows: list[dict[str, object]],
    review_queue: object,
    row_fields: tuple[str, ...],
) -> list[dict[str, object]]:
    queue, _, _ = _validate_row_list(
        review_queue,
        "review_queue",
        require_nonempty=False,
    )
    if queue and tuple(queue[0]) != _ROW_FIELDS:
        raise _input_error("review_queue rows do not have the same schema as rows")
    if any(tuple(item) != _ROW_FIELDS for item in queue):
        raise _input_error("review_queue rows do not have the same schema as rows")
    try:
        expected = select_low_confidence_rows(rows)
    except ReviewError as exc:
        raise _input_error(f"cannot compute low-confidence queue: {exc}", exc) from exc
    if len(queue) != len(expected):
        raise _input_error("review_queue does not exactly match low-confidence policy")
    for index, (actual, wanted) in enumerate(zip(queue, expected)):
        actual_identity = _identity(actual)
        wanted_identity = _identity(wanted)
        if actual_identity != wanted_identity:
            raise _input_error(
                f"review_queue identity mismatch at position {index}: "
                f"{actual_identity!r} != {wanted_identity!r}"
            )
        actual_json = _normalize_json(actual, "review_queue row")
        wanted_json = _normalize_json(wanted, "rows row")
        if not _strict_equal(actual_json, wanted_json):
            raise _input_error(
                f"review_queue decision mismatch at position {index}"
            )
    return queue


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except (OSError, ValueError, TypeError) as exc:
        raise _input_error(f"cannot read input path {path}: {exc}", exc) from exc
    return digest.hexdigest()


def _validate_provenance(
    input_paths: object,
    input_hashes: object,
) -> dict[str, dict[str, str]]:
    if not isinstance(input_paths, dict) or not isinstance(input_hashes, dict):
        raise _input_error("input_paths and input_hashes must be dictionaries")
    if (
        set(input_paths) != _INPUT_KEYS
        or set(input_hashes) != _INPUT_KEYS
        or set(input_paths) != set(input_hashes)
    ):
        raise _input_error("input provenance keys must exactly match the frozen set")
    if any(type(key) is not str or not key for key in input_paths):
        raise _input_error("input provenance keys must be non-empty strings")
    manifest: dict[str, dict[str, str]] = {}
    for key in sorted(input_paths):
        path = input_paths[key]
        expected = input_hashes[key]
        if not isinstance(path, Path):
            raise _input_error(f"input path {key!r} must be a Path")
        try:
            canonical = path.resolve()
            is_regular = path.is_file() and stat.S_ISREG(path.stat().st_mode)
        except (OSError, ValueError, TypeError) as exc:
            raise _input_error(f"cannot inspect input path {path}: {exc}", exc) from exc
        if not path.is_absolute() or path != canonical:
            raise _input_error(f"input path {key!r} must be canonical absolute: {path}")
        if not is_regular:
            raise _input_error(f"input path {key!r} is not an existing regular file: {path}")
        if type(expected) is not str or _FINGERPRINT_RE.fullmatch(expected) is None:
            raise _input_error(f"input hash {key!r} must be lower-case SHA-256")
        actual = _sha256_file(path)
        if actual != expected:
            raise _input_error(f"input SHA-256 mismatch for {path}")
        manifest[key] = {"path": str(path), "sha256": actual}
    return manifest


def _csv_cell(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (list, tuple, Mapping)):
        return _canonical_json(value)
    return value


def _render_csv(rows: list[dict[str, object]], fields: tuple[str, ...]) -> str:
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(
        stream,
        fieldnames=list(fields),
        extrasaction="raise",
        lineterminator="\n",
    )
    writer.writeheader()
    for row in rows:
        writer.writerow({field: _csv_cell(row[field]) for field in fields})
    return stream.getvalue()


def _render_jsonl(rows: list[dict[str, object]]) -> str:
    return "".join(_canonical_json(row) + "\n" for row in rows)


def _label(value: object) -> str:
    if value is None:
        return "NONE"
    if type(value) in (str, int, float, bool):
        return str(value)
    return _canonical_json(value)


def _counter_lines(counter: Mapping[str, int]) -> list[str]:
    if not counter:
        return ["- `NONE`: 0"]
    return [f"- `{key}`: {counter[key]}" for key in sorted(counter)]


def _count_rows(rows: list[dict[str, object]], queue_count: int) -> dict[str, object]:
    incident_subtypes: dict[str, Counter[str]] = {}
    outcomes: Counter[str] = Counter()
    auxiliary: Counter[str] = Counter()
    confidence: Counter[str] = Counter()
    boundary: Counter[str] = Counter()
    rewards: Counter[str] = Counter()
    for row in rows:
        incident = str(row["incident"])
        incident_subtypes.setdefault(incident, Counter())[str(row["retrieval_primary_subtype"])] += 1
        outcomes[str(row["retrieval_outcome"])] += 1
        tags = row["auxiliary_tags"]
        for tag in tags:  # type: ignore[union-attr]
            auxiliary[str(tag)] += 1
        confidence[str(row["confidence"])] += 1
        boundary[str(row["boundary_flag"])] += 1
        rewards[_label(row["reward_official"])] += 1
    per_incident = {
        incident: {subtype: count for subtype, count in sorted(counter.items())}
        for incident, counter in sorted(
            incident_subtypes.items(), key=lambda item: _incident_number(item[0])
        )
    }
    return {
        "per_incident_subtype": per_incident,
        "retrieval_outcome": dict(sorted(outcomes.items())),
        "auxiliary_tag": dict(sorted(auxiliary.items())),
        "confidence": dict(sorted(confidence.items())),
        "boundary_flag": dict(sorted(boundary.items())),
        "official_reward": dict(sorted(rewards.items())),
        "low_confidence_review_queue": queue_count,
    }


def _render_summary(rows: list[dict[str, object]], queue_count: int) -> str:
    counts = _count_rows(rows, queue_count)
    lines = [
        "# SQL Retrieval Subtype Summary",
        "",
        f"Total records: {len(rows)}",
        "",
        "## Per-incident subtype counts",
        "",
        "| Incident | Subtype | Count |",
        "| --- | --- | ---: |",
    ]
    per_incident = counts["per_incident_subtype"]
    for incident, subtypes in per_incident.items():  # type: ignore[union-attr]
        for subtype, count in subtypes.items():  # type: ignore[union-attr]
            lines.append(f"| {incident} | {subtype} | {count} |")
    lines.extend(["", "## Retrieval outcome counts", ""])
    lines.extend(_counter_lines(counts["retrieval_outcome"]))  # type: ignore[arg-type]
    lines.extend(["", "## Auxiliary tag counts", ""])
    lines.extend(_counter_lines(counts["auxiliary_tag"]))  # type: ignore[arg-type]
    lines.extend(["", "## Confidence counts", ""])
    lines.extend(_counter_lines(counts["confidence"]))  # type: ignore[arg-type]
    lines.extend(["", "## Boundary flag counts", ""])
    lines.extend(_counter_lines(counts["boundary_flag"]))  # type: ignore[arg-type]
    lines.extend(["", "## Official reward distribution", ""])
    lines.extend(_counter_lines(counts["official_reward"]))  # type: ignore[arg-type]
    lines.extend([
        "",
        f"Low-confidence review queue count: {queue_count}",
        "",
        "Warning: warning overlay does not alter official scoring.",
        "",
    ])
    return "\n".join(lines)


def _parse_json_object(text: str) -> object:
    def reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"duplicate JSON object key: {key}")
            result[key] = value
        return result

    return json.loads(text, object_pairs_hook=reject_duplicates)


def _parse_csv_cell(cell: str, expected: object, field: str) -> object:
    normalized_expected = _normalize_json(expected, field)
    if expected is None:
        if cell != "":
            raise _input_error(f"CSV field {field} should be blank for null")
        return None
    if type(expected) is bool:
        parsed = {"True": True, "False": False}.get(cell, object())
    elif type(expected) is int:
        if re.fullmatch(r"-?(?:0|[1-9][0-9]*)\Z", cell) is None:
            raise _input_error(f"CSV field {field} is not an exact integer")
        try:
            parsed = int(cell)
        except (ValueError, OverflowError) as exc:
            raise _input_error(f"CSV field {field} is not an exact integer", exc) from exc
    elif type(expected) is float:
        try:
            parsed = float(cell)
        except (ValueError, TypeError) as exc:
            raise _input_error(f"CSV field {field} is not a float", exc) from exc
    elif type(expected) is str:
        parsed = cell
    else:
        try:
            parsed = _parse_json_object(cell)
        except (TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise _input_error(f"CSV field {field} is not valid JSON", exc) from exc
        parsed = _normalize_json(parsed, field)
    if not _strict_equal(parsed, normalized_expected):
        raise _input_error(f"CSV field {field} changed value")
    return parsed


def _validate_csv_file(
    path: Path,
    expected_rows: list[dict[str, object]],
    fields: tuple[str, ...],
) -> None:
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if reader.fieldnames != list(fields):
                raise _input_error("CSV header changed stable field order")
            parsed = list(reader)
    except InputError:
        raise
    except (OSError, UnicodeError, csv.Error, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _input_error(f"cannot reopen CSV output {path}: {exc}", exc) from exc
    if len(parsed) != len(expected_rows):
        raise _input_error(f"CSV record count mismatch for {path}")
    for position, (raw, expected) in enumerate(zip(parsed, expected_rows)):
        if set(raw) != set(fields):
            raise _input_error(f"CSV row {position} has an unexpected field")
        for field in fields:
            _parse_csv_cell(raw[field], expected[field], field)


def _validate_jsonl_file(path: Path, expected_rows: list[dict[str, object]]) -> None:
    parsed: list[object] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.endswith("\n"):
                    raise _input_error(f"JSONL line {line_number} lacks newline")
                if not line[:-1]:
                    raise _input_error(f"JSONL line {line_number} is blank")
                parsed.append(_parse_json_object(line[:-1]))
    except (OSError, UnicodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _input_error(f"cannot reopen JSONL output {path}: {exc}", exc) from exc
    if len(parsed) != len(expected_rows):
        raise _input_error(f"JSONL record count mismatch for {path}")
    for position, (actual, expected) in enumerate(zip(parsed, expected_rows)):
        if type(actual) is not dict:
            raise _input_error(f"JSONL row {position} is not an object")
        actual_normalized = _normalize_json(actual, f"JSONL row {position}")
        expected_normalized = _normalize_json(expected, f"row {position}")
        if not _strict_equal(actual_normalized, expected_normalized):
            raise _input_error(f"JSONL row {position} changed values")


def _write_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8", newline="\n")


def _path_lexists(path: Path) -> bool:
    try:
        return os.path.lexists(os.fspath(path))
    except (OSError, TypeError, ValueError) as exc:
        raise _input_error(f"cannot inspect output path {path}: {exc}", exc) from exc


def write_retrieval_outputs(
    rows: list[dict[str, object]],
    review_queue: list[dict[str, object]],
    output_dir: Path,
    input_paths: dict[str, Path],
    input_hashes: dict[str, str],
    git_commit: str | None,
) -> list[Path]:
    """Validate, render and atomically publish the five retrieval reports."""
    if not isinstance(output_dir, Path):
        raise _input_error("output_dir must be a Path")
    if _path_lexists(output_dir):
        raise OutputCollisionError(f"output path already exists: {output_dir}")
    if git_commit is not None and type(git_commit) is not str:
        raise _input_error("git_commit must be None or an exact string")
    parent = output_dir.parent
    try:
        if not parent.exists() or not parent.is_dir():
            raise _input_error(f"output parent is not an existing directory: {parent}")
    except OSError as exc:
        raise _input_error(f"cannot inspect output parent {parent}: {exc}", exc) from exc

    try:
        validated_rows, row_fields, _ = _validate_row_list(
            rows, "rows", require_nonempty=True
        )
        queue = _validate_review_queue(validated_rows, review_queue, row_fields)
        input_manifest = _validate_provenance(input_paths, input_hashes)
    except InputError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise _input_error(f"invalid retrieval inputs: {exc}", exc) from exc

    # Sorting is performed on a new list so callers' rows and nested values are
    # never mutated.  Stable field order comes from the first caller row.
    sorted_rows = sorted(validated_rows, key=lambda row: _sort_key(_identity(row)))
    sorted_queue = sorted(queue, key=lambda row: _sort_key(_identity(row)))
    try:
        payloads = {
            "sql_retrieval_subtypes.csv": _render_csv(sorted_rows, row_fields),
            "sql_retrieval_subtypes.jsonl": _render_jsonl(sorted_rows),
            "sql_retrieval_subtypes_summary.md": _render_summary(sorted_rows, len(sorted_queue)),
            "low_confidence_review_queue.csv": _render_csv(sorted_queue, row_fields),
        }
    except InputError:
        raise
    except (KeyError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
        raise _input_error(f"cannot render retrieval outputs: {exc}", exc) from exc

    temp_dir: Path | None = None
    moved = False
    reserved_target = False
    reservation_stat: os.stat_result | None = None
    try:
        try:
            temp_dir = Path(tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=parent))
        except OSError as exc:
            raise _input_error(f"cannot create temporary output directory: {exc}", exc) from exc

        staged_paths: dict[str, Path] = {}
        try:
            for name in _OUTPUT_NAMES[:-1]:
                staged_path = temp_dir / name
                _write_text(staged_path, payloads[name])
                staged_paths[name] = staged_path
        except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
            raise _input_error(f"cannot write staged output: {exc}", exc) from exc

        _validate_csv_file(staged_paths["sql_retrieval_subtypes.csv"], sorted_rows, row_fields)
        _validate_jsonl_file(staged_paths["sql_retrieval_subtypes.jsonl"], sorted_rows)
        _validate_csv_file(staged_paths["low_confidence_review_queue.csv"], sorted_queue, row_fields)

        try:
            output_hashes = {
                name: _sha256_file(staged_paths[name])
                for name in sorted(staged_paths)
            }
        except InputError:
            raise
        except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
            raise _input_error(f"cannot hash staged output: {exc}", exc) from exc
        manifest = {
            "schema_version": OVERLAY_SCHEMA_VERSION,
            "overlay_taxonomy_version": TAXONOMY_VERSION,
            "record_count": len(sorted_rows),
            "git_commit": git_commit,
            "input_manifest": input_manifest,
            "output_hashes": output_hashes,
            "output_count": len(_OUTPUT_NAMES),
            "counts": _count_rows(sorted_rows, len(sorted_queue)),
        }
        manifest_path = temp_dir / "analysis_manifest.json"
        try:
            manifest_text = (
                json.dumps(
                    manifest,
                    ensure_ascii=False,
                    sort_keys=True,
                    indent=2,
                    allow_nan=False,
                )
                + "\n"
            )
            _write_text(manifest_path, manifest_text)
        except (OSError, TypeError, ValueError, UnicodeError, OverflowError, RecursionError) as exc:
            raise _input_error(f"cannot serialize or write manifest: {exc}", exc) from exc
        try:
            with manifest_path.open("r", encoding="utf-8") as handle:
                reopened_manifest = json.load(handle)
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, OverflowError, RecursionError) as exc:
            raise _input_error(f"cannot reopen manifest: {exc}", exc) from exc
        if not _strict_equal(reopened_manifest, manifest):
            raise _input_error("manifest changed during serialization")

        # Reserve the destination immediately before publication.  Publish by
        # no-replace hard links through the reservation's directory fd; this
        # avoids directory-rename replacement races while keeping publication
        # limited to this call's own inode.
        try:
            output_dir.mkdir()
            reserved_target = True
            reservation_stat = output_dir.lstat()
        except FileExistsError as exc:
            raise OutputCollisionError(
                f"output path appeared during report generation: {output_dir}"
            ) from exc
        except OSError as exc:
            raise _input_error(f"cannot reserve output path: {exc}", exc) from exc
        try:
            directory_fd = os.open(
                os.fspath(output_dir),
                os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
            )
        except OSError as exc:
            raise _input_error(f"cannot open reserved output path: {exc}", exc) from exc
        try:
            for name in _OUTPUT_NAMES:
                if reservation_stat is None or not os.path.samestat(
                    reservation_stat, output_dir.lstat()
                ):
                    raise OutputCollisionError(
                        f"output path changed during publication: {output_dir}"
                    )
                source = staged_paths[name] if name != "analysis_manifest.json" else manifest_path
                try:
                    os.link(
                        os.fspath(source),
                        name,
                        dst_dir_fd=directory_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError as exc:
                    raise OutputCollisionError(
                        f"output path appeared during publication: {output_dir}"
                    ) from exc
                except OSError as exc:
                    try:
                        changed = reservation_stat is None or not os.path.samestat(
                            reservation_stat, output_dir.lstat()
                        )
                    except OSError:
                        changed = True
                    if changed:
                        raise OutputCollisionError(
                            f"output path changed during publication: {output_dir}"
                        ) from exc
                    raise _input_error(f"cannot publish output file {name}: {exc}", exc) from exc
                if not os.path.samestat(reservation_stat, output_dir.lstat()):
                    raise OutputCollisionError(
                        f"output path changed during publication: {output_dir}"
                    )
        finally:
            os.close(directory_fd)
        if reservation_stat is None or not os.path.samestat(
            reservation_stat, output_dir.lstat()
        ):
            raise OutputCollisionError(
                f"output path changed after publication: {output_dir}"
            )
        shutil.rmtree(temp_dir)
        moved = True
        return [output_dir / name for name in _OUTPUT_NAMES]
    finally:
        if not moved and temp_dir is not None and temp_dir.exists():
            shutil.rmtree(temp_dir)
        if not moved and reserved_target and reservation_stat is not None and output_dir.is_dir():
            try:
                # Remove only our reservation; the inode check protects a
                # user path that replaced it during cleanup.
                if os.path.samestat(reservation_stat, output_dir.stat()):
                    shutil.rmtree(output_dir)
            except OSError:
                pass


__all__ = ["write_retrieval_outputs"]
