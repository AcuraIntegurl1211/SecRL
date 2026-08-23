from __future__ import annotations

import hashlib
from pathlib import Path

from fastapi import APIRouter, Depends
from fastapi.responses import Response

from secrl_platform.api.dependencies import ApiContext, get_context, require_user
from secrl_platform.api.errors import ApiError
from secrl_platform.storage.artifacts import ArtifactRef
from secrl_platform.storage.orm import ArtifactORM, LocalUserORM


router = APIRouter(tags=["artifacts"])


@router.get("/artifacts/{id}/metadata")
def artifact_metadata(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    artifact, _ref = _authorized_artifact(context, id)
    return _metadata(artifact)


@router.get("/artifacts/{id}", response_class=Response)
def download_artifact(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> Response:
    artifact, ref = _authorized_artifact(context, id)
    try:
        content = ref.path.read_bytes()
    except OSError as exc:
        raise ApiError(
            409,
            "ARTIFACT_INTEGRITY_ERROR",
            "Artifact integrity verification failed",
        ) from exc
    if len(content) != ref.size or hashlib.sha256(content).hexdigest() != ref.sha256:
        raise ApiError(
            409,
            "ARTIFACT_INTEGRITY_ERROR",
            "Artifact integrity verification failed",
        )
    filename = f"{artifact.kind}-{artifact.sha256}.json"
    return Response(
        content=content,
        media_type=ref.media_type,
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _authorized_artifact(
    context: ApiContext,
    artifact_id: str,
) -> tuple[ArtifactORM, ArtifactRef]:
    with context.session_factory() as session:
        artifact = session.get(ArtifactORM, artifact_id)
        if artifact is None:
            raise ApiError(404, "ARTIFACT_NOT_FOUND", "Artifact was not found")
        if artifact.visibility != "PUBLIC":
            raise ApiError(403, "ARTIFACT_RESTRICTED", "Artifact is restricted")
        store_root = context.artifact_store.root
        resolved_root = store_root.resolve()
        expected = (
            Path("sha256")
            / artifact.sha256[:2]
            / artifact.sha256[2:4]
            / artifact.sha256
        )
        if Path(artifact.storage_key).parts != expected.parts:
            raise ApiError(
                409,
                "ARTIFACT_INTEGRITY_ERROR",
                "Artifact storage path is invalid",
            )
        path = store_root / artifact.storage_key
        resolved_path = path.resolve()
        if resolved_root not in resolved_path.parents:
            raise ApiError(
                409,
                "ARTIFACT_INTEGRITY_ERROR",
                "Artifact storage path is invalid",
            )
        ref = ArtifactRef(
            kind=artifact.kind,
            sha256=artifact.sha256,
            size=artifact.size_bytes,
            path=path,
            media_type="application/json",
        )
        return artifact, ref


def _metadata(artifact: ArtifactORM) -> dict:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "ref_type": artifact.ref_type,
        "ref_id": artifact.ref_id,
    }
