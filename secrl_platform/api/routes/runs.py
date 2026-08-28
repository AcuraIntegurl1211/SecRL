from __future__ import annotations

import json
from decimal import Decimal

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select

from secrl_platform.api.dependencies import (
    ApiContext,
    get_context,
    require_csrf_user,
    require_user,
)
from secrl_platform.api.errors import ApiError
from secrl_platform.api.routes.artifacts import read_authorized_artifact
from secrl_platform.api.schemas import ReviewCreateRequest
from secrl_platform.analysis.service import (
    AnalysisRunRepository,
    HumanReviewRepository,
    PersistentReviewRecord,
    RegisteredAnalysisRun,
    analyze_completed_run,
)
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.runner.state import RunStateMachine
from secrl_platform.storage.orm import (
    ArtifactORM,
    AttributionORM,
    AuditEventORM,
    CaseAttemptORM,
    CaseRecordORM,
    EvaluationTaskORM,
    HumanReviewORM,
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
        failed_attempt = session.scalar(
            select(CaseAttemptORM)
            .where(
                CaseAttemptORM.run_id == id,
                CaseAttemptORM.status == "FAILED",
            )
            .order_by(CaseAttemptORM.created_at.desc(), CaseAttemptORM.id.desc())
            .limit(1)
        )
        return _run_payload(run, task, failed_attempt)


@router.get("/runs/{id}/progress", tags=["runs"])
def get_run_progress(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    with context.session_factory() as session:
        run = session.get(RunORM, id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Run was not found")
        task = session.get(EvaluationTaskORM, run.task_id)
        if task is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Run was not found")
        attempts = session.scalars(
            select(CaseAttemptORM)
            .where(CaseAttemptORM.run_id == id)
            .order_by(CaseAttemptORM.created_at, CaseAttemptORM.id)
        ).all()
        spec = json.loads(task.task_spec_json)
        budget = json.loads(task.budget_json)
        completed = 0
        failed = 0
        correct = 0
        rewards: list[float] = []
        agent_tokens = 0
        evaluator_tokens = 0
        cost = Decimal("0")
        for attempt in attempts:
            if attempt.status == "SUCCEEDED":
                completed += 1
            elif attempt.status == "FAILED":
                failed += 1
            if attempt.status != "SUCCEEDED":
                continue
            metrics = json.loads(attempt.metrics_json or "{}")
            if metrics.get("correct") is True:
                correct += 1
            reward = metrics.get("reward")
            if isinstance(reward, (int, float)):
                rewards.append(float(reward))
            agent_tokens += int(metrics.get("prompt_tokens", 0) or 0) + int(
                metrics.get("completion_tokens", 0) or 0
            )
            evaluator_tokens += int(
                metrics.get("evaluator_prompt_tokens", 0) or 0
            ) + int(metrics.get("evaluator_completion_tokens", 0) or 0)
            cost += Decimal(str(metrics.get("estimated_cost", "0") or "0")) + Decimal(
                str(metrics.get("evaluator_estimated_cost", "0") or "0")
            )
        elapsed_seconds = None
        if task.started_at is not None:
            from datetime import datetime, timezone

            end = task.finished_at or datetime.now(timezone.utc)
            elapsed_seconds = max(0.0, (end - task.started_at).total_seconds())
        return {
            "run_id": run.id,
            "task_id": task.id,
            "task_status": task.status,
            "frozen_case_count": int(spec.get("case_count", 0) or 0),
            "completed": completed,
            "failed": failed,
            "correct": correct,
            "reward_sum": sum(rewards) if rewards else None,
            "average_reward": (sum(rewards) / len(rewards)) if rewards else None,
            "tokens": {
                "agent": agent_tokens,
                "evaluator": evaluator_tokens,
                "total": agent_tokens + evaluator_tokens,
            },
            "estimated_cost": str(cost),
            "budget": {
                "max_tokens": budget.get("max_tokens"),
                "max_cost": budget.get("max_cost"),
                "max_cases": budget.get("max_cases"),
            },
            "elapsed_seconds": elapsed_seconds,
            "current_case_index": run.next_case_index,
        }


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
        artifacts = {
            artifact.ref_id: artifact
            for artifact in session.scalars(
                select(ArtifactORM).where(
                    ArtifactORM.ref_id.in_([attempt.id for _case, attempt in rows]),
                    ArtifactORM.ref_type == "case_attempt",
                    ArtifactORM.kind == "trajectory",
                    ArtifactORM.visibility == "PUBLIC",
                )
            ).all()
        }
        return [
            {
                "case_id": case.external_id,
                "attempt_id": attempt.id,
                "attempt_no": attempt.attempt_no,
                "status": attempt.status,
                "is_final": attempt.is_final,
                "metrics": json.loads(attempt.metrics_json),
                "error": json.loads(attempt.error_json) if attempt.error_json else None,
                "trajectory_artifact": (
                    _artifact_payload(artifacts[attempt.id])
                    if attempt.id in artifacts
                    else None
                ),
            }
            for case, attempt in rows
        ]


@router.get("/runs/{id}/cases/{case_id}/trajectory", tags=["runs"])
def get_trajectory_step(
    id: str,
    case_id: str,
    step: int = Query(..., ge=0),
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    _task_id(context, id)
    with context.session_factory() as session:
        row = session.execute(
            select(CaseRecordORM, CaseAttemptORM)
            .join(CaseAttemptORM, CaseAttemptORM.case_id == CaseRecordORM.id)
            .where(
                CaseAttemptORM.run_id == id,
                CaseRecordORM.external_id == case_id,
                CaseAttemptORM.is_final.is_(True),
            )
            .order_by(CaseAttemptORM.attempt_no.desc())
            .limit(1)
        ).first()
        if row is None:
            raise ApiError(404, "CASE_NOT_FOUND", "Run case was not found")
        case, attempt = row
        artifact = session.scalar(
            select(ArtifactORM).where(
                ArtifactORM.ref_type == "case_attempt",
                ArtifactORM.ref_id == attempt.id,
                ArtifactORM.kind == "trajectory",
                ArtifactORM.visibility == "PUBLIC",
            )
        )
        if artifact is None:
            raise ApiError(404, "TRAJECTORY_NOT_FOUND", "Trajectory was not found")
        artifact_id = artifact.id
    artifact, content = read_authorized_artifact(context, artifact_id)
    try:
        payload = json.loads(content)
        exchanges = payload["exchanges"]
        exchange = exchanges[step]
    except (IndexError, KeyError, TypeError):
        raise ApiError(
            416,
            "TRAJECTORY_STEP_OUT_OF_RANGE",
            "Trajectory step is out of range",
        )
    except json.JSONDecodeError as exc:
        raise ApiError(
            409,
            "ARTIFACT_INTEGRITY_ERROR",
            "Trajectory artifact is invalid",
        ) from exc
    return {
        "case_id": case.external_id,
        "attempt_id": attempt.id,
        "artifact_id": artifact.id,
        "artifact_sha256": artifact.sha256,
        "step": step,
        "total_steps": len(exchanges),
        "exchange": exchange,
    }


@router.get("/runs/{id}/artifacts", tags=["runs"])
def list_run_artifacts(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    _task_id(context, id)
    with context.session_factory() as session:
        attempt_ids = select(CaseAttemptORM.id).where(CaseAttemptORM.run_id == id)
        artifacts = session.scalars(
            select(ArtifactORM)
            .where(
                ArtifactORM.ref_type == "case_attempt",
                ArtifactORM.ref_id.in_(attempt_ids),
                ArtifactORM.visibility == "PUBLIC",
            )
            .order_by(ArtifactORM.created_at, ArtifactORM.id)
        ).all()
        return [_artifact_payload(artifact) for artifact in artifacts]


@router.get("/runs/{id}/attributions", tags=["analysis"])
def list_run_attributions(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    _task_id(context, id)
    with context.session_factory() as session:
        rows = session.execute(
            select(AttributionORM, CaseRecordORM.external_id)
            .join(CaseAttemptORM, CaseAttemptORM.id == AttributionORM.case_attempt_id)
            .join(CaseRecordORM, CaseRecordORM.id == CaseAttemptORM.case_id)
            .where(CaseAttemptORM.run_id == id)
            .order_by(CaseRecordORM.ordinal, AttributionORM.id)
        ).all()
        return [
            {
                "id": attribution.id,
                "case_attempt_id": attribution.case_attempt_id,
                "case_id": case_id,
                "taxonomy": attribution.taxonomy,
                "label": attribution.label,
                "confidence": attribution.confidence,
                "evidence": json.loads(attribution.evidence_json),
            }
            for attribution, case_id in rows
        ]


@router.get("/runs/{id}/audit", tags=["runs"])
def list_run_audit(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    _task_id(context, id)
    with context.session_factory() as session:
        attribution_ids = select(AttributionORM.id).join(
            CaseAttemptORM,
            CaseAttemptORM.id == AttributionORM.case_attempt_id,
        ).where(CaseAttemptORM.run_id == id)
        review_ids = select(HumanReviewORM.id).where(
            HumanReviewORM.attribution_id.in_(attribution_ids)
        )
        events = session.scalars(
            select(AuditEventORM)
            .where(AuditEventORM.entity_id.in_(review_ids))
            .order_by(AuditEventORM.created_at, AuditEventORM.id)
        ).all()
        return [
            {
                "id": event.id,
                "created_at": event.created_at.isoformat(),
                "actor_user_id": event.actor_user_id,
                "action": event.action,
                "entity_type": event.entity_type,
                "entity_id": event.entity_id,
                "payload": json.loads(event.payload_json),
            }
            for event in events
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
        if task is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Run task was not found")
        task_spec = json.loads(task.task_spec_json)
        frozen_case_ids = tuple(task_spec.get("case_ids", ()))
        frozen_record_ids = tuple(task_spec.get("case_record_ids", ()))
        case_scope = (
            CaseRecordORM.id.in_(frozen_record_ids)
            if frozen_record_ids
            else CaseRecordORM.scenario_id == run.scenario_id
        )
        case = session.scalar(
            select(CaseRecordORM).where(
                CaseRecordORM.dataset_version_id == task.dataset_version_id,
                case_scope,
                CaseRecordORM.external_id == case_id,
            )
        )
        if case is None or case.external_id not in frozen_case_ids:
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
) -> dict:
    _task_id(context, id)
    try:
        record = analyze_completed_run(
            run_id=id,
            session_factory=context.session_factory,
            artifact_store=context.artifact_store,
        )
    except KeyError as exc:
        raise ApiError(404, "RUN_NOT_FOUND", "Run was not found") from exc
    except ValueError as exc:
        raise ApiError(409, "ANALYSIS_NOT_READY", "Run cannot be analyzed") from exc
    except Exception as exc:
        raise ApiError(500, "ANALYSIS_FAILED", "Failure analysis failed") from exc
    return _analysis_payload(record)


@router.get("/runs/{id}/analysis", tags=["analysis"])
def get_analysis(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    _task_id(context, id)
    return [
        _analysis_payload(record)
        for record in AnalysisRunRepository(
            context.session_factory,
            context.artifact_store,
        ).history(id)
    ]


@router.post("/attributions/{id}/reviews", tags=["analysis"], status_code=201)
def create_review(
    id: str,
    payload: ReviewCreateRequest,
    user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    try:
        review = HumanReviewRepository(context.session_factory).submit(
            attribution_id=id,
            reviewer_user_id=user.id,
            primary=payload.primary,
            secondary=payload.secondary,
            confidence=payload.confidence,
            evidence=payload.evidence,
            notes=payload.notes,
        )
    except KeyError as exc:
        raise ApiError(404, "ATTRIBUTION_NOT_FOUND", "Attribution was not found") from exc
    except ValueError as exc:
        raise ApiError(422, "INVALID_REVIEW", "Human review is invalid") from exc
    return _review_payload(review)


@router.get("/attributions/{id}/reviews", tags=["analysis"])
def list_reviews(
    id: str,
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> list[dict]:
    with context.session_factory() as session:
        if session.get(AttributionORM, id) is None:
            raise ApiError(404, "ATTRIBUTION_NOT_FOUND", "Attribution was not found")
    return [
        _review_payload(review)
        for review in HumanReviewRepository(context.session_factory).history(id)
    ]


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
        if left_task.dataset_version_id != right_task.dataset_version_id:
            raise ApiError(
                409,
                "DATASET_REVISION_MISMATCH",
                "Comparison requires the same Dataset revision",
            )
        terminal = {"SUCCEEDED", "FAILED", "BUDGET_EXHAUSTED", "CANCELED"}
        if left_task.status not in terminal or right_task.status not in terminal:
            raise ApiError(
                409,
                "TASK_NOT_COMPLETED",
                "Comparison requires completed tasks",
            )
        return {
            "revision": {
                "benchmark_revision_id": left_task.benchmark_revision_id,
                "dataset_version_id": left_task.dataset_version_id,
            },
            "left": _comparison_payload(session, left_task),
            "right": _comparison_payload(session, right_task),
        }


def _comparison_payload(session, task: EvaluationTaskORM) -> dict:
    attempts = session.scalars(
        select(CaseAttemptORM)
        .join(RunORM, RunORM.id == CaseAttemptORM.run_id)
        .where(
            RunORM.task_id == task.id,
            CaseAttemptORM.is_final.is_(True),
        )
        .order_by(CaseAttemptORM.created_at, CaseAttemptORM.id)
    ).all()
    metrics = [json.loads(attempt.metrics_json) for attempt in attempts]
    case_count = len(metrics)
    success_count = sum(bool(item.get("correct", False)) for item in metrics)
    rewards = [float(item["reward"]) for item in metrics if "reward" in item]
    steps = [float(item["steps"]) for item in metrics if "steps" in item]
    token_cost_available = task.model_config_revision_id is not None
    tokens = None
    estimated_cost = None
    if token_cost_available:
        tokens = sum(
            int(item.get("prompt_tokens", 0))
            + int(item.get("completion_tokens", 0))
            + int(item.get("evaluator_prompt_tokens", 0))
            + int(item.get("evaluator_completion_tokens", 0))
            for item in metrics
        )
        estimated_cost = str(
            sum(
                (
                    Decimal(str(item.get("estimated_cost", "0")))
                    + Decimal(str(item.get("evaluator_estimated_cost", "0")))
                    for item in metrics
                ),
                Decimal("0"),
            )
        )
    duration_seconds = None
    if task.started_at is not None and task.finished_at is not None:
        duration_seconds = max(
            0.0,
            (task.finished_at - task.started_at).total_seconds(),
        )
    return {
        "id": task.id,
        "status": task.status,
        "benchmark_revision_id": task.benchmark_revision_id,
        "dataset_version_id": task.dataset_version_id,
        "metrics": {
            "case_count": case_count,
            "success_count": success_count,
            "success_rate": success_count / case_count if case_count else None,
            "average_reward": sum(rewards) / len(rewards) if rewards else None,
            "average_steps": sum(steps) / len(steps) if steps else None,
            "tokens": tokens,
            "estimated_cost": estimated_cost,
            "token_cost_available": token_cost_available,
            "duration_seconds": duration_seconds,
        },
    }


def _task_id(context: ApiContext, run_id: str) -> str:
    with context.session_factory() as session:
        run = session.get(RunORM, run_id)
        if run is None:
            raise ApiError(404, "RUN_NOT_FOUND", "Run was not found")
        return run.task_id


def _run_payload(
    run: RunORM,
    task: EvaluationTaskORM | None,
    failed_attempt: CaseAttemptORM | None = None,
) -> dict:
    payload = {
        "id": run.id,
        "task_id": run.task_id,
        "status": task.status if task is not None else run.status,
        "checkpoint": run.next_case_index,
        "run_spec_sha256": run.run_spec_sha256,
    }
    if failed_attempt is not None and failed_attempt.error_json:
        payload["failure"] = json.loads(failed_attempt.error_json)
    return payload


def _artifact_payload(artifact: ArtifactORM) -> dict:
    return {
        "id": artifact.id,
        "kind": artifact.kind,
        "sha256": artifact.sha256,
        "size_bytes": artifact.size_bytes,
        "ref_type": artifact.ref_type,
        "ref_id": artifact.ref_id,
        "download_url": f"/api/v1/artifacts/{artifact.id}",
    }


def _review_payload(review: PersistentReviewRecord) -> dict:
    return {
        "id": review.id,
        "attribution_id": review.attribution_id,
        "revision": review.revision,
        "prior_review_id": review.prior_review_id,
        "reviewer_user_id": review.reviewer_user_id,
        "primary": review.primary,
        "secondary": list(review.secondary),
        "confidence": review.confidence,
        "evidence": list(review.evidence),
        "notes": review.notes,
    }


def _analysis_payload(record: RegisteredAnalysisRun) -> dict:
    return {
        "id": record.id,
        "run_id": record.run_id,
        "revision": record.revision,
        "taxonomy_version": record.taxonomy_version,
        "input_manifest_sha256": record.input_manifest_sha256,
        "output_manifest_sha256": record.output_manifest_sha256,
        "manifest_artifact_id": record.manifest_artifact_id,
        "artifact_visibility": "RESTRICTED",
    }
