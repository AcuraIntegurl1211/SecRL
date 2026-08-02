"""Offline prepare/finalize CLI for the SQL retrieval subtype overlay.

The command deliberately has no execution or network integrations.  It only
reads hash-gated source files, reconstructs immutable evidence bundles, and
delegates publication to the validated extraction/review/reporting layers.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import re
import subprocess
import sys
from dataclasses import fields
from collections.abc import Mapping
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from experiments.failure_analysis.models import (  # noqa: E402
    AnalysisError,
    InputError,
    MappingError,
    OutputCollisionError,
)
from experiments.failure_analysis.identity import sha256_file  # noqa: E402
from experiments.failure_analysis.retrieval_extract import (  # noqa: E402
    SourceSpec,
    build_evidence_bundles,
    load_reviewed_rows,
    load_source_specs,
    write_preparation_files,
)
from experiments.failure_analysis.retrieval_models import (  # noqa: E402
    QueryStep,
    RetrievalEvidenceBundle,
)
from experiments.failure_analysis.retrieval_reporting import (  # noqa: E402
    write_retrieval_outputs,
)
from experiments.failure_analysis.retrieval_review import (  # noqa: E402
    apply_completed_review,
    load_overlay_taxonomy,
    select_low_confidence_rows,
)


EXPECTED_COUNTS: dict[str, int] = {
    "incident_5": 18,
    "incident_38": 2,
    "incident_34": 20,
    "incident_39": 42,
    "incident_55": 54,
    "incident_134": 30,
    "incident_166": 47,
    "incident_322": 20,
}
EXPECTED_MANIFEST_COUNT = 8
EXPECTED_SOURCE_COUNT = 24
_BUNDLE_FIELDS = tuple(field.name for field in fields(RetrievalEvidenceBundle))
_HASH_RE = re.compile(r"[0-9a-f]{64}\Z")
_INCIDENT_RE = re.compile(r"incident_[0-9]+\Z")
_ROW_INPUTS = {"aggregate_csv", "completed_review_csv", "taxonomy", "evidence_jsonl"}
_JSON_SCALARS = (str, bool, int)


def _lexists(path: Path) -> bool:
    try:
        return os.path.lexists(os.fspath(path))
    except (OSError, TypeError, ValueError) as exc:
        raise InputError(f"cannot inspect path {path}: {exc}") from exc


def _canonical_file(path: Path, label: str) -> Path:
    if not isinstance(path, Path):
        raise InputError(f"{label} must be a Path")
    try:
        canonical = path.resolve(strict=True)
        if not canonical.is_file():
            raise InputError(f"{label} is not a regular file: {path}")
        if not os.path.isfile(os.fspath(canonical)):
            raise InputError(f"{label} is not a regular file: {path}")
        return canonical
    except InputError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise InputError(f"{label} is not readable: {path}: {exc}") from exc


def _new_destination(path: Path, label: str) -> None:
    if not isinstance(path, Path):
        raise InputError(f"{label} must be a Path")
    if _lexists(path):
        raise OutputCollisionError(f"{label} already exists: {path}")
    try:
        parent = path.parent
        if parent.exists() and not parent.is_dir():
            raise InputError(f"{label} parent is not a directory: {parent}")
    except (OSError, ValueError, RuntimeError) as exc:
        raise InputError(f"cannot inspect {label} parent {path.parent}: {exc}") from exc


def _hash(path: Path, label: str) -> str:
    try:
        return sha256_file(path)
    except AnalysisError:
        raise
    except (OSError, ValueError, TypeError) as exc:
        raise InputError(f"cannot hash {label} {path}: {exc}") from exc


def _manifest_incidents(manifest_paths: list[Path], source_specs: dict[str, SourceSpec]) -> dict[str, Path]:
    if len(manifest_paths) != EXPECTED_MANIFEST_COUNT:
        raise InputError("exactly eight --manifest paths are required")
    if set(source_specs) != set(EXPECTED_COUNTS):
        raise MappingError("manifest incidents do not match the frozen eight-incident set")
    result: dict[str, Path] = {}
    for path in manifest_paths:
        path = _canonical_file(path, "manifest")
        try:
            value = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_reject_duplicate_json_keys,
                parse_constant=_parse_constant,
            )
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError) as exc:
            raise InputError(f"invalid manifest {path}: {exc}") from exc
        incident = value.get("incident") if isinstance(value, dict) else None
        if type(incident) is not str or not _INCIDENT_RE.fullmatch(incident):
            raise InputError(f"invalid manifest incident: {path}")
        if incident in result:
            raise MappingError(f"duplicate manifest incident: {incident}")
        result[incident] = path
    if set(result) != set(EXPECTED_COUNTS):
        raise MappingError("manifest incidents do not match the frozen eight-incident set")
    return result


def _source_provenance(
    aggregate: Path,
    completed_review: Path | None,
    taxonomy: Path,
    evidence: Path | None,
    manifest_paths: dict[str, Path],
    source_specs: dict[str, SourceSpec],
) -> tuple[dict[str, Path], dict[str, str]]:
    paths: dict[str, Path] = {
        "aggregate_csv": aggregate,
        "taxonomy": taxonomy,
    }
    if completed_review is not None:
        paths["completed_review_csv"] = completed_review
    if evidence is not None:
        paths["evidence_jsonl"] = evidence
    for incident in EXPECTED_COUNTS:
        paths[f"manifest_{incident}"] = manifest_paths[incident]
        spec = source_specs[incident]
        paths[f"agent_{incident}"] = spec.agent_path
        paths[f"env_{incident}"] = spec.env_path
        paths[f"question_{incident}"] = spec.question_path
    required = _ROW_INPUTS
    if set(paths) != required | {
        f"manifest_{incident}" for incident in EXPECTED_COUNTS
    } | {
        f"{kind}_{incident}"
        for incident in EXPECTED_COUNTS
        for kind in ("agent", "env", "question")
    }:
        raise MappingError("incomplete finalization provenance set")
    if len(paths) != 4 + EXPECTED_MANIFEST_COUNT + EXPECTED_SOURCE_COUNT:
        raise MappingError("finalization provenance must contain exactly 36 inputs")
    canonical_paths = {key: _canonical_file(value, f"provenance {key}") for key, value in paths.items()}
    hashes = {key: _hash(value, key) for key, value in canonical_paths.items()}
    return canonical_paths, hashes


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _json_value(value: object, label: str) -> object:
    if value is None or type(value) in _JSON_SCALARS:
        return value
    if type(value) is float:
        if not math.isfinite(value):
            raise MappingError(f"{label} contains a non-finite float")
        return value
    if type(value) is dict:
        result: dict[str, object] = {}
        for key, item in value.items():
            if type(key) is not str:
                raise MappingError(f"{label} contains a non-string object key")
            result[key] = _json_value(item, label)
        return result
    if type(value) is list:
        return [_json_value(item, label) for item in value]
    raise MappingError(f"{label} is not a JSON value")


def _mapping_error(message: str, exc: BaseException | None = None) -> MappingError:
    error = MappingError(message)
    if exc is not None:
        error.__cause__ = exc
    return error


def _parse_evidence_row(raw: object, position: int) -> RetrievalEvidenceBundle:
    if type(raw) is not dict:
        raise MappingError(f"evidence row {position} must be an object")
    if set(raw) != set(_BUNDLE_FIELDS):
        raise MappingError(f"evidence row {position} fields are not frozen")
    for key in _BUNDLE_FIELDS:
        if key not in raw:
            raise MappingError(f"evidence row {position} is missing {key}")
    incident = raw["incident"]
    if type(incident) is not str or not _INCIDENT_RE.fullmatch(incident):
        raise MappingError(f"evidence row {position} has invalid incident")
    for key in ("question_index", "agent_source_index", "env_source_index", "trajectory_steps"):
        value = raw[key]
        if type(value) is not int or value < 0:
            raise MappingError(f"evidence row {position} has invalid {key}")
    for key in ("submitted", "submitted_at_step_limit"):
        if type(raw[key]) is not bool:
            raise MappingError(f"evidence row {position} has invalid {key}")
    if raw["submitted"] and raw["trajectory_steps"] <= 0:
        raise MappingError(f"evidence row {position} has invalid submission contract")
    if raw["submitted_at_step_limit"] and (
        not raw["submitted"] or raw["trajectory_steps"] <= 0
    ):
        raise MappingError(f"evidence row {position} has invalid step-limit contract")
    for key in (
        "question_fingerprint_sha256", "question_text_fingerprint_sha256",
        "agent_source_sha256", "env_source_sha256", "question_source_sha256",
    ):
        if type(raw[key]) is not str or _HASH_RE.fullmatch(raw[key]) is None:
            raise MappingError(f"evidence row {position} has invalid {key}")
    for key in ("question", "context", "submitted_answer", "reviewed_primary_original", "review_notes_original"):
        if type(raw[key]) is not str:
            raise MappingError(f"evidence row {position} has invalid {key}")
    reward = raw["reward_official"]
    if type(reward) is not float or not math.isfinite(reward):
        raise MappingError(f"evidence row {position} has invalid reward_official")
    golden_answer = _json_value(raw["golden_answer"], f"evidence row {position} golden_answer")
    golden_solution = _json_value(raw["golden_solution"], f"evidence row {position} golden_solution")
    query_steps_raw = raw["query_steps"]
    if type(query_steps_raw) is not list:
        raise MappingError(f"evidence row {position} query_steps must be a list")
    query_steps: list[QueryStep] = []
    previous_step = 0
    for step_position, value in enumerate(query_steps_raw):
        if type(value) is not dict or set(value) != {"step", "sql", "observation", "query_success"}:
            raise MappingError(f"evidence row {position} query_steps[{step_position}] schema")
        step = value["step"]
        if type(step) is not int or step <= previous_step or step > raw["trajectory_steps"]:
            raise MappingError(f"evidence row {position} query_steps step")
        if type(value["sql"]) is not str or type(value["observation"]) is not str:
            raise MappingError(f"evidence row {position} query_steps text")
        success = value["query_success"]
        if success is not None and type(success) is not bool:
            raise MappingError(f"evidence row {position} query_steps query_success")
        _json_value(value, f"evidence row {position} query_steps[{step_position}]")
        query_steps.append(QueryStep(step, value["sql"], value["observation"], success))
        previous_step = step
    try:
        return RetrievalEvidenceBundle(
            incident=incident,
            question_index=raw["question_index"],
            question_fingerprint_sha256=raw["question_fingerprint_sha256"],
            question_text_fingerprint_sha256=raw["question_text_fingerprint_sha256"],
            question=raw["question"],
            context=raw["context"],
            golden_answer=golden_answer,
            golden_solution=golden_solution,
            submitted_answer=raw["submitted_answer"],
            trajectory_steps=raw["trajectory_steps"],
            submitted=raw["submitted"],
            submitted_at_step_limit=raw["submitted_at_step_limit"],
            reward_official=reward,
            reviewed_primary_original=raw["reviewed_primary_original"],
            review_notes_original=raw["review_notes_original"],
            agent_source_index=raw["agent_source_index"],
            env_source_index=raw["env_source_index"],
            agent_source_sha256=raw["agent_source_sha256"],
            env_source_sha256=raw["env_source_sha256"],
            question_source_sha256=raw["question_source_sha256"],
            query_steps=tuple(query_steps),
        )
    except (TypeError, ValueError, OverflowError, RecursionError) as exc:
        raise _mapping_error(f"invalid evidence row {position}", exc) from exc


def _parse_constant(value: str) -> object:
    raise ValueError(f"non-finite JSON constant {value}")


def _load_evidence_bundles(path: Path) -> list[RetrievalEvidenceBundle]:
    path = _canonical_file(path, "evidence JSONL")
    bundles: list[RetrievalEvidenceBundle] = []
    identities: set[tuple[str, int, str]] = set()
    try:
        with path.open("r", encoding="utf-8") as handle:
            for position, line in enumerate(handle, 1):
                if not line.endswith("\n") or not line[:-1]:
                    raise MappingError(f"invalid evidence JSONL line {position}")
                raw = json.loads(
                    line[:-1],
                    object_pairs_hook=_reject_duplicate_json_keys,
                    parse_constant=_parse_constant,
                )
                bundle = _parse_evidence_row(raw, position)
                identity = (
                    bundle.incident,
                    bundle.question_index,
                    bundle.question_fingerprint_sha256,
                )
                if identity in identities:
                    raise MappingError(f"duplicate evidence identity: {identity}")
                identities.add(identity)
                bundles.append(bundle)
    except MappingError:
        raise
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError, RecursionError) as exc:
        raise _mapping_error(f"cannot parse evidence JSONL {path}: {exc}", exc) from exc
    return bundles


def _strict_equal(left: object, right: object) -> bool:
    # MappingProxyType is used by immutable evidence bundles.  Compare any
    # Mapping recursively instead of relying on Python's bool==int equality.
    if isinstance(left, Mapping) or isinstance(right, Mapping):
        if not isinstance(left, Mapping) or not isinstance(right, Mapping):
            return False
        if len(left) != len(right) or set(left) != set(right):
            return False
        return all(_strict_equal(left[key], right[key]) for key in left)  # type: ignore[index]
    if type(left) is not type(right):
        return False
    if isinstance(left, (list, tuple)):
        return len(left) == len(right) and all(_strict_equal(a, b) for a, b in zip(left, right))  # type: ignore[arg-type]
    return left == right


def _compare_evidence_bundles(
    fresh: list[RetrievalEvidenceBundle], evidence: list[RetrievalEvidenceBundle]
) -> None:
    if len(fresh) != len(evidence):
        raise MappingError("evidence bundle count does not match fresh extraction")
    fresh_map: dict[tuple[str, int, str], RetrievalEvidenceBundle] = {}
    for bundle in fresh:
        identity = (bundle.incident, bundle.question_index, bundle.question_fingerprint_sha256)
        if identity in fresh_map:
            raise MappingError(f"duplicate fresh bundle identity: {identity}")
        fresh_map[identity] = bundle
    seen: set[tuple[str, int, str]] = set()
    for bundle in evidence:
        identity = (bundle.incident, bundle.question_index, bundle.question_fingerprint_sha256)
        if identity in seen:
            raise MappingError(f"duplicate evidence identity: {identity}")
        seen.add(identity)
        wanted = fresh_map.get(identity)
        if wanted is None:
            raise MappingError(f"evidence identity is absent from fresh extraction: {identity}")
        if any(not _strict_equal(getattr(bundle, field), getattr(wanted, field)) for field in _BUNDLE_FIELDS):
            raise MappingError(f"evidence bundle drift for identity: {identity}")
    if seen != set(fresh_map):
        raise MappingError("evidence identities do not exactly match fresh extraction")


def _git_commit() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(__file__).resolve().parents[2],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = result.stdout.strip()
    return commit if result.returncode == 0 and commit else None


def _load_inputs(args: argparse.Namespace) -> tuple[Path, dict[str, Path], dict[str, SourceSpec], dict[str, object]]:
    aggregate = _canonical_file(args.aggregate_csv, "aggregate CSV")
    taxonomy_path = _canonical_file(args.taxonomy, "taxonomy")
    root = args.source_repo_root
    if not isinstance(root, Path):
        raise InputError("source-repo-root must be a Path")
    try:
        root = root.resolve(strict=True)
        if not root.is_dir():
            raise InputError(f"source-repo-root is not a directory: {root}")
    except InputError:
        raise
    except (OSError, ValueError, RuntimeError) as exc:
        raise InputError(f"cannot read source-repo-root {root}: {exc}") from exc
    manifest_paths = [_canonical_file(path, "manifest") for path in args.manifest]
    try:
        source_specs = load_source_specs(manifest_paths, root)
    except InputError as exc:
        # A malformed manifest is an input error.  Once a manifest is
        # structurally valid, however, a missing/hash-changed source is a
        # mapping failure under the frozen provenance contract.
        message = str(exc)
        if (
            "SHA-256 mismatch" in message
            or "source path is not a file" in message
            or "duplicate source manifest incident" in message
        ):
            raise MappingError(message) from exc
        raise
    manifest_by_incident = _manifest_incidents(manifest_paths, source_specs)
    taxonomy = load_overlay_taxonomy(taxonomy_path)
    return aggregate, manifest_by_incident, source_specs, taxonomy


def _build_fresh(args: argparse.Namespace) -> tuple[Path, dict[str, Path], dict[str, SourceSpec], dict[str, object], list[RetrievalEvidenceBundle]]:
    aggregate, manifest_paths, source_specs, taxonomy = _load_inputs(args)
    _hash(aggregate, "aggregate CSV")
    rows = load_reviewed_rows(aggregate)
    bundles = build_evidence_bundles(rows, source_specs, EXPECTED_COUNTS)
    if len(bundles) != sum(EXPECTED_COUNTS.values()):
        raise MappingError("fresh extraction did not produce exactly 233 bundles")
    return aggregate, manifest_paths, source_specs, taxonomy, bundles


def _run_prepare(args: argparse.Namespace) -> list[Path]:
    _new_destination(args.work_dir, "work-dir")
    _aggregate, _manifest_paths, _source_specs, _taxonomy, bundles = _build_fresh(args)
    evidence = args.work_dir / "evidence_bundles.jsonl"
    review = args.work_dir / "review_template.csv"
    write_preparation_files(bundles, evidence, review)
    return [evidence, review]


def _run_finalize(args: argparse.Namespace) -> list[Path]:
    _new_destination(args.output_dir, "output-dir")
    aggregate, manifest_paths, source_specs, taxonomy, fresh = _build_fresh(args)
    evidence_path = _canonical_file(args.evidence_jsonl, "evidence JSONL")
    review_path = _canonical_file(args.completed_review_csv, "completed review CSV")
    evidence = _load_evidence_bundles(evidence_path)
    _compare_evidence_bundles(fresh, evidence)
    rows = apply_completed_review(fresh, review_path, taxonomy)
    queue = select_low_confidence_rows(rows)
    input_paths, input_hashes = _source_provenance(
        aggregate,
        review_path,
        _canonical_file(args.taxonomy, "taxonomy"),
        evidence_path,
        manifest_paths,
        source_specs,
    )
    parent = args.output_dir.parent
    try:
        parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise InputError(f"cannot create output parent {parent}: {exc}") from exc
    return write_retrieval_outputs(rows, queue, args.output_dir, input_paths, input_hashes, _git_commit())


def prepare(args: argparse.Namespace) -> list[Path]:
    """Run the validated offline preparation stage."""
    return _run_prepare(args)


def finalize(args: argparse.Namespace) -> list[Path]:
    """Run the validated offline finalization stage."""
    return _run_finalize(args)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Offline SQL retrieval subtype preparation/finalization")
    subparsers = parser.add_subparsers(dest="mode", required=True)

    prepare = subparsers.add_parser("prepare")
    _add_common_inputs(prepare)
    prepare.add_argument("--work-dir", required=True, type=Path)

    finalize = subparsers.add_parser("finalize")
    _add_common_inputs(finalize)
    finalize.add_argument("--evidence-jsonl", required=True, type=Path)
    finalize.add_argument("--completed-review-csv", required=True, type=Path)
    finalize.add_argument("--output-dir", required=True, type=Path)
    return parser.parse_args(argv)


def _add_common_inputs(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--aggregate-csv", required=True, type=Path)
    parser.add_argument("--manifest", required=True, action="append", type=Path)
    parser.add_argument("--source-repo-root", required=True, type=Path)
    parser.add_argument("--taxonomy", required=True, type=Path)


def run(args: argparse.Namespace) -> list[Path]:
    if args.mode == "prepare":
        if len(args.manifest) != EXPECTED_MANIFEST_COUNT:
            raise InputError("exactly eight --manifest paths are required")
        return prepare(args)
    if args.mode == "finalize":
        if len(args.manifest) != EXPECTED_MANIFEST_COUNT:
            raise InputError("exactly eight --manifest paths are required")
        return finalize(args)
    raise InputError(f"unknown CLI mode: {args.mode!r}")


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
