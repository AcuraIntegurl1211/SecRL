from __future__ import annotations

import csv
import hashlib
import json
import math
import random
import re
import shutil
import tempfile
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import (
    Attribution,
    FeatureRecord,
    InputError,
    OutputCollisionError,
    ReviewError,
    SCHEMA_VERSION,
)


TOOL_VERSION = "failure_analysis_reporting_v1"
REVIEW_FIELDS = [
    "incident",
    "question_index",
    "question_fingerprint_sha256",
    "candidate_primary",
    "candidate_secondary",
    "reviewed_primary",
    "reviewed_secondary",
    "review_status",
    "review_notes",
]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _reward_bucket(reward: float) -> str:
    if reward == 1:
        return "correct"
    if reward == 0:
        return "incorrect"
    return "partial"


def build_row(
    features: FeatureRecord,
    attribution: Attribution,
    taxonomy_version: str,
) -> dict[str, Any]:
    mapped = features.mapped
    identity = mapped.identity
    nodes = mapped.agent.get("nodes", mapped.question.get("nodes", []))
    if not isinstance(nodes, list):
        nodes = [nodes]

    return {
        "schema_version": SCHEMA_VERSION,
        "taxonomy_version": taxonomy_version,
        "incident": identity.incident,
        "question_index": identity.question_index,
        "question_fingerprint_sha256": identity.question_fingerprint_sha256,
        "question_text_fingerprint_sha256": (
            identity.question_text_fingerprint_sha256
        ),
        "nodes": list(nodes),
        "control_status": (
            "correct_control" if features.reward_official == 1 else "failure"
        ),
        "reward_official": features.reward_official,
        "reward_bucket": _reward_bucket(features.reward_official),
        "golden_answer": mapped.question.get("answer"),
        "submitted_answer": features.submitted_answer,
        "agent_source_index": mapped.agent_source_index,
        "env_source_index": mapped.env_source_index,
        "mapping_status": "complete",
        "log_complete": features.submitted,
        "sql_total": features.sql_total,
        "sql_success": features.sql_success,
        "sql_failure": features.sql_failure,
        "empty_result_count": features.empty_result_count,
        "duplicate_query_count": features.duplicate_query_count,
        "steps": features.steps,
        "max_steps": features.max_steps,
        "submitted": features.submitted,
        "submitted_at_step_limit": features.submitted_at_step_limit,
        "gold_evidence_match": features.gold_evidence_match,
        "gold_evidence_steps": list(features.gold_evidence_steps),
        "evaluator_fields_complete": features.evaluator_fields_complete,
        "agent_prompt_tokens": features.agent_prompt_tokens,
        "agent_completion_tokens": features.agent_completion_tokens,
        "agent_total_tokens": features.agent_total_tokens,
        "evaluator_tokens": features.evaluator_tokens,
        "primary_cause_candidate": attribution.primary_cause_candidate,
        "primary_cause_status": attribution.primary_cause_status,
        "secondary_cause_candidates": list(
            attribution.secondary_cause_candidates
        ),
        "confidence": attribution.confidence,
        "evidence": [item.as_dict() for item in features.evidence],
        "needs_human_review": attribution.needs_human_review,
        "human_review_reasons": list(attribution.human_review_reasons),
        "reviewed_primary": attribution.reviewed_primary,
        "reviewed_secondary": list(attribution.reviewed_secondary),
        "review_status": attribution.review_status,
        "review_notes": attribution.review_notes,
    }


def _identity(row: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(row.get("incident", "")),
        int(row.get("question_index", -1)),
        str(row.get("question_fingerprint_sha256", "")),
    )


def _integrity_anomaly(row: dict[str, Any]) -> bool:
    if row.get("mapping_status") not in (None, "complete"):
        return True
    if row.get("log_complete") is False:
        return True
    reasons = row.get("human_review_reasons", [])
    return isinstance(reasons, list) and any(
        "integrity" in str(reason).lower() for reason in reasons
    )


