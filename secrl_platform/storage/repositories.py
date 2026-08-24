from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

from sqlalchemy import select, update
from sqlalchemy.orm import Session, aliased, sessionmaker

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
        queued_task = aliased(EvaluationTaskORM)
        running_task = aliased(EvaluationTaskORM)
        next_task_id = (
            select(queued_task.id)
            .where(queued_task.status == "QUEUED")
            .order_by(queued_task.created_at, queued_task.id)
            .limit(1)
            .scalar_subquery()
        )
        running_exists = (
            select(running_task.id)
            .where(running_task.status == "RUNNING")
            .exists()
        )
        with self._session_factory.begin() as session:
            task = session.scalar(
                update(EvaluationTaskORM)
                .where(
                    EvaluationTaskORM.id == next_task_id,
                    EvaluationTaskORM.status == "QUEUED",
                    ~running_exists,
                )
                .values(status="RUNNING", started_at=utc_now())
                .returning(EvaluationTaskORM)
            )
            if task is None:
                return None
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
