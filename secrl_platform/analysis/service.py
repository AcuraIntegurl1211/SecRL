"""Versioned failure-analysis execution and append-only human review."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


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