def select_review_rows(
    rows: list[dict[str, Any]],
    taxonomy: dict[str, Any],
) -> list[dict[str, Any]]:
    mandatory = set(taxonomy.get("always_human_review", []))
    sampling = taxonomy.get("review_sampling", {})
    if not isinstance(sampling, dict):
        sampling = {}
    seed = int(sampling.get("seed", 20260720))
    rate = float(
        sampling.get(
            "rate",
            sampling.get("fraction", sampling.get("sample_rate", 0.1)),
        )
    )
    minimum = int(
        sampling.get(
            "minimum_per_nonempty_category",
            sampling.get("minimum", 1),
        )
    )

    selected: dict[tuple[str, int, str], dict[str, Any]] = {}
    remaining: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        primary = row.get("primary_cause_candidate")
        forced = (
            primary in mandatory
            or row.get("confidence") == "low"
            or row.get("needs_human_review") is True
            or _integrity_anomaly(row)
        )
        if forced:
            selected[_identity(row)] = row
        elif isinstance(primary, str) and primary:
            remaining.setdefault(primary, []).append(row)

    generator = random.Random(seed)
    for primary in sorted(remaining):
        group = sorted(remaining[primary], key=_identity)
        shuffled = list(group)
        generator.shuffle(shuffled)
        count = max(minimum, math.ceil(len(group) * rate))
        for row in shuffled[:count]:
            selected[_identity(row)] = row

    return [selected[key] for key in sorted(selected)]


def _parse_secondary(value: str, field: str, categories: set[str]) -> list[str]:
    try:
        parsed = json.loads(value)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ReviewError(f"invalid review field {field}: malformed JSON") from exc
    if not isinstance(parsed, list) or not all(
        isinstance(item, str) for item in parsed
    ):
        raise ReviewError(f"invalid review field {field}: expected JSON string list")
    invalid = sorted(set(parsed) - categories)
    if invalid:
        raise ReviewError(
            f"invalid review field {field}: unknown categories {invalid}"
        )
    return parsed


def apply_human_review(
    rows: list[dict[str, Any]],
    review_path: Path,
    taxonomy: dict[str, Any],
) -> None:
    categories = set(taxonomy.get("categories", []))
    indexed = {_identity(row): row for row in rows}
    pending: list[tuple[dict[str, Any], str | None, list[str], str, str]] = []
    seen: set[tuple[str, int, str]] = set()

    try:
        handle = review_path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise ReviewError(f"cannot read review file {review_path}: {exc}") from exc

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames != REVIEW_FIELDS:
            raise ReviewError(
                f"invalid review header in {review_path}: expected {REVIEW_FIELDS}"
            )
        for source_index, record in enumerate(reader, 2):
            incident = record.get("incident", "")
            try:
                question_index = int(record.get("question_index", ""))
            except ValueError as exc:
                raise ReviewError(
                    f"invalid review row {source_index}: question_index"
                ) from exc
            fingerprint = record.get("question_fingerprint_sha256", "")
            if re.fullmatch(r"[0-9a-fA-F]{64}", fingerprint) is None:
                raise ReviewError(
                    f"invalid review row {source_index}: fingerprint"
                )
            identity = (incident, question_index, fingerprint)
            if identity in seen:
                raise ReviewError(
                    f"duplicate review identity at row {source_index}: {identity}"
                )
            seen.add(identity)
            row = indexed.get(identity)
            if row is None:
                raise ReviewError(
                    f"unknown review identity at row {source_index}: {identity}"
                )

            candidate_primary = record.get("candidate_primary", "") or None
            if candidate_primary != row.get("primary_cause_candidate"):
                raise ReviewError(
                    f"candidate primary mismatch at row {source_index}"
                )
            candidate_secondary = _parse_secondary(
                record.get("candidate_secondary", ""),
                "candidate_secondary",
                categories,
            )
            if candidate_secondary != row.get("secondary_cause_candidates", []):
                raise ReviewError(
                    f"candidate secondary mismatch at row {source_index}"
                )

            reviewed_primary = record.get("reviewed_primary", "") or None
            if reviewed_primary is not None and reviewed_primary not in categories:
                raise ReviewError(
                    f"invalid review field reviewed_primary at row {source_index}"
                )
            reviewed_secondary = _parse_secondary(
                record.get("reviewed_secondary", ""),
                "reviewed_secondary",
                categories,
            )
            pending.append(
                (
                    row,
                    reviewed_primary,
                    reviewed_secondary,
                    record.get("review_status", ""),
                    record.get("review_notes", ""),
                )
            )

    for row, primary, secondary, status, notes in pending:
        row["reviewed_primary"] = primary
        row["reviewed_secondary"] = secondary
        row["review_status"] = status
        row["review_notes"] = notes


