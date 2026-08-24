from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from experiments.failure_analysis.models import (  # noqa: E402
    AnalysisError,
    InputError,
    OutputCollisionError,
)


EXPECTED_INCIDENTS = (
    "incident_5",
    "incident_38",
    "incident_34",
    "incident_39",
    "incident_55",
    "incident_134",
    "incident_166",
    "incident_322",
)

SCHEMA_VERSION = "failure_attribution_v1"
TAXONOMY_VERSION = "taxonomy_v1"
FINGERPRINT_PATTERN = re.compile(r"[0-9a-fA-F]{64}")
INCIDENT_PATTERN = re.compile(r"incident_(\d+)")


def _identity(row: dict[str, Any]) -> tuple[str, int, str]:
    return (
        row["incident"],
        row["question_index"],
        row["question_fingerprint_sha256"],
    )


def _validate_row(row: Any, path: Path, line_number: int) -> dict[str, Any]:
    location = f"{path} line {line_number}"
    if not isinstance(row, dict):
        raise InputError(f"invalid JSONL {location}: expected object")
    if row.get("schema_version") != SCHEMA_VERSION:
        raise InputError(
            f"invalid schema_version at {location}: expected {SCHEMA_VERSION}"
        )
    if row.get("taxonomy_version") != TAXONOMY_VERSION:
        raise InputError(
            f"invalid taxonomy_version at {location}: expected {TAXONOMY_VERSION}"
        )

    incident = row.get("incident")
    if not isinstance(incident, str) or INCIDENT_PATTERN.fullmatch(incident) is None:
        raise InputError(f"invalid incident at {location}: {incident!r}")
    if incident not in EXPECTED_INCIDENTS:
        raise InputError(f"unexpected incident at {location}: {incident}")

    question_index = row.get("question_index")
    if (
        not isinstance(question_index, int)
        or isinstance(question_index, bool)
        or question_index < 0
    ):
        raise InputError(
            f"invalid question_index at {location}: {question_index!r}"
        )

    fingerprint = row.get("question_fingerprint_sha256")
    if (
        not isinstance(fingerprint, str)
        or FINGERPRINT_PATTERN.fullmatch(fingerprint) is None
    ):
        raise InputError(
            f"invalid question_fingerprint_sha256 at {location}"
        )
    return row


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise InputError(f"input JSONL is not a readable file: {path}")
    rows: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, 1):
                if not line.strip():
                    continue
                try:
                    value = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise InputError(
                        f"invalid JSONL {path} line {line_number}: {exc.msg}"
                    ) from exc
                rows.append(_validate_row(value, path, line_number))
    except OSError as exc:
        raise InputError(f"cannot read input JSONL {path}: {exc}") from exc
    if not rows:
        raise InputError(f"input JSONL contains no records: {path}")
    return rows


def load_incident_rows(paths: list[Path]) -> list[dict[str, Any]]:
    if len(paths) != len(EXPECTED_INCIDENTS):
        raise InputError(
            "expected exactly eight input JSONL files; "
            "missing incident input or duplicate argument"
        )

    seen_incidents: set[str] = set()
    seen_identities: set[tuple[str, int, str]] = set()
    combined: list[dict[str, Any]] = []

    for path in paths:
        file_rows = _read_jsonl(path)
        file_incidents = {row["incident"] for row in file_rows}
        if len(file_incidents) != 1:
            raise InputError(
                f"input JSONL mixes incidents in one file: {path}"
            )
        incident = next(iter(file_incidents))
        if incident in seen_incidents:
            raise InputError(f"duplicate incident input: {incident}")
        seen_incidents.add(incident)

        for row in file_rows:
            identity = _identity(row)
            if identity in seen_identities:
                raise InputError(
                    f"duplicate stable identity in {path}: {identity}"
                )
            seen_identities.add(identity)
            combined.append(row)

    missing = sorted(
        set(EXPECTED_INCIDENTS) - seen_incidents,
        key=_incident_number,
    )
    if missing:
        raise InputError(f"missing incident inputs: {missing}")
    return combined


def _incident_number(value: str) -> int:
    match = INCIDENT_PATTERN.fullmatch(value)
    if match is None:
        raise InputError(f"invalid incident: {value}")
    return int(match.group(1))


def _row_sort_key(row: dict[str, Any]) -> tuple[int, int, str]:
    return (
        _incident_number(row["incident"]),
        row["question_index"],
        row["question_fingerprint_sha256"],
    )


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


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        raise InputError("cannot aggregate zero records")
    fields = sorted(rows[0])
    expected_fields = set(fields)
    for row in rows:
        if set(row) != expected_fields:
            raise InputError("incident JSONL records have inconsistent fields")
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {key: _csv_value(row[key]) for key in fields}
            )


