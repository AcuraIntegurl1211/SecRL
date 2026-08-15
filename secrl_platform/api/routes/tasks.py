from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.protocol import AgentManifest, AgentRevisionRef
from secrl_platform.api.dependencies import (
    ApiContext,
    get_context,
    require_csrf_user,
    require_user,
)
from secrl_platform.api.errors import ApiError
from secrl_platform.api.schemas import TaskCreateRequest, TaskCreateResponse
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.orm import (
    AgentRevisionORM,
    EvaluationTaskORM,
    LocalUserORM,
)


router = APIRouter(tags=["tasks"])


@router.get("/tasks")
def list_tasks(
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    with context.session_factory() as session:
        tasks = session.scalars(
            select(EvaluationTaskORM).order_by(
                EvaluationTaskORM.created_at,
                EvaluationTaskORM.id,
            )
        ).all()
        return [
            {
                "id": task.id,
                "name": task.name,
                "status": task.status,
                "task_spec": json.loads(task.task_spec_json),
                "task_spec_sha256": hashlib.sha256(
                    task.task_spec_json.encode("utf-8")
                ).hexdigest(),
            }
            for task in tasks
        ]


@router.post("/tasks", response_model=TaskCreateResponse, status_code=201)
def create_task(
    payload: TaskCreateRequest,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> TaskCreateResponse:
    adapter = ProtocolSmokeAdapter.load_default()
    if payload.benchmark_id != adapter.manifest().benchmark_id:
        raise ApiError(422, "INVALID_TASK_SPEC", "Unknown benchmark revision")
    revision = _resolve_agent_revision(context, payload.agent_revision_id)
    budget = payload.budget.model_dump(mode="json", exclude_none=True)
    try:
        handle = RunnerRepository(context.session_factory).create_protocol_smoke_run(
            name=payload.name,
            adapter=adapter,
            agent_revision=revision,
            case_ids=payload.case_ids,
            budget=budget,
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApiError(422, "INVALID_TASK_SPEC", "Invalid Protocol-Smoke task") from exc
    with context.session_factory() as session:
        task = session.get(EvaluationTaskORM, handle.task_id)
        if task is None:
            raise ApiError(500, "INTERNAL_ERROR", "Task was not persisted")
        task_hash = hashlib.sha256(task.task_spec_json.encode("utf-8")).hexdigest()
        status = task.status
    return TaskCreateResponse(
        id=handle.task_id,
        run_id=handle.run_id,
        status=status,
        task_spec_sha256=task_hash,
    )


def _resolve_agent_revision(
    context: ApiContext,
    revision_id: str,
) -> AgentRevisionRef:
    builtin = DeterministicSmokeAgent.revision()
    if revision_id == builtin.id:
        return builtin
    with context.session_factory() as session:
        stored = session.get(AgentRevisionORM, revision_id)
        if stored is None or stored.kind != "SERVICE":
            raise ApiError(422, "INVALID_TASK_SPEC", "Unknown agent revision")
        manifest_json = stored.manifest_json
        digest = stored.sha256
        endpoint = stored.service_endpoint
        service_manifest_sha256 = stored.service_manifest_sha256
    if endpoint is None or service_manifest_sha256 is None:
        raise ApiError(422, "INVALID_TASK_SPEC", "Invalid Agent Service revision")
    try:
        manifest = AgentManifest.model_validate_json(manifest_json)
        return AgentRevisionRef(
            id=manifest.agent_id,
            manifest=manifest,
            manifest_sha256=digest,
        )
    except ValueError as exc:
        raise ApiError(
            422,
            "INVALID_TASK_SPEC",
            "Invalid Agent Service revision",
        ) from exc
