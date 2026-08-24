"""Atomic, content-addressed backup and restore for the Lite data directory."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import tempfile
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from secrl_platform import __version__ as PLATFORM_VERSION

BACKUP_SCHEMA_VERSION = 1


class BackupIntegrityError(RuntimeError):
    """Raised when a backup cannot be proven safe to restore."""


@dataclass(frozen=True)
class BackupResult:
    backup_dir: Path
    database_sha256: str
    artifact_manifest_sha256: str
    platform_version: str


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _write_json(path: Path, payload: Any) -> str:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n").encode()
    path.write_bytes(encoded)
    return hashlib.sha256(encoded).hexdigest()


def _safe_relative(path: str) -> Path:
    relative = Path(path)
    if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
        raise BackupIntegrityError("backup path escapes its root")
    return relative


def _online_backup(source: Path, destination: Path) -> None:
    if not source.is_file():
        raise BackupIntegrityError("database file is missing")
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with sqlite3.connect(source) as source_db, sqlite3.connect(destination) as destination_db:
            source_db.backup(destination_db)
    except sqlite3.Error as exc:
        raise BackupIntegrityError("SQLite online backup failed") from exc


def create_backup(
    data_dir: Path,
    backup_dir: Path,
    *,
    platform_version: str = PLATFORM_VERSION,
) -> BackupResult:
    data_dir = Path(data_dir)
    backup_dir = Path(backup_dir)
    if backup_dir.exists() and any(backup_dir.iterdir()):
        raise BackupIntegrityError("backup target must be empty")
    backup_dir.mkdir(parents=True, exist_ok=True)
    database_source = data_dir / "secrl-lite.sqlite3"
    database_target = backup_dir / "secrl-lite.sqlite3"
    _online_backup(database_source, database_target)
    artifacts_source = data_dir / "artifacts"
    artifacts_target = backup_dir / "artifacts"
    if artifacts_source.exists():
        shutil.copytree(artifacts_source, artifacts_target)
    artifacts_target.mkdir(parents=True, exist_ok=True)
    entries: list[dict[str, Any]] = []
    for path in sorted(artifacts_target.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(artifacts_target).as_posix()
        _safe_relative(relative)
        entries.append({"path": relative, "sha256": _sha256(path), "size": path.stat().st_size})
    artifact_manifest_sha256 = _write_json(
        backup_dir / "artifact-manifest.json",
        {"schema_version": BACKUP_SCHEMA_VERSION, "artifacts": entries},
    )
    database_sha256 = _sha256(database_target)
    _write_json(
        backup_dir / "manifest.json",
        {
            "schema_version": BACKUP_SCHEMA_VERSION,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "database_sha256": database_sha256,
            "artifact_manifest_sha256": artifact_manifest_sha256,
            "platform_version": platform_version,
        },
    )
    return BackupResult(backup_dir, database_sha256, artifact_manifest_sha256, platform_version)


def _load_manifest(backup_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    if any((backup_dir / name).is_symlink() for name in ("manifest.json", "artifact-manifest.json")):
        raise BackupIntegrityError("backup manifests may not be symlinks")
    try:
        manifest = json.loads((backup_dir / "manifest.json").read_text())
        artifact_manifest_path = backup_dir / "artifact-manifest.json"
        artifact_manifest = json.loads(artifact_manifest_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BackupIntegrityError("backup manifest is missing or invalid") from exc
    if manifest.get("schema_version") != BACKUP_SCHEMA_VERSION or artifact_manifest.get("schema_version") != BACKUP_SCHEMA_VERSION:
        raise BackupIntegrityError("backup schema version is newer than this platform")
    if manifest.get("platform_version") != PLATFORM_VERSION:
        raise BackupIntegrityError("backup platform version is incompatible")
    if _sha256(backup_dir / "artifact-manifest.json") != manifest.get("artifact_manifest_sha256"):
        raise BackupIntegrityError("artifact manifest hash mismatch")
    return manifest, artifact_manifest


def _verify_backup(backup_dir: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    allowed_root = {"manifest.json", "artifact-manifest.json", "secrl-lite.sqlite3", "artifacts"}
    for child in backup_dir.iterdir():
        if child.name not in allowed_root or child.is_symlink():
            raise BackupIntegrityError("backup contains an unexpected root entry")
    manifest, artifact_manifest = _load_manifest(backup_dir)
    database = backup_dir / "secrl-lite.sqlite3"
    if not database.is_file() or database.is_symlink() or _sha256(database) != manifest.get("database_sha256"):
        raise BackupIntegrityError("database hash mismatch")
    artifacts_root = backup_dir / "artifacts"
    if artifacts_root.is_symlink() or not artifacts_root.is_dir():
        raise BackupIntegrityError("artifact root is invalid")
    expected_paths: set[Path] = set()
    for entry in artifact_manifest.get("artifacts", []):
        if not isinstance(entry, dict) or not isinstance(entry.get("path"), str):
            raise BackupIntegrityError("artifact manifest entry is invalid")
        relative = _safe_relative(entry["path"])
        if relative in expected_paths:
            raise BackupIntegrityError("artifact manifest contains duplicates")
        expected_paths.add(relative)
        path = artifacts_root / relative
        for parent in path.parents:
            if parent == artifacts_root:
                break
            if parent.is_symlink():
                raise BackupIntegrityError("artifact path contains a symlink")
        if not path.is_file() or path.is_symlink() or path.stat().st_size != entry.get("size") or _sha256(path) != entry.get("sha256"):
            raise BackupIntegrityError("artifact hash mismatch")
    actual_paths = {path.relative_to(artifacts_root) for path in artifacts_root.rglob("*") if path.is_file()}
    if actual_paths != expected_paths:
        raise BackupIntegrityError("backup contains unregistered artifacts")
    return manifest, artifact_manifest


def restore_backup(backup_dir: Path, target_dir: Path) -> BackupResult:
    backup_dir = Path(backup_dir)
    target_dir = Path(target_dir)
    if not backup_dir.is_dir():
        raise BackupIntegrityError("backup directory is missing")
    if target_dir.exists() and any(target_dir.iterdir()):
        raise BackupIntegrityError("restore target must be empty")
    manifest, _artifact_manifest = _verify_backup(backup_dir)
    parent = target_dir.parent
    parent.mkdir(parents=True, exist_ok=True)
    stage = parent / f".{target_dir.name}.restore-{uuid.uuid4().hex}"
    try:
        shutil.copytree(backup_dir, stage)
        _verify_backup(stage)
        if target_dir.exists():
            target_dir.rmdir()
        os.replace(stage, target_dir)
    except BackupIntegrityError:
        shutil.rmtree(stage, ignore_errors=True)
        raise
    except (OSError, shutil.Error) as exc:
        shutil.rmtree(stage, ignore_errors=True)
        raise BackupIntegrityError("restore failed without replacing target") from exc
    return BackupResult(
        target_dir,
        manifest["database_sha256"],
        manifest["artifact_manifest_sha256"],
        manifest["platform_version"],
    )