def _counter_lines(counter: Counter[Any]) -> list[str]:
    if not counter:
        return ["- `NONE`: 0"]
    return [
        f"- `{key}`: {counter[key]}"
        for key in sorted(counter, key=str)
    ]


def _distribution(
    rows: list[dict[str, Any]],
    field: str,
    *,
    none_label: str = "NONE",
) -> Counter[Any]:
    return Counter(
        row.get(field) if row.get(field) is not None else none_label
        for row in rows
    )


def _write_summary(path: Path, rows: list[dict[str, Any]]) -> None:
    incidents = sorted(EXPECTED_INCIDENTS, key=_incident_number)
    grouped = {
        incident: [row for row in rows if row["incident"] == incident]
        for incident in incidents
    }
    lines = [
        "# All-Incident Failure Attribution Summary",
        "",
        f"Overall record count: {len(rows)}",
        "",
        "## Per-incident record counts",
        "",
        "| Incident | Records |",
        "| --- | ---: |",
        *[
            f"| {incident} | {len(grouped[incident])} |"
            for incident in incidents
        ],
        "",
        "## Official reward distribution",
        "",
        *_counter_lines(_distribution(rows, "reward_official")),
        "",
        "## Primary candidate counts",
        "",
        *_counter_lines(_distribution(rows, "primary_cause_candidate")),
        "",
        "## Reviewed primary counts",
        "",
        *_counter_lines(_distribution(rows, "reviewed_primary")),
        "",
        "## Review completion counts",
        "",
        *_counter_lines(_distribution(rows, "review_status")),
        "",
        "## Per-incident distributions",
        "",
    ]

    for incident in incidents:
        incident_rows = grouped[incident]
        lines.extend(
            [
                f"### {incident}",
                "",
                "Official rewards:",
                *_counter_lines(
                    _distribution(incident_rows, "reward_official")
                ),
                "",
                "Primary candidates:",
                *_counter_lines(
                    _distribution(incident_rows, "primary_cause_candidate")
                ),
                "",
                "Reviewed primary:",
                *_counter_lines(
                    _distribution(incident_rows, "reviewed_primary")
                ),
                "",
                "Review completion:",
                *_counter_lines(
                    _distribution(incident_rows, "review_status")
                ),
                "",
            ]
        )

    path.write_text("\n".join(lines), encoding="utf-8", newline="\n")


def _validate_outputs(
    csv_path: Path,
    summary_path: Path,
    expected_rows: list[dict[str, Any]],
) -> None:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        csv_rows = list(csv.DictReader(handle))
    if len(csv_rows) != len(expected_rows):
        raise InputError("aggregate validation failed: CSV record count")

    expected_counts = Counter(row["incident"] for row in expected_rows)
    csv_counts = Counter(row["incident"] for row in csv_rows)
    if csv_counts != expected_counts:
        raise InputError("aggregate validation failed: per-incident CSV counts")

    summary = summary_path.read_text(encoding="utf-8")
    if f"Overall record count: {len(expected_rows)}" not in summary:
        raise InputError("aggregate validation failed: summary total")
    for incident, count in expected_counts.items():
        if f"| {incident} | {count} |" not in summary:
            raise InputError(
                f"aggregate validation failed: summary count for {incident}"
            )


def aggregate(paths: list[Path], output_dir: Path) -> list[Path]:
    if output_dir.exists():
        raise OutputCollisionError(f"output path already exists: {output_dir}")

    rows = sorted(load_incident_rows(paths), key=_row_sort_key)
    parent = output_dir.parent
    if parent.exists() and not parent.is_dir():
        raise InputError(f"output parent is not a directory: {parent}")
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InputError(f"cannot create output parent {parent}: {exc}") from exc

    temp_dir = Path(
        tempfile.mkdtemp(prefix=f".{output_dir.name}.", dir=parent)
    )
    moved = False
    try:
        csv_path = temp_dir / "all_incidents_attribution.csv"
        summary_path = temp_dir / "all_incidents_summary.md"
        _write_csv(csv_path, rows)
        _write_summary(summary_path, rows)
        _validate_outputs(csv_path, summary_path, rows)

        if output_dir.exists():
            raise OutputCollisionError(
                f"output path appeared during aggregation: {output_dir}"
            )
        temp_dir.rename(output_dir)
        moved = True
        return [
            output_dir / "all_incidents_attribution.csv",
            output_dir / "all_incidents_summary.md",
        ]
    finally:
        if not moved and temp_dir.exists():
            shutil.rmtree(temp_dir)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Aggregate eight canonical SecRL attribution JSONL files"
    )
    parser.add_argument(
        "--input-jsonl",
        action="append",
        required=True,
        type=Path,
        help="incident-level attribution JSONL; repeat exactly eight times",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="new aggregate output directory",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        aggregate(args.input_jsonl, args.output_dir)
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