def _json_text(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _csv_value(value: Any) -> Any:
    if isinstance(value, (list, dict)):
        return _json_text(value)
    return value


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(_json_text(row))
            handle.write("\n")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise InputError("cannot write empty attribution report")
    fields = list(rows[0])
    if any(list(row) != fields for row in rows):
        raise InputError("attribution rows have inconsistent fields")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({key: _csv_value(value) for key, value in row.items()})


def _review_record(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "incident": row["incident"],
        "question_index": row["question_index"],
        "question_fingerprint_sha256": row["question_fingerprint_sha256"],
        "candidate_primary": row["primary_cause_candidate"],
        "candidate_secondary": _json_text(row["secondary_cause_candidates"]),
        "reviewed_primary": row["reviewed_primary"],
        "reviewed_secondary": _json_text(row["reviewed_secondary"]),
        "review_status": row["review_status"],
        "review_notes": row["review_notes"],
    }


def _write_review_csv(
    path: Path,
    rows: list[dict[str, Any]],
    taxonomy: dict[str, Any],
) -> None:
    selected = select_review_rows(rows, taxonomy)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        for row in selected:
            writer.writerow(_review_record(row))


def _counter_lines(counter: Counter[Any]) -> list[str]:
    return [f"- `{key}`: {counter[key]}" for key in sorted(counter, key=str)]


def _write_summary(path: Path, rows: list[dict[str, Any]], incident: str) -> None:
    rewards = Counter(str(row["reward_official"]) for row in rows)
    primary = Counter(
        row["primary_cause_candidate"] or "NONE" for row in rows
    )
    reviews = Counter(row["review_status"] for row in rows)
    mapped = sum(row["mapping_status"] == "complete" for row in rows)
    sql_success = sum(int(row["sql_success"]) for row in rows)
    sql_failure = sum(int(row["sql_failure"]) for row in rows)
    lines = [
        f"# {incident} Failure Attribution Summary",
        "",
        f"- Record count: {len(rows)}",
        f"- Mapping count: {mapped}",
        f"- SQL success total: {sql_success}",
        f"- SQL failure total: {sql_failure}",
        "",
        "## Official reward distribution",
        "",
        *_counter_lines(rewards),
        "",
        "## Candidate primary counts",
        "",
        *_counter_lines(primary),
        "",
        "## Review-status counts",
        "",
        *_counter_lines(reviews),
        "",
        "## Scoring warning",
        "",
        "Candidate attributions are analytical labels and do not alter official scoring.",
        "",
    ]
    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _validate_written_rows(jsonl_path: Path, csv_path: Path) -> None:
    with jsonl_path.open("r", encoding="utf-8") as handle:
        jsonl_rows = [json.loads(line) for line in handle if line.strip()]
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    if len(jsonl_rows) != len(csv_rows):
        raise InputError("report validation failed: JSONL/CSV record counts differ")
    jsonl_reward = sum(float(row["reward_official"]) for row in jsonl_rows)
    csv_reward = sum(float(row["reward_official"]) for row in csv_rows)
    if jsonl_reward != csv_reward:
        raise InputError("report validation failed: JSONL/CSV reward totals differ")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def write_outputs(
    rows: list[dict[str, Any]],
    taxonomy_path: Path,
    incident: str,
    output_dir: Path,
    source_paths: dict[str, Path],
    max_steps: int,
    git_commit: str | None,
    review_applied: bool,
) -> list[Path]:
    if output_dir.exists():
        raise OutputCollisionError(f"output path already exists: {output_dir}")
    parent = output_dir.parent
    if not parent.is_dir():
        raise InputError(f"output parent is not a directory: {parent}")

    try:
        taxonomy = json.loads(taxonomy_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"invalid taxonomy {taxonomy_path}: {exc}") from exc
    if not isinstance(taxonomy, dict):
        raise InputError(f"invalid taxonomy {taxonomy_path}: expected object")
    taxonomy_version = taxonomy.get("taxonomy_version")
    if taxonomy_version != "taxonomy_v1":
        raise InputError(f"invalid taxonomy {taxonomy_path}: expected taxonomy_v1")

    temp_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=parent)
    )
    moved = False
    try:
        taxonomy_output = temp_dir / "taxonomy_v1.json"
        jsonl_output = temp_dir / f"{incident}_attribution.jsonl"
        csv_output = temp_dir / f"{incident}_attribution.csv"
        markdown_output = temp_dir / f"{incident}_summary.md"
        review_output = temp_dir / "human_review.csv"
        manifest_output = temp_dir / f"{incident}_analysis_manifest.json"

        _write_json(taxonomy_output, taxonomy)
        _write_jsonl(jsonl_output, rows)
        _write_csv(csv_output, rows)
        _write_summary(markdown_output, rows, incident)
        _write_review_csv(review_output, rows, taxonomy)
        _validate_written_rows(jsonl_output, csv_output)

        source_manifest: dict[str, dict[str, str]] = {}
        for name in sorted(source_paths):
            source_path = source_paths[name]
            source_manifest[name] = {
                "path": str(source_path),
                "sha256": _sha256_file(source_path),
            }

        output_files = {
            "taxonomy": taxonomy_output,
            "jsonl": jsonl_output,
            "csv": csv_output,
            "markdown": markdown_output,
            "review_csv": review_output,
        }
        output_manifest = {
            path.name: {
                "filename": path.name,
                "sha256": _sha256_file(path),
            }
            for path in sorted(output_files.values(), key=lambda item: item.name)
        }
        mapping_count = sum(
            row.get("mapping_status") == "complete" for row in rows
        )
        manifest = {
            "schema_version": SCHEMA_VERSION,
            "taxonomy_version": taxonomy_version,
            "incident": incident,
            "max_steps": max_steps,
            "generated_at_utc": datetime.now(timezone.utc)
            .isoformat()
            .replace("+00:00", "Z"),
            "tool_version": TOOL_VERSION,
            "git_commit": git_commit,
            "review_applied": review_applied,
            "record_count": len(rows),
            "mapping_counts": {
                "agent": mapping_count,
                "env": mapping_count,
                "question": mapping_count,
            },
            "sources": source_manifest,
            "outputs": output_manifest,
        }
        _write_json(manifest_output, manifest)

        if output_dir.exists():
            raise OutputCollisionError(
                f"output path appeared during generation: {output_dir}"
            )
        temp_dir.rename(output_dir)
        moved = True
        return [
            output_dir / name
            for name in sorted(
                [
                    taxonomy_output.name,
                    jsonl_output.name,
                    csv_output.name,
                    markdown_output.name,
                    review_output.name,
                    manifest_output.name,
                ]
            )
        ]
    finally:
        if not moved and temp_dir.exists():
            shutil.rmtree(temp_dir)
