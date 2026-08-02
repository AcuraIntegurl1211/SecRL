"""Build immutable, reviewable SQL-retrieval evidence bundles."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import os
import re
import tempfile
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from .features import extract_features
from .identity import canonical_json, map_logs, sha256_file
from .models import InputError, MappingError, OutputCollisionError
from .retrieval_models import QueryStep, RetrievalEvidenceBundle, thaw_json_value


_FINGERPRINT_RE = re.compile(r"[0-9a-f]{64}\Z")
_INCIDENT_RE = re.compile(r"incident_([0-9]+)\Z")
_CANONICAL_NONNEGATIVE_INT_RE = re.compile(r"(?:0|[1-9][0-9]*)\Z")
_AGGREGATE_SUBMISSION_FIELDS = (
    "steps",
    "max_steps",
    "submitted",
    "submitted_at_step_limit",
)
_REVIEW_REQUIRED_FIELDS = (
    "incident",
    "question_index",
    "question_fingerprint_sha256",
    "reward_official",
    *_AGGREGATE_SUBMISSION_FIELDS,
    "reviewed_primary",
    "review_notes",
)


@dataclass(frozen=True)
class SourceSpec:
    incident: str
    agent_path: Path
    env_path: Path
    question_path: Path
    agent_sha256: str
    env_sha256: str
    question_sha256: str


@dataclass(frozen=True)
class _AggregateSubmissionContract:
    steps: int
    max_steps: int
    submitted: bool
    submitted_at_step_limit: bool


def _incident_sort_key(incident: str) -> int:
    match = _INCIDENT_RE.fullmatch(incident) if isinstance(incident, str) else None
    if match is None:
        raise InputError(f"invalid incident identity: {incident!r}")
    return int(match.group(1))


def _row_identity(row: dict[str, str], source_index: int) -> tuple[str, int, str]:
    incident = row.get("incident", "")
    if not incident:
        raise InputError(f"invalid review row {source_index}: incident")
    _incident_sort_key(incident)
    value = row.get("question_index", "")
    try:
        question_index = int(value)
    except (TypeError, ValueError) as exc:
        raise InputError(
            f"invalid review row {source_index}: question_index"
        ) from exc
    if question_index < 0:
        raise InputError(f"invalid review row {source_index}: question_index")
    fingerprint = row.get("question_fingerprint_sha256", "")
    if _FINGERPRINT_RE.fullmatch(fingerprint) is None:
        raise InputError(f"invalid review row {source_index}: fingerprint")
    return incident, question_index, fingerprint


def _reviewed_reward(row: dict[str, str], source_index: int) -> float:
    value = row.get("reward_official", "")
    try:
        reward = float(value)
    except (TypeError, ValueError) as exc:
        raise InputError(
            f"invalid review row {source_index}: reward_official"
        ) from exc
    if not math.isfinite(reward):
        raise InputError(f"invalid review row {source_index}: reward_official")
    return reward


def _aggregate_mapping_error(
    row: dict[str, object], source_index: int, field: str, detail: str
) -> MappingError:
    return MappingError(
        "invalid aggregate submission contract: "
        f"incident={row.get('incident', '')!r} "
        f"question_index={row.get('question_index', '')} "
        f"fingerprint={row.get('question_fingerprint_sha256', '')} "
        f"row={source_index} field={field} {detail}"
    )


def _canonical_nonnegative_int(
    row: dict[str, object], source_index: int, field: str
) -> int:
    value = row.get(field)
    if not isinstance(value, str) or _CANONICAL_NONNEGATIVE_INT_RE.fullmatch(value) is None:
        raise _aggregate_mapping_error(row, source_index, field, "is not canonical")
    return int(value)


def _canonical_bool(
    row: dict[str, object], source_index: int, field: str
) -> bool:
    value = row.get(field)
    if value not in ("True", "False"):
        raise _aggregate_mapping_error(row, source_index, field, "is not canonical")
    return value == "True"


def _aggregate_submission_contract(
    row: dict[str, object], source_index: int
) -> _AggregateSubmissionContract:
    steps = _canonical_nonnegative_int(row, source_index, "steps")
    max_steps = _canonical_nonnegative_int(row, source_index, "max_steps")
    submitted = _canonical_bool(row, source_index, "submitted")
    submitted_at_step_limit = _canonical_bool(
        row, source_index, "submitted_at_step_limit"
    )
    if submitted_at_step_limit and not submitted:
        raise _aggregate_mapping_error(
            row,
            source_index,
            "submitted_at_step_limit",
            "requires submitted=True",
        )
    if submitted_at_step_limit and steps <= 0:
        raise _aggregate_mapping_error(
            row,
            source_index,
            "submitted_at_step_limit",
            "requires steps>0",
        )
    return _AggregateSubmissionContract(
        steps=steps,
        max_steps=max_steps,
        submitted=submitted,
        submitted_at_step_limit=submitted_at_step_limit,
    )


def load_reviewed_rows(path: Path) -> list[dict[str, str]]:
    """Read and identity-validate only rows reviewed as SQL retrieval failures."""
    try:
        handle = path.open("r", encoding="utf-8", newline="")
    except OSError as exc:
        raise InputError(f"cannot read reviewed rows {path}: {exc}") from exc

    with handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise InputError(f"invalid review header in {path}")
        missing_submission_fields = [
            field
            for field in _AGGREGATE_SUBMISSION_FIELDS
            if field not in reader.fieldnames
        ]
        if missing_submission_fields:
            raise MappingError(
                f"invalid review header in {path}: missing "
                f"{','.join(missing_submission_fields)}"
            )
        if any(field not in reader.fieldnames for field in _REVIEW_REQUIRED_FIELDS):
            raise InputError(f"invalid review header in {path}")
        selected: list[dict[str, str]] = []
        identities: set[tuple[str, int, str]] = set()
        for source_index, raw_row in enumerate(reader, 2):
            row = {key: (value if value is not None else "") for key, value in raw_row.items()}
            identity = _row_identity(row, source_index)
            _reviewed_reward(row, source_index)
            _aggregate_submission_contract(row, source_index)
            if identity in identities:
                raise InputError(
                    f"duplicate review identity at row {source_index}: {identity}"
                )
            identities.add(identity)
            if row["reviewed_primary"] == "SQL_RETRIEVAL":
                selected.append(row)

    return sorted(
        selected,
        key=lambda row: (_incident_sort_key(row["incident"]), int(row["question_index"])),
    )


def _load_manifest(path: Path) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        raise InputError(f"invalid source manifest {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise InputError(f"invalid source manifest {path}: expected object")
    return value


def _source_record(
    manifest_path: Path,
    sources: dict[str, Any],
    source_name: str,
    repo_root: Path,
) -> tuple[Path, str]:
    value = sources.get(source_name)
    if not isinstance(value, dict):
        raise InputError(
            f"invalid source manifest {manifest_path}: missing sources.{source_name}"
        )
    raw_path = value.get("path")
    expected_sha = value.get("sha256")
    if not isinstance(raw_path, str) or not raw_path:
        raise InputError(
            f"invalid source manifest {manifest_path}: missing sources.{source_name}.path"
        )
    if not isinstance(expected_sha, str) or _FINGERPRINT_RE.fullmatch(expected_sha) is None:
        raise InputError(
            f"invalid source manifest {manifest_path}: invalid sources.{source_name}.sha256"
        )
    resolved = Path(raw_path)
    if not resolved.is_absolute():
        resolved = repo_root / resolved
    if not resolved.is_file():
        raise InputError(f"source path is not a file: {resolved}")
    actual_sha = sha256_file(resolved)
    if actual_sha.lower() != expected_sha.lower():
        raise InputError(f"source SHA-256 mismatch for {resolved}")
    return resolved, actual_sha


def load_source_specs(
    manifest_paths: list[Path], repo_root: Path
) -> dict[str, SourceSpec]:
    """Load trusted manifest paths and hash-gate their referenced source inputs."""
    root = repo_root
    specs: dict[str, SourceSpec] = {}
    for manifest_path in manifest_paths:
        manifest = _load_manifest(manifest_path)
        incident = manifest.get("incident")
        sources = manifest.get("sources")
        if not isinstance(incident, str) or not incident:
            raise InputError(f"invalid source manifest {manifest_path}: incident")
        _incident_sort_key(incident)
        if incident in specs:
            raise InputError(f"duplicate source manifest incident: {incident}")
        if not isinstance(sources, dict):
            raise InputError(f"invalid source manifest {manifest_path}: sources")
        agent_path, agent_sha = _source_record(manifest_path, sources, "agent", root)
        env_path, env_sha = _source_record(manifest_path, sources, "env", root)
        question_path, question_sha = _source_record(
            manifest_path, sources, "question", root
        )
        specs[incident] = SourceSpec(
            incident=incident,
            agent_path=agent_path,
            env_path=env_path,
            question_path=question_path,
            agent_sha256=agent_sha,
            env_sha256=env_sha,
            question_sha256=question_sha,
        )
    return specs


def _load_hashed_json(path: Path, expected_sha256: str) -> list[dict[str, Any]]:
    """Read trusted, hash-gated manifest input bytes exactly once before parsing."""
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise InputError(f"cannot read input path {path}: {exc}") from exc
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise InputError(f"source SHA-256 mismatch for {path}")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise InputError(f"invalid JSON input {path}: {exc}") from exc
    if not isinstance(value, list):
        raise InputError(f"invalid JSON input {path}: expected top-level list")
    for index, item in enumerate(value):
        if not isinstance(item, dict):
            raise InputError(
                f"invalid JSON input {path}: expected object at index {index}"
            )
    return value


def _build_query_steps(env: dict[str, Any]) -> tuple[QueryStep, ...]:
    trajectory = env.get("trajectory")
    if not isinstance(trajectory, list):
        raise MappingError("env trajectory is not a list")
    steps: list[QueryStep] = []
    for index, item in enumerate(trajectory, 1):
        if not isinstance(item, dict):
            raise MappingError(f"env trajectory step {index} is not an object")
        info = item.get("info")
        if not isinstance(info, dict):
            info = {}
        if info.get("submit") is True:
            continue
        sql = item.get("action", "")
        observation = item.get("observation", "")
        query_success = info.get("query_success")
        steps.append(
            QueryStep(
                step=index,
                sql=sql if isinstance(sql, str) else str(sql),
                observation=(
                    observation if isinstance(observation, str) else str(observation)
                ),
                query_success=query_success if isinstance(query_success, bool) else None,
            )
        )
    return tuple(steps)


def _submitted_answer(env: dict[str, Any]) -> str:
    trajectory = env.get("trajectory")
    if not isinstance(trajectory, list):
        raise MappingError("env trajectory is not a list")
    answer = ""
    for item in trajectory:
        if not isinstance(item, dict):
            raise MappingError("env trajectory contains a non-object step")
        info = item.get("info")
        if isinstance(info, dict) and info.get("submit") is True:
            value = info.get("submitted_answer", item.get("action", ""))
            answer = value if isinstance(value, str) else str(value)
    return answer


def _review_identity(row: dict[str, str]) -> tuple[str, int, str]:
    return _row_identity(row, 0)


def build_evidence_bundles(
    reviewed_rows: list[dict[str, str]],
    source_specs: dict[str, SourceSpec],
    expected_counts: dict[str, int],
) -> list[RetrievalEvidenceBundle]:
    """Map reviewed identities to source-verified, immutable evidence bundles."""
    if set(source_specs) != set(expected_counts):
        raise MappingError("source incidents and expected-count incidents differ")
    if any(not isinstance(count, int) or isinstance(count, bool) or count < 0 for count in expected_counts.values()):
        raise MappingError("expected counts must be non-negative integers")

    rows_by_incident: dict[str, list[dict[str, str]]] = {
        incident: [] for incident in source_specs
    }
    identities: set[tuple[str, int, str]] = set()
    aggregate_by_identity: dict[
        tuple[str, int, str], _AggregateSubmissionContract
    ] = {}
    for row in reviewed_rows:
        identity = _review_identity(row)
        aggregate_by_identity[identity] = _aggregate_submission_contract(row, 0)
        if identity in identities:
            raise MappingError(f"duplicate output identity: {identity}")
        identities.add(identity)
        incident = identity[0]
        if incident not in rows_by_incident:
            raise MappingError(f"reviewed incident has no source manifest: {incident}")
        rows_by_incident[incident].append(row)

    if sum(expected_counts.values()) != len(reviewed_rows):
        raise MappingError("expected total does not equal reviewed row count")
    for incident, count in expected_counts.items():
        if len(rows_by_incident[incident]) != count:
            raise MappingError(f"reviewed count mismatch for incident {incident}")

    bundles: list[RetrievalEvidenceBundle] = []
    for incident in sorted(source_specs, key=_incident_sort_key):
        spec = source_specs[incident]
        agent_entries = _load_hashed_json(spec.agent_path, spec.agent_sha256)
        env_entries = _load_hashed_json(spec.env_path, spec.env_sha256)
        questions = _load_hashed_json(spec.question_path, spec.question_sha256)
        mapped_by_index = {
            mapped.identity.question_index: mapped
            for mapped in map_logs(incident, agent_entries, env_entries, questions)
        }
        for row in rows_by_incident[incident]:
            _, question_index, fingerprint = _review_identity(row)
            aggregate = aggregate_by_identity[(incident, question_index, fingerprint)]
            reviewed_reward = _reviewed_reward(row, 0)
            mapped = mapped_by_index.get(question_index)
            if mapped is None:
                raise MappingError(
                    f"reviewed question index is absent from sources: {incident}/{question_index}"
                )
            if mapped.identity.question_fingerprint_sha256 != fingerprint:
                raise MappingError(
                    f"review fingerprint mismatch: {incident}/{question_index}"
                )
            trajectory = mapped.env.get("trajectory")
            if not isinstance(trajectory, list):
                raise MappingError(
                    "env trajectory is not a list: "
                    f"incident={incident} question_index={question_index} "
                    f"fingerprint={fingerprint} field=trajectory"
                )
            for step_index, step in enumerate(trajectory, 1):
                if not isinstance(step, dict):
                    raise MappingError(
                        "env trajectory step is not an object: "
                        f"incident={incident} question_index={question_index} "
                        f"fingerprint={fingerprint} field=trajectory "
                        f"step={step_index}"
                    )
            env_steps = mapped.env.get("steps")
            if type(env_steps) is not int:
                raise MappingError(
                    "invalid env steps: "
                    f"incident={incident} question_index={question_index} "
                    f"fingerprint={fingerprint} field=steps expected exact int"
                )
            if env_steps != len(trajectory):
                raise MappingError(
                    "env steps mismatch: "
                    f"incident={incident} question_index={question_index} "
                    f"fingerprint={fingerprint} field=steps "
                    f"env={env_steps} trajectory={len(trajectory)}"
                )
            # The existing extractor compares agent and env official rewards.
            features = extract_features(mapped, aggregate.max_steps)
            for field, aggregate_value, mapped_value in (
                ("steps", aggregate.steps, features.steps),
                ("submitted", aggregate.submitted, features.submitted),
                (
                    "submitted_at_step_limit",
                    aggregate.submitted_at_step_limit,
                    features.submitted_at_step_limit,
                ),
            ):
                if aggregate_value != mapped_value:
                    raise MappingError(
                        "aggregate/env submission mismatch: "
                        f"incident={incident} question_index={question_index} "
                        f"fingerprint={fingerprint} field={field} "
                        f"aggregate={aggregate_value} mapped={mapped_value}"
                    )
            if reviewed_reward != features.reward_official:
                raise MappingError(
                    "reviewed official reward mismatch: "
                    f"incident={incident} question_index={question_index} "
                    f"fingerprint={fingerprint} aggregate={reviewed_reward} "
                    f"mapped={features.reward_official}"
                )
            question = mapped.question
            context = question.get("context", "")
            bundles.append(
                RetrievalEvidenceBundle(
                    incident=incident,
                    question_index=question_index,
                    question_fingerprint_sha256=(
                        mapped.identity.question_fingerprint_sha256
                    ),
                    question_text_fingerprint_sha256=(
                        mapped.identity.question_text_fingerprint_sha256
                    ),
                    question=question["question"],
                    context=context if isinstance(context, str) else canonical_json(context),
                    golden_answer=question.get("answer"),
                    golden_solution=question.get("solution"),
                    submitted_answer=_submitted_answer(mapped.env),
                    trajectory_steps=features.steps,
                    submitted=features.submitted,
                    submitted_at_step_limit=features.submitted_at_step_limit,
                    reward_official=features.reward_official,
                    reviewed_primary_original=row["reviewed_primary"],
                    review_notes_original=row["review_notes"],
                    agent_source_index=mapped.agent_source_index,
                    env_source_index=mapped.env_source_index,
                    agent_source_sha256=spec.agent_sha256,
                    env_source_sha256=spec.env_sha256,
                    question_source_sha256=spec.question_sha256,
                    query_steps=_build_query_steps(mapped.env),
                )
            )

    if len({(item.incident, item.question_index, item.question_fingerprint_sha256) for item in bundles}) != len(bundles):
        raise MappingError("duplicate output identity")
    return sorted(
        bundles,
        key=lambda item: (_incident_sort_key(item.incident), item.question_index),
    )


_DECISION_DEFAULTS: dict[str, object] = {
    "retrieval_primary_subtype": "INDETERMINATE",
    "auxiliary_tags": [],
    "retrieval_outcome": "UNOBSERVED",
    "boundary_flag": "NONE",
    "confidence": "indeterminate",
    "decision_status": "needs_review",
    "first_divergence_step": None,
    "relevant_sql_steps": [],
    "sql_evidence": "",
    "observation_evidence": "",
    "gold_evidence_basis": "",
    "rationale": "semantic review required before assigning a retrieval subtype",
}

_EVIDENCE_FIELDS = tuple(field.name for field in fields(RetrievalEvidenceBundle))
_REVIEW_FIELDS = (*_EVIDENCE_FIELDS, *_DECISION_DEFAULTS)


def _json_text(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _validate_hash(value: object, field: str) -> None:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        raise InputError(f"invalid bundle {field}")


def _validate_bundle(bundle: RetrievalEvidenceBundle) -> None:
    if type(bundle) is not RetrievalEvidenceBundle:
        raise InputError("preparation files require RetrievalEvidenceBundle values")
    _incident_sort_key(bundle.incident)
    for field in ("question_index", "agent_source_index", "env_source_index"):
        value = getattr(bundle, field)
        if type(value) is not int or value < 0:
            raise InputError(f"invalid bundle {field}")
    if type(bundle.trajectory_steps) is not int or bundle.trajectory_steps < 0:
        raise InputError("invalid bundle trajectory_steps")
    for field in ("submitted", "submitted_at_step_limit"):
        if type(getattr(bundle, field)) is not bool:
            raise InputError(f"invalid bundle {field}")
    if bundle.submitted and bundle.trajectory_steps <= 0:
        raise InputError("invalid bundle submitted requires trajectory_steps>0")
    if bundle.submitted_at_step_limit and not bundle.submitted:
        raise InputError(
            "invalid bundle submitted_at_step_limit requires submitted"
        )
    if bundle.submitted_at_step_limit and bundle.trajectory_steps <= 0:
        raise InputError(
            "invalid bundle submitted_at_step_limit requires trajectory_steps"
        )
    for field in (
        "question_fingerprint_sha256", "question_text_fingerprint_sha256",
        "agent_source_sha256", "env_source_sha256", "question_source_sha256",
    ):
        _validate_hash(getattr(bundle, field), field)
    for field in (
        "question", "context", "submitted_answer", "reviewed_primary_original",
        "review_notes_original",
    ):
        if not isinstance(getattr(bundle, field), str):
            raise InputError(f"invalid bundle {field}")
    if isinstance(bundle.reward_official, bool) or not isinstance(
        bundle.reward_official, (int, float)
    ) or not math.isfinite(float(bundle.reward_official)):
        raise InputError("invalid bundle reward_official")
    if type(bundle.query_steps) is not tuple:
        raise InputError("invalid bundle query_steps")
    previous_step = 0
    for query_step in bundle.query_steps:
        if type(query_step) is not QueryStep:
            raise InputError("invalid bundle query step")
        if type(query_step.step) is not int or query_step.step <= previous_step:
            raise InputError("invalid bundle query step")
        if query_step.step > bundle.trajectory_steps:
            raise InputError(
                "invalid bundle query step exceeds trajectory_steps"
            )
        if not isinstance(query_step.sql, str) or not isinstance(
            query_step.observation, str
        ):
            raise InputError("invalid bundle query step")
        if query_step.query_success is not None and type(
            query_step.query_success
        ) is not bool:
            raise InputError("invalid bundle query step")
        previous_step = query_step.step
    for field in ("golden_answer", "golden_solution"):
        try:
            _json_text(thaw_json_value(getattr(bundle, field)))
        except (TypeError, ValueError) as exc:
            raise InputError(f"invalid bundle {field}") from exc


def _bundle_row(bundle: RetrievalEvidenceBundle) -> dict[str, object]:
    # dataclasses.asdict deep-copies MappingProxyType frozen gold payloads.
    row = {field.name: getattr(bundle, field.name) for field in fields(bundle)}
    row["golden_answer"] = thaw_json_value(bundle.golden_answer)
    row["golden_solution"] = thaw_json_value(bundle.golden_solution)
    row["query_steps"] = [asdict(step) for step in bundle.query_steps]
    return row


def _csv_value(value: object) -> object:
    if value is None:
        return ""
    if isinstance(value, (dict, list, tuple)):
        return _json_text(value)
    return value


def _render_preparation_payloads(
    evidence_rows: list[dict[str, object]], review_rows: list[dict[str, object]],
) -> tuple[bytes, bytes]:
    """Render both outputs before creating directories or temporary files."""
    try:
        evidence_text = "".join(f"{_json_text(row)}\n" for row in evidence_rows)
        review_buffer = io.StringIO(newline="")
        writer = csv.DictWriter(
            review_buffer,
            fieldnames=_REVIEW_FIELDS,
            lineterminator="\n",
        )
        writer.writeheader()
        for row in review_rows:
            writer.writerow({key: _csv_value(row.get(key)) for key in _REVIEW_FIELDS})
        return evidence_text.encode("utf-8", "strict"), review_buffer.getvalue().encode(
            "utf-8", "strict"
        )
    except (UnicodeError, TypeError, ValueError, OverflowError, csv.Error) as exc:
        raise InputError(f"cannot serialize preparation files: {exc}") from exc


def _validate_output_bundles(
    bundles: list[RetrievalEvidenceBundle],
) -> list[RetrievalEvidenceBundle]:
    identities: set[tuple[str, int, str]] = set()
    for bundle in bundles:
        _validate_bundle(bundle)
        identity = (
            bundle.incident,
            bundle.question_index,
            bundle.question_fingerprint_sha256,
        )
        if identity in identities:
            raise InputError(f"duplicate output identity: {identity}")
        identities.add(identity)
        # Verify frozen payloads can be serialized before touching output paths.
        try:
            _json_text(_bundle_row(bundle))
        except (TypeError, ValueError) as exc:
            raise InputError(f"invalid output identity: {identity}") from exc
    return sorted(
        bundles,
        key=lambda item: (_incident_sort_key(item.incident), item.question_index),
    )


def _written_identities(
    evidence_jsonl: Path, review_template: Path,
) -> tuple[list[tuple[str, int, str]], list[tuple[str, int, str]]]:
    try:
        written_evidence = evidence_jsonl.read_text(encoding="utf-8").splitlines()
        with review_template.open("r", encoding="utf-8", newline="") as handle:
            written_review = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        raise InputError(f"cannot validate preparation files: {exc}") from exc
    try:
        evidence_identities = [
            (row["incident"], row["question_index"], row["question_fingerprint_sha256"])
            for row in (json.loads(line) for line in written_evidence)
        ]
        review_identities = [
            (row["incident"], int(row["question_index"]), row["question_fingerprint_sha256"])
            for row in written_review
        ]
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise InputError(f"invalid written preparation output: {exc}") from exc
    return evidence_identities, review_identities


def _unlink_if_staged_target(target: Path, staged: Path) -> None:
    try:
        if target.exists() and os.path.samestat(target.stat(), staged.stat()):
            target.unlink()
    except OSError:
        pass


def _unlink_quietly(path: Path | None) -> None:
    if path is None:
        return
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def write_preparation_files(
    bundles: list[RetrievalEvidenceBundle], evidence_jsonl: Path, review_template: Path
) -> None:
    """Stage and exclusively publish a non-overwriting evidence/review file pair."""
    ordered = _validate_output_bundles(bundles)
    evidence_target = evidence_jsonl.resolve()
    review_target = review_template.resolve()
    if evidence_target == review_target:
        raise InputError("evidence and review output paths must differ")
    if any(
        os.path.lexists(path)
        for path in (evidence_jsonl, review_template, evidence_target, review_target)
    ):
        raise OutputCollisionError("refusing to overwrite an existing preparation output")

    evidence_rows = [_bundle_row(bundle) for bundle in ordered]
    review_rows = [{**row, **_DECISION_DEFAULTS} for row in evidence_rows]
    expected_identities = [
        (bundle.incident, bundle.question_index, bundle.question_fingerprint_sha256)
        for bundle in ordered
    ]
    evidence_payload, review_payload = _render_preparation_payloads(
        evidence_rows, review_rows
    )
    evidence_stage: Path | None = None
    review_stage: Path | None = None
    published: list[tuple[Path, Path]] = []
    try:
        evidence_target.parent.mkdir(parents=True, exist_ok=True)
        review_target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=evidence_target.parent,
            prefix=".evidence-", delete=False,
        ) as evidence_handle:
            evidence_stage = Path(evidence_handle.name)
            evidence_handle.write(evidence_payload)
        with tempfile.NamedTemporaryFile(
            mode="wb", dir=review_target.parent,
            prefix=".review-", delete=False,
        ) as review_handle:
            review_stage = Path(review_handle.name)
            review_handle.write(review_payload)
        evidence_identities, review_identities = _written_identities(
            evidence_stage, review_stage
        )
        if evidence_identities != expected_identities or review_identities != expected_identities:
            raise InputError("written preparation output identity mismatch")
        for staged, target in (
            (evidence_stage, evidence_target),
            (review_stage, review_target),
        ):
            assert staged is not None
            os.link(staged, target)
            published.append((target, staged))
    except FileExistsError as exc:
        for target, staged in published:
            _unlink_if_staged_target(target, staged)
        raise OutputCollisionError(
            "refusing to overwrite an existing preparation output"
        ) from exc
    except (OSError, UnicodeError) as exc:
        for target, staged in published:
            _unlink_if_staged_target(target, staged)
        raise InputError(f"cannot write preparation files: {exc}") from exc
    except InputError:
        for target, staged in published:
            _unlink_if_staged_target(target, staged)
        raise
    finally:
        _unlink_quietly(evidence_stage)
        _unlink_quietly(review_stage)
