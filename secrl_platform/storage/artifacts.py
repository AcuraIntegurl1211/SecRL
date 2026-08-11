from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path


class ArtifactIntegrityError(RuntimeError):
    pass


@dataclass(frozen=True)
class ArtifactRef:
    kind: str
    sha256: str
    size: int
    path: Path
    media_type: str


class LocalArtifactStore:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    def put_bytes(
        self,
        kind: str,
        content: bytes,
        *,
        media_type: str = "application/octet-stream",
    ) -> ArtifactRef:
        expected_digest = hashlib.sha256(content).hexdigest()
        target = self._path_for(expected_digest)
        target.parent.mkdir(parents=True, exist_ok=True)

        temporary_path: Path | None = None
        try:
            hasher = hashlib.sha256()
            with tempfile.NamedTemporaryFile(
                dir=target.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                temporary.write(content)
                hasher.update(content)
                temporary.flush()
                os.fsync(temporary.fileno())
            digest = hasher.hexdigest()
            if digest != expected_digest:
                raise ArtifactIntegrityError("artifact digest changed while writing")
            os.replace(temporary_path, target)
            temporary_path = None
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

        return ArtifactRef(
            kind=kind,
            sha256=expected_digest,
            size=len(content),
            path=target,
            media_type=media_type,
        )

    def verify(self, ref: ArtifactRef) -> bool:
        expected_path = self._path_for(ref.sha256)
        if ref.path != expected_path:
            raise ArtifactIntegrityError("artifact path does not match its digest")
        try:
            content = ref.path.read_bytes()
        except OSError as error:
            raise ArtifactIntegrityError("artifact cannot be read") from error
        actual_digest = hashlib.sha256(content).hexdigest()
        if actual_digest != ref.sha256 or len(content) != ref.size:
            raise ArtifactIntegrityError("artifact content failed integrity verification")
        return True

    def _path_for(self, digest: str) -> Path:
        if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
            raise ArtifactIntegrityError("artifact digest must be lowercase SHA-256")
        return self.root / "sha256" / digest[:2] / digest[2:4] / digest


def verify_all_artifacts(root: Path | None = None) -> bool:
    if root is None:
        data_dir = Path(os.environ.get("SECRL_DATA_DIR", "/data"))
        root = Path(os.environ.get("SECRL_ARTIFACT_DIR", data_dir / "artifacts"))
    sha_root = Path(root) / "sha256"
    if not sha_root.exists():
        return True

    try:
        for path in sha_root.rglob("*"):
            if not path.is_file():
                continue
            digest = path.name
            expected_path = sha_root / digest[:2] / digest[2:4] / digest
            if path != expected_path:
                return False
            hasher = hashlib.sha256()
            size = 0
            with path.open("rb") as artifact_file:
                for chunk in iter(lambda: artifact_file.read(1024 * 1024), b""):
                    hasher.update(chunk)
                    size += len(chunk)
            ref = ArtifactRef(
                kind="unknown",
                sha256=digest,
                size=size,
                path=path,
                media_type="application/octet-stream",
            )
            LocalArtifactStore(Path(root)).verify(ref)
    except (ArtifactIntegrityError, OSError):
        return False
    return True
