from __future__ import annotations

import hashlib
import json

from fastapi import APIRouter, Depends
from sqlalchemy import select

from secrl_platform.agents.builtin import DeterministicSmokeAgent
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
from secrl_platform.storage.orm import EvaluationTaskORM, LocalUserORM


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
    revision = DeterministicSmokeAgent.revision()
    adapter = ProtocolSmokeAdapter.load_default()
    if payload.benchmark_id != adapter.manifest().benchmark_id:
        raise ApiError(422, "INVALID_TASK_SPEC", "Unknown benchmark revision")
    if payload.agent_revision_id != revision.id:
        raise ApiError(422, "INVALID_TASK_SPEC", "Unknown agent revision")
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
