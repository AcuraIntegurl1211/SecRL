from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends
from sqlalchemy import select

from secrl_platform.api.dependencies import ApiContext, get_context, require_user
from secrl_platform.storage.orm import (
    CaseAttemptORM,
    EvaluationTaskORM,
    LocalUserORM,
    RunORM,
)


router = APIRouter(tags=["overview"])

_ACTIVE_STATUSES = {
    "QUEUED",
    "RUNNING",
    "PAUSED",
    "PAUSE_REQUESTED",
    "INTERRUPTED",
}


@router.get("/overview")
def get_overview(
    _user: LocalUserORM = Depends(require_user),
    context: ApiContext = Depends(get_context),
) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)
    with context.session_factory() as session:
        tasks = session.scalars(
            select(EvaluationTaskORM).order_by(
                EvaluationTaskORM.created_at, EvaluationTaskORM.id
            )
        ).all()
        active = sum(1 for task in tasks if task.status in _ACTIVE_STATUSES)
        recent_task_ids = {
            task.id
            for task in tasks
            if task.status == "SUCCEEDED"
            and task.finished_at is not None
            and task.finished_at >= cutoff
        }
        rewards: list[float] = []
        if recent_task_ids:
            rows = session.execute(
                select(CaseAttemptORM.metrics_json)
                .join(RunORM, RunORM.id == CaseAttemptORM.run_id)
                .where(
                    RunORM.task_id.in_(recent_task_ids),
                    CaseAttemptORM.status == "SUCCEEDED",
                )
            )
            for (metrics_json,) in rows:
                metrics = json.loads(metrics_json or "{}")
                reward = metrics.get("reward")
                if isinstance(reward, (int, float)):
                    rewards.append(float(reward))
        return {
            "active_tasks": active,
            "completed_runs_24h": len(recent_task_ids),
            "average_reward_24h": (
                sum(rewards) / len(rewards) if rewards else None
            ),
        }
