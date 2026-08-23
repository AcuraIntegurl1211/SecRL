from __future__ import annotations

import json

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from secrl_platform.api.dependencies import (
    ApiContext,
    get_context,
    require_csrf_user,
    require_user,
)
from secrl_platform.api.errors import ApiError
from secrl_platform.api.schemas import ReviewCreateRequest
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.runner.state import RunStateMachine
from secrl_platform.storage.orm import (
    AttributionORM,
    CaseAttemptORM,
    CaseRecordORM,
    EvaluationTaskORM,
    LocalUserORM,
    RunORM,
)


router = APIRouter()


@router.get("/runs/{id}", tags=["runs"])
def get_run(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    with context.session_factory() as session:
        run = session.get(RunORM, id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Run was not found")
        task = session.get(EvaluationTaskORM, run.task_id)
        return _run_payload(run, task)


@router.post("/runs/{id}:pause", tags=["runs"])
def pause_run(
    id: str,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    task_id = _task_id(context, id)
    try:
        RunnerRepository(context.session_factory).request_pause(task_id, id)
    except ValueError as exc:
        raise ApiError(409, "RUN_STATE_CONFLICT", "Run cannot be paused") from exc
    return {"id": id, "status": "PAUSE_REQUESTED"}


@router.post("/runs/{id}:resume", tags=["runs"])
def resume_run(
    id: str,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    task_id = _task_id(context, id)
    try:
        RunnerRepository(context.session_factory).resume(task_id, id)
    except ValueError as exc:
        raise ApiError(409, "RUN_STATE_CONFLICT", "Run cannot be resumed") from exc
    return {"id": id, "status": "QUEUED"}


@router.post("/runs/{id}:cancel", tags=["runs"])
def cancel_run(
    id: str,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    task_id = _task_id(context, id)
    try:
        RunnerRepository(context.session_factory).request_cancel(task_id, id)
    except ValueError as exc:
        raise ApiError(409, "RUN_STATE_CONFLICT", "Run cannot be canceled") from exc
    return {"id": id, "status": RunnerRepository(context.session_factory).task_status(task_id)}


@router.get("/runs/{id}/cases", tags=["runs"])
def list_run_cases(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    _task_id(context, id)
    with context.session_factory() as session:
        rows = session.execute(
            select(CaseRecordORM, CaseAttemptORM)
            .join(CaseAttemptORM, CaseAttemptORM.case_id == CaseRecordORM.id)
            .where(CaseAttemptORM.run_id == id)
            .order_by(CaseRecordORM.ordinal, CaseAttemptORM.attempt_no)
        ).all()
        return [
            {
                "case_id": case.external_id,
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "is_final": attempt.is_final,
                "metrics": json.loads(attempt.metrics_json),
                "error": json.loads(attempt.error_json) if attempt.error_json else None,
            }
            for case, attempt in rows
        ]


@router.post("/runs/{id}/cases/{case_id}:retry", tags=["runs"])
def retry_case(
    id: str,
    case_id: str,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    with context.session_factory.begin() as session:
        run = session.get(RunORM, id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Run was not found")
        task = session.get(EvaluationTaskORM, run.task_id)
        case = session.scalar(
            select(CaseRecordORM).where(
                CaseRecordORM.scenario_id == run.scenario_id,
                CaseRecordORM.external_id == case_id,
            )
        )
        if task is None or case is None:
            raise ApiError(404, "CASE_NOT_FOUND", "Run case was not found")
        if task.status != "FAILED" or case.ordinal != run.next_case_index:
            raise ApiError(409, "CASE_RETRY_CONFLICT", "Case is not retryable")
        machine = RunStateMachine(task.status)
        machine.transition("QUEUED")
        task.status = machine.state
        task.finished_at = None
        run.status = "QUEUED"
        return {"run_id": id, "case_id": case_id, "status": "QUEUED"}


@router.post("/runs/{id}:analyze", tags=["analysis"])
def analyze_run(
    id: str,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> None:
    _task_id(context, id)
    raise ApiError(
        409,
        "MILESTONE_NOT_AVAILABLE",
        "Failure analysis is not available in Milestone 2",
    )


@router.get("/runs/{id}/analysis", tags=["analysis"])
def get_analysis(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> None:
    _task_id(context, id)
    raise ApiError(
        409,
        "MILESTONE_NOT_AVAILABLE",
        "Failure analysis is not available in Milestone 2",
    )


@router.post("/attributions/{id}/reviews", tags=["analysis"])
def create_review(
    id: str,
    _payload: ReviewCreateRequest,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> None:
    with context.session_factory() as session:
        if session.get(AttributionORM, id) is None:
            raise ApiError(404, "ATTRIBUTION_NOT_FOUND", "Attribution was not found")
    raise ApiError(
        409,
        "MILESTONE_NOT_AVAILABLE",
        "Attribution reviews are not available in Milestone 2",
    )


@router.get("/compare", tags=["compare"])
def compare(
    left: str = Query(...),
    right: str = Query(...),
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    with context.session_factory() as session:
        left_task = session.get(EvaluationTaskORM, left)
        right_task = session.get(EvaluationTaskORM, right)
        if left_task is None or right_task is None:
            raise ApiError(404, "TASK_NOT_FOUND", "Comparison task was not found")
        if left_task.benchmark_revision_id != right_task.benchmark_revision_id:
            raise ApiError(
                409,
                "BENCHMARK_REVISION_MISMATCH",
                "Comparison requires the same Benchmark revision",
            )
        return {
            "left": {"id": left_task.id, "status": left_task.status},
            "right": {"id": right_task.id, "status": right_task.status},
        }


def _task_id(context: ApiContext, run_id: str) -> str:
    with context.session_factory() as session:
        run = session.get(RunORM, run_id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Run was not found")
        return run.task_id


def _run_payload(run: RunORM, task: EvaluationTaskORM | None) -> dict:
    return {
        "id": run.id,
        "task_id": run.task_id,
        "status": task.status if task is not None else run.status,
        "checkpoint": run.next_case_index,
        "run_spec_sha256": run.run_spec_sha256,
    }
