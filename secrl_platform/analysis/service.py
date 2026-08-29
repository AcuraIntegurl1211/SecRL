"""Versioned failure-analysis execution and append-only human review."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.storage.orm import (
    AnalysisRunORM,
    ArtifactORM,
    AttributionORM,
    AuditEventORM,
    CaseAttemptORM,
    HumanReviewORM,
    BenchmarkRevisionORM,
    CaseRecordORM,
    EvaluationTaskORM,
    RunORM,
)
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.repositories import canonical_json


class FailureAnalysisError(RuntimeError):
    pass


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True)
class ArtifactRef:
    path: Path
    sha256: str
    size: int


@dataclass(frozen=True)
class FrozenAnalysisInputs:
    paths: Mapping[str, Path]
    hashes: Mapping[str, str]

    def verify(self) -> Mapping[str, str]:
        for name, path in self.paths.items():
            if not path.is_file():
                raise ValueError(f"analysis input is missing: {name}")
            actual = _sha256(path)
            if actual != self.hashes[name]:
                raise ValueError(f"analysis input hash mismatch: {name}")
        return self.hashes


@dataclass(frozen=True)
class AnalysisInputs:
    agent_json: Path
    env_json: Path
    question_json: Path
    taxonomy: Path
    manifest: Path | None = None

    def paths(self) -> dict[str, Path]:
        values = {
            "agent": Path(self.agent_json),
            "env": Path(self.env_json),
            "question": Path(self.question_json),
            "taxonomy": Path(self.taxonomy),
        }
        if self.manifest is not None:
            values["manifest"] = Path(self.manifest)
        return values

    def freeze(self) -> FrozenAnalysisInputs:
        paths = self.paths()
        for name, path in paths.items():
            if not path.is_file():
                raise ValueError(f"analysis input is missing: {name}")
        return FrozenAnalysisInputs(
            paths=MappingProxyType(dict(paths)),
            hashes=MappingProxyType({name: _sha256(path) for name, path in paths.items()}),
        )


@dataclass(frozen=True)
class AnalysisRun:
    incident: str
    outputs: tuple[ArtifactRef, ...]
    manifest: ArtifactRef
    stdout: ArtifactRef
    stderr: ArtifactRef


@dataclass(frozen=True)
class ReviewRecord:
    attribution_id: str
    revision: int
    prior_revision: int | None
    reviewer_user_id: str
    primary: str
    secondary: tuple[str, ...]
    confidence: str
    evidence: tuple[str, ...]
    notes: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "secondary", tuple(self.secondary))
        object.__setattr__(self, "evidence", tuple(self.evidence))


class HumanReviewStore:
    """Small append-only review store used by the platform repository layer."""

    def __init__(self) -> None:
        self._records: dict[str, list[ReviewRecord]] = {}

    def append(self, record: ReviewRecord) -> ReviewRecord:
        if record.revision < 1:
            raise ValueError("review revision must be positive")
        history = self._records.setdefault(record.attribution_id, [])
        expected = len(history) + 1
        prior = history[-1].revision if history else None
        if record.revision != expected or record.prior_revision != prior:
            raise ValueError("human review revisions must be append-only")
        if not record.reviewer_user_id or not record.primary:
            raise ValueError("reviewer and primary label are required")
        history.append(record)
        return record

    def history(self, attribution_id: str) -> tuple[ReviewRecord, ...]:
        return tuple(self._records.get(attribution_id, ()))


@dataclass(frozen=True)
class PersistentReviewRecord:
    id: str
    attribution_id: str
    revision: int
    prior_review_id: str | None
    reviewer_user_id: str
    primary: str
    secondary: tuple[str, ...]
    confidence: str
    evidence: tuple[str, ...]
    notes: str


class HumanReviewRepository:
    """SQLite-backed append-only review history with one audit event per revision."""

    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def submit(
        self,
        *,
        attribution_id: str,
        reviewer_user_id: str | None,
        primary: str,
        secondary: tuple[str, ...],
        confidence: str,
        evidence: tuple[str, ...],
        notes: str,
    ) -> PersistentReviewRecord:
        if not reviewer_user_id or not primary:
            raise ValueError("reviewer and primary label are required")
        if confidence not in {"low", "medium", "high"}:
            raise ValueError("review confidence is invalid")
        with self._session_factory.begin() as session:
            if session.get(AttributionORM, attribution_id) is None:
                raise KeyError(attribution_id)
            prior = session.scalar(
                select(HumanReviewORM)
                .where(HumanReviewORM.attribution_id == attribution_id)
                .order_by(HumanReviewORM.revision.desc())
                .limit(1)
            )
            review = HumanReviewORM(
                attribution_id=attribution_id,
                revision=1 if prior is None else prior.revision + 1,
                prior_review_id=None if prior is None else prior.id,
                label=primary,
                secondary_json=canonical_json(list(secondary)),
                confidence=confidence,
                evidence_json=canonical_json(list(evidence)),
                notes=notes,
                reviewer_user_id=reviewer_user_id,
            )
            session.add(review)
            session.flush()
            session.add(
                AuditEventORM(
                    actor_user_id=reviewer_user_id,
                    action="human_review.append",
                    entity_type="human_review",
                    entity_id=review.id,
                    payload_json=canonical_json(
                        {
                            "attribution_id": attribution_id,
                            "revision": review.revision,
                            "prior_review_id": review.prior_review_id,
                        }
                    ),
                )
            )
            return _persistent_review(review)

    def history(self, attribution_id: str) -> tuple[PersistentReviewRecord, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(HumanReviewORM)
                .where(HumanReviewORM.attribution_id == attribution_id)
                .order_by(HumanReviewORM.revision)
            ).all()
            return tuple(_persistent_review(row) for row in rows)


def _persistent_review(row: HumanReviewORM) -> PersistentReviewRecord:
    if row.reviewer_user_id is None:
        raise ValueError("persisted HumanReview is missing its reviewer")
    return PersistentReviewRecord(
        id=row.id,
        attribution_id=row.attribution_id,
        revision=row.revision,
        prior_review_id=row.prior_review_id,
        reviewer_user_id=row.reviewer_user_id,
        primary=row.label,
        secondary=tuple(json.loads(row.secondary_json)),
        confidence=row.confidence,
        evidence=tuple(json.loads(row.evidence_json)),
        notes=row.notes,
    )


@dataclass(frozen=True)
class RegisteredAnalysisRun:
    id: str
    run_id: str
    revision: int
    taxonomy_version: str
    input_manifest_sha256: str
    output_manifest_sha256: str
    manifest_artifact_id: str


class AnalysisRunRepository:
    """Register verified analysis outputs and sanitized attribution summaries."""

    def __init__(
        self,
        session_factory: sessionmaker[Session],
        artifact_store: LocalArtifactStore,
    ) -> None:
        self._session_factory = session_factory
        self._artifact_store = artifact_store

    def register(
        self,
        *,
        run_id: str,
        analysis: AnalysisRun,
        case_attempt_ids: tuple[str, ...],
    ) -> RegisteredAnalysisRun:
        manifest_payload = json.loads(analysis.manifest.path.read_text(encoding="utf-8"))
        inputs = manifest_payload.get("inputs")
        if not isinstance(inputs, Mapping):
            raise ValueError("analysis service manifest is missing inputs")
        input_manifest_sha256 = hashlib.sha256(
            canonical_json(inputs).encode("utf-8")
        ).hexdigest()
        stored = []
        for source in (*analysis.outputs, analysis.manifest):
            ref = self._artifact_store.put_bytes(
                "restricted-analysis",
                source.path.read_bytes(),
                media_type="application/json",
            )
            self._artifact_store.verify(ref)
            stored.append((source.path.name, ref))
        attribution_source = next(
            (source for source in analysis.outputs if source.path.name.endswith("_attribution.jsonl")),
            None,
        )
        if attribution_source is None:
            raise ValueError("analysis attribution JSONL is missing")
        rows = [
            json.loads(line)
            for line in attribution_source.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        if len(rows) != len(case_attempt_ids):
            raise ValueError("analysis attribution count does not match final attempts")
        with self._session_factory.begin() as session:
            prior_revision = session.scalar(
                select(AnalysisRunORM.revision)
                .where(AnalysisRunORM.run_id == run_id)
                .order_by(AnalysisRunORM.revision.desc())
                .limit(1)
            )
            analysis_id = str(uuid.uuid4())
            artifact_rows: dict[str, ArtifactORM] = {}
            for filename, ref in stored:
                storage_key = str(ref.path.relative_to(self._artifact_store.root))
                artifact = session.scalar(
                    select(ArtifactORM).where(ArtifactORM.storage_key == storage_key)
                )
                if artifact is None:
                    artifact = ArtifactORM(
                        storage_key=storage_key,
                        kind=ref.kind,
                        sha256=ref.sha256,
                        size_bytes=ref.size,
                        ref_type="analysis_run",
                        ref_id=analysis_id,
                        visibility="RESTRICTED",
                    )
                    session.add(artifact)
                    session.flush()
                elif artifact.visibility != "RESTRICTED":
                    artifact.visibility = "RESTRICTED"
                artifact_rows[filename] = artifact
            manifest_artifact = artifact_rows[analysis.manifest.path.name]
            record = AnalysisRunORM(
                id=analysis_id,
                run_id=run_id,
                revision=(prior_revision or 0) + 1,
                taxonomy_version=str(manifest_payload.get("taxonomy_version", "")),
                input_manifest_sha256=input_manifest_sha256,
                output_manifest_sha256=analysis.manifest.sha256,
                manifest_artifact_id=manifest_artifact.id,
            )
            session.add(record)
            for attempt_id, payload in zip(case_attempt_ids, rows, strict=True):
                if session.get(CaseAttemptORM, attempt_id) is None:
                    raise KeyError(attempt_id)
                confidence = {"high": 0.9, "medium": 0.6, "low": 0.3}.get(
                    str(payload.get("confidence", "")).lower(),
                    0.0,
                )
                session.add(
                    AttributionORM(
                        case_attempt_id=attempt_id,
                        taxonomy=str(payload.get("taxonomy_version", "taxonomy_v1")),
                        label=str(payload.get("candidate_primary", "UNKNOWN")),
                        confidence=confidence,
                        explanation=str(payload.get("explanation", "")),
                        evidence_json=canonical_json(payload.get("evidence", [])),
                    )
                )
            session.flush()
            return _registered_analysis(record)

    def history(self, run_id: str) -> tuple[RegisteredAnalysisRun, ...]:
        with self._session_factory() as session:
            rows = session.scalars(
                select(AnalysisRunORM)
                .where(AnalysisRunORM.run_id == run_id)
                .order_by(AnalysisRunORM.revision)
            ).all()
            return tuple(_registered_analysis(row) for row in rows)


def _registered_analysis(row: AnalysisRunORM) -> RegisteredAnalysisRun:
    return RegisteredAnalysisRun(
        id=row.id,
        run_id=row.run_id,
        revision=row.revision,
        taxonomy_version=row.taxonomy_version,
        input_manifest_sha256=row.input_manifest_sha256,
        output_manifest_sha256=row.output_manifest_sha256,
        manifest_artifact_id=row.manifest_artifact_id,
    )


def analyze_completed_run(
    *,
    run_id: str,
    session_factory: sessionmaker[Session],
    artifact_store: LocalArtifactStore,
    python_executable: str | None = None,
) -> RegisteredAnalysisRun:
    """Materialize one completed SecRL run into the frozen analyzer and register it."""
    from secrl_platform.benchmarks.secrl import SecRLAdapter
    from secrl_platform.storage.artifacts import ArtifactRef as StoredArtifactRef

    with session_factory() as session:
        run = session.get(RunORM, run_id)
        if run is None:
            raise KeyError(run_id)
        task = session.get(EvaluationTaskORM, run.task_id)
        benchmark = (
            session.get(BenchmarkRevisionORM, task.benchmark_revision_id)
            if task is not None and task.benchmark_revision_id is not None
            else None
        )
        if task is None or task.status != "SUCCEEDED":
            raise ValueError("analysis requires a completed run")
        if benchmark is None or benchmark.adapter_name != "secrl":
            raise ValueError("failure analysis supports SecRL runs only")
        rows = session.execute(
            select(CaseRecordORM, CaseAttemptORM, ArtifactORM)
            .join(CaseAttemptORM, CaseAttemptORM.case_id == CaseRecordORM.id)
            .join(
                ArtifactORM,
                (ArtifactORM.ref_type == "case_attempt")
                & (ArtifactORM.ref_id == CaseAttemptORM.id),
            )
            .where(
                CaseAttemptORM.run_id == run_id,
                CaseAttemptORM.is_final.is_(True),
                CaseAttemptORM.status == "SUCCEEDED",
                ArtifactORM.kind == "trajectory",
            )
            .order_by(CaseRecordORM.ordinal)
        ).all()
        run_spec = json.loads(run.run_spec_json)
    if not rows:
        raise ValueError("completed run has no final attempts")
    adapter = SecRLAdapter()
    access = adapter.restricted_access()
    incident = rows[0][0].external_id.split(":", 1)[0]
    if any(case.external_id.split(":", 1)[0] != incident for case, _attempt, _artifact in rows):
        raise ValueError("analysis run mixes Incident revisions")
    agent_records: list[dict[str, Any]] = []
    env_records: list[dict[str, Any]] = []
    question_records: list[dict[str, Any]] = []
    attempt_ids: list[str] = []
    for case, attempt, artifact in rows:
        path = artifact_store.root / artifact.storage_key
        ref = StoredArtifactRef(
            kind=artifact.kind,
            sha256=artifact.sha256,
            size=artifact.size_bytes,
            path=path,
            media_type="application/json",
        )
        artifact_store.verify(ref)
        trajectory = json.loads(path.read_text(encoding="utf-8"))
        source = adapter.read_source_artifact(case.external_id, access)
        metrics = json.loads(attempt.metrics_json)
        legacy_trajectory = _legacy_trajectory(trajectory, float(metrics.get("reward", 0.0)))
        agent_records.append(
            {
                "nodes": source.get("nodes", []),
                "question_dict": source,
                "reward": metrics.get("reward", 0.0),
                "trials": {},
            }
        )
        env_records.append(
            {
                "nodes": source.get("nodes", []),
                "question": source,
                "reward": metrics.get("reward", 0.0),
                "trajectory": legacy_trajectory,
            }
        )
        question_records.append(source)
        attempt_ids.append(attempt.id)
    repository_root = Path(__file__).resolve().parents[2]
    taxonomy = repository_root / "experiments" / "failure_analysis" / "taxonomy_v1.json"
    max_steps = int(run_spec["limits"]["max_steps"])
    with tempfile.TemporaryDirectory(prefix="secrl-analysis-") as directory:
        root = Path(directory)
        paths = {}
        for name, payload in (
            ("agent", agent_records),
            ("env", env_records),
            ("question", question_records),
        ):
            path = root / f"{name}.json"
            path.write_text(canonical_json(payload), encoding="utf-8")
            paths[name] = path
        materialized = FailureAnalysisService(
            python_executable=python_executable,
            repository_root=repository_root,
        ).materialize_inputs(
            AnalysisInputs(
                agent_json=paths["agent"],
                env_json=paths["env"],
                question_json=paths["question"],
                taxonomy=taxonomy,
            ),
            root / "inputs",
        )
        analysis = FailureAnalysisService(
            python_executable=python_executable,
            repository_root=repository_root,
        ).run(
            materialized,
            incident=incident,
            output_dir=root / "output",
            max_steps=max_steps,
        )
        return AnalysisRunRepository(session_factory, artifact_store).register(
            run_id=run_id,
            analysis=analysis,
            case_attempt_ids=tuple(attempt_ids),
        )


def _legacy_trajectory(trajectory: Mapping[str, Any], reward: float) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for exchange in trajectory.get("exchanges", []):
        action = exchange.get("action", {})
        observation = exchange.get("result_observation", {})
        is_submit = action.get("type") == "submit"
        if is_submit:
            action_text = str(action.get("answer", ""))
        else:
            action_text = str(action.get("arguments", {}).get("query", action))
        content = observation.get("content", {})
        converted.append(
            {
                "action": action_text,
                "observation": "" if is_submit else str(content.get("result", content)),
                "reward": reward if is_submit else 0.0,
                "done": bool(is_submit or observation.get("terminal", False)),
                "info": {
                    "query_success": bool(content.get("query_success", True)),
                    "submit": is_submit,
                    **({"submitted_answer": action_text, "reward": reward} if is_submit else {}),
                },
            }
        )
    return converted


class FailureAnalysisService:
    """Run the checked-in offline analyzer with hash-verified read-only inputs."""

    def __init__(self, *, python_executable: str | None = None, repository_root: Path | None = None) -> None:
        self.python_executable = python_executable or sys.executable
        self.repository_root = Path(repository_root or Path(__file__).resolve().parents[2])

    def materialize_inputs(self, source: AnalysisInputs, destination: Path) -> AnalysisInputs:
        frozen = source.freeze()
        destination = Path(destination)
        if destination.exists():
            raise ValueError(f"materialized input directory already exists: {destination}")
        destination.mkdir(parents=True)
        names = {"agent": "agent.json", "env": "env.json", "question": "question.json", "taxonomy": "taxonomy_v1.json"}
        copied: dict[str, Path] = {}
        for key, path in frozen.paths.items():
            if key == "manifest":
                continue
            target = destination / names[key]
            shutil.copyfile(path, target)
            os.chmod(target, 0o444)
            copied[key] = target
        manifest_path = destination / "input_manifest.json"
        manifest_path.write_text(
            json.dumps(
                {"schema_version": "analysis_input_manifest_v1", "inputs": {key: {"filename": path.name, "sha256": _sha256(path)} for key, path in sorted(copied.items())}},
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        os.chmod(manifest_path, 0o444)
        return AnalysisInputs(
            agent_json=copied["agent"],
            env_json=copied["env"],
            question_json=copied["question"],
            taxonomy=copied["taxonomy"],
            manifest=manifest_path,
        )

    def run(
        self,
        inputs: AnalysisInputs,
        *,
        incident: str,
        output_dir: Path,
        max_steps: int,
        review_csv: Path | None = None,
        timeout_seconds: int = 120,
    ) -> AnalysisRun:
        if not incident.strip() or max_steps < 1:
            raise ValueError("incident and max_steps are required")
        frozen = inputs.freeze()
        frozen.verify()
        try:
            taxonomy_payload = json.loads(Path(inputs.taxonomy).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("taxonomy input is not valid JSON") from exc
        if not isinstance(taxonomy_payload, Mapping) or taxonomy_payload.get("taxonomy_version") != "taxonomy_v1":
            raise ValueError("taxonomy input must be the frozen taxonomy_v1 definition")
        if inputs.manifest is not None:
            try:
                input_manifest = json.loads(Path(inputs.manifest).read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("analysis input manifest is not valid JSON") from exc
            declared = input_manifest.get("inputs", {}) if isinstance(input_manifest, Mapping) else {}
            for key in ("agent", "env", "question", "taxonomy"):
                if declared.get(key, {}).get("sha256") != frozen.hashes[key]:
                    raise ValueError(f"analysis input manifest hash mismatch: {key}")
        if review_csv is not None:
            if not Path(review_csv).is_file():
                raise ValueError("review CSV is missing")
            review_csv = Path(review_csv)
        output_dir = Path(output_dir)
        if output_dir.exists():
            raise ValueError("analysis output path already exists")
        output_dir.parent.mkdir(parents=True, exist_ok=True)
        command = [
            self.python_executable,
            "-m",
            "experiments.failure_analysis.analyze_failures",
            "--agent-json",
            str(inputs.agent_json),
            "--env-json",
            str(inputs.env_json),
            "--question-json",
            str(inputs.question_json),
            "--incident",
            incident,
            "--output-dir",
            str(output_dir),
            "--taxonomy",
            str(inputs.taxonomy),
            "--max-steps",
            str(max_steps),
        ]
        if review_csv is not None:
            command.extend(["--review-csv", str(review_csv)])
        try:
            completed = subprocess.run(
                command,
                cwd=self.repository_root,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise FailureAnalysisError("failure analysis process could not be started") from exc
        if completed.returncode != 0:
            raise FailureAnalysisError(f"failure analysis process failed with code {completed.returncode}")
        if not output_dir.is_dir():
            raise FailureAnalysisError("failure analysis produced no output directory")

        stdout_path = output_dir / "process_stdout.txt"
        stderr_path = output_dir / "process_stderr.txt"
        stdout_path.write_text(completed.stdout or "", encoding="utf-8")
        stderr_path.write_text(completed.stderr or "", encoding="utf-8")
        os.chmod(stdout_path, 0o440)
        os.chmod(stderr_path, 0o440)

        cli_manifests = sorted(output_dir.glob("*_analysis_manifest.json"))
        if len(cli_manifests) != 1:
            raise FailureAnalysisError("failure analysis output manifest is missing or ambiguous")
        cli_manifest = json.loads(cli_manifests[0].read_text(encoding="utf-8"))
        source_manifest = cli_manifest.get("sources", {})
        for key in ("agent", "env", "question"):
            expected = frozen.hashes[key]
            if source_manifest.get(key, {}).get("sha256") != expected:
                raise FailureAnalysisError(f"failure analysis source hash mismatch: {key}")
        for filename, metadata in cli_manifest.get("outputs", {}).items():
            path = output_dir / filename
            if not path.is_file() or _sha256(path) != metadata.get("sha256"):
                raise FailureAnalysisError(f"failure analysis output hash mismatch: {filename}")
        output_refs: list[ArtifactRef] = []
        for path in sorted(output_dir.iterdir()):
            if path.is_file() and path.name != "service_manifest.json":
                output_refs.append(ArtifactRef(path=path, sha256=_sha256(path), size=path.stat().st_size))
        service_manifest_path = output_dir / "service_manifest.json"
        service_manifest_path.write_text(
            json.dumps(
                {
                    "schema_version": "analysis_service_manifest_v1",
                    "taxonomy_version": "taxonomy_v1",
                    "incident": incident,
                    "max_steps": max_steps,
                    "inputs": dict(frozen.hashes),
                    "outputs": {ref.path.name: {"sha256": ref.sha256, "size": ref.size} for ref in output_refs},
                },
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        manifest_ref = ArtifactRef(path=service_manifest_path, sha256=_sha256(service_manifest_path), size=service_manifest_path.stat().st_size)
        stdout_ref = next(ref for ref in output_refs if ref.path == stdout_path)
        stderr_ref = next(ref for ref in output_refs if ref.path == stderr_path)
        return AnalysisRun(
            incident=incident,
            outputs=tuple(output_refs),
            manifest=manifest_ref,
            stdout=stdout_ref,
            stderr=stderr_ref,
        )
