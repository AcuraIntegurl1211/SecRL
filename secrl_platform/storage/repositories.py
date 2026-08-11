from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.storage.orm import EvaluationTaskORM, utc_now


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


@dataclass(frozen=True)
class TaskRecord:
    id: str
    name: str
    status: str
    task_spec_json: str
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None

    @classmethod
    def from_orm(cls, task: EvaluationTaskORM) -> "TaskRecord":
        return cls(
            id=task.id,
            name=task.name,
            status=task.status,
            task_spec_json=task.task_spec_json,
            created_at=task.created_at,
            started_at=task.started_at,
            finished_at=task.finished_at,
        )


class TaskRepository:
    def __init__(self, session_factory: sessionmaker[Session]) -> None:
        self._session_factory = session_factory

    def create(self, task_spec: Mapping[str, Any]) -> TaskRecord:
        task = EvaluationTaskORM(
            name=str(task_spec.get("name", "Untitled evaluation")),
            task_spec_json=canonical_json(task_spec),
            status="QUEUED",
        )
        with self._session_factory.begin() as session:
            session.add(task)
            session.flush()
            return TaskRecord.from_orm(task)

    def claim_next(self) -> TaskRecord | None:
        with self._session_factory.begin() as session:
            running = session.scalar(
                select(EvaluationTaskORM.id).where(
                    EvaluationTaskORM.status == "RUNNING"
                )
            )
            if running is not None:
                return None
            task = session.scalar(
                select(EvaluationTaskORM)
                .where(EvaluationTaskORM.status == "QUEUED")
                .order_by(EvaluationTaskORM.created_at, EvaluationTaskORM.id)
                .limit(1)
            )
            if task is None:
                return None
            task.status = "RUNNING"
            task.started_at = utc_now()
            session.flush()
            return TaskRecord.from_orm(task)

    def finish(self, task_id: str, status: str) -> TaskRecord:
        if status not in {
            "SUCCEEDED",
            "FAILED",
            "BUDGET_EXHAUSTED",
            "CANCELED",
        }:
            raise ValueError(f"invalid terminal task status: {status}")
        with self._session_factory.begin() as session:
            task = session.get(EvaluationTaskORM, task_id)
            if task is None:
                raise KeyError(task_id)
            task.status = status
            task.finished_at = utc_now()
            session.flush()
            return TaskRecord.from_orm(task)
