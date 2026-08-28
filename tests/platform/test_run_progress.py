"""Live run progress and overview aggregation contract tests."""

from __future__ import annotations

import json
import tempfile
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path

from fastapi.testclient import TestClient
from sqlalchemy import select

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.api.app import create_app
from secrl_platform.auth.passwords import hash_password
from secrl_platform.config import Settings
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import (
    CaseAttemptORM,
    CaseRecordORM,
    EvaluationTaskORM,
    LocalUserORM,
    RunORM,
)


class _Harness(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.session_factory = create_engine_and_session(
            root / "platform.sqlite3", create=True
        )
        with self.session_factory.begin() as session:
            session.add(
                LocalUserORM(
                    username="admin",
                    password_hash=hash_password("correct horse battery staple"),
                    status="ACTIVE",
                )
            )
        self.artifact_store = LocalArtifactStore(root / "artifacts")
        self.settings = Settings(
            data_dir=root,
            master_key="00" * 32,
            session_secret="s" * 32,
            model_provider_allowlist=("models.invalid",),
        )
        self.app = create_app(
            settings=self.settings,
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        self.assertEqual(login.status_code, 200, login.text)

    def tearDown(self) -> None:
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def _create_run(self, budget: dict | None = None) -> tuple[str, str, list[str]]:
        from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter

        revision = DeterministicSmokeAgent.revision()
        handle = RunnerRepository(self.session_factory).create_benchmark_run(
            name="progress fixture",
            adapter=ProtocolSmokeAdapter.load_default(),
            agent_revision=revision,
            case_ids=("smoke-001",),
            budget=budget or {"max_tokens": 500000, "max_cost": "3"},
        )
        with self.session_factory() as session:
            run = session.get(RunORM, handle.run_id)
            task = session.get(EvaluationTaskORM, handle.task_id)
            rows = session.execute(
                select(CaseRecordORM.id)
                .where(CaseRecordORM.dataset_version_id == task.dataset_version_id)
                .order_by(CaseRecordORM.ordinal)
            ).scalars()
            case_ids = list(rows)
        return handle.task_id, handle.run_id, case_ids

    def _add_attempt(
        self,
        run_id: str,
        case_id: str,
        *,
        status: str,
        metrics: dict,
        attempt_no: int = 1,
    ) -> None:
        with self.session_factory.begin() as session:
            session.add(
                CaseAttemptORM(
                    id=str(uuid.uuid4()),
                    run_id=run_id,
                    case_id=case_id,
                    attempt_no=attempt_no,
                    status=status,
                    is_final=status == "SUCCEEDED",
                    error_json=None,
                    metrics_json=json.dumps(metrics),
                )
            )


class RunProgressTest(_Harness):
    def test_progress_aggregates_completed_attempts_and_budget(self):
        task_id, run_id, cases = self._create_run()
        self._add_attempt(
            run_id, cases[0], status="SUCCEEDED",
            metrics={
                "reward": 1.0, "correct": True, "steps": 4,
                "prompt_tokens": 1000, "completion_tokens": 200,
                "evaluator_prompt_tokens": 100, "evaluator_completion_tokens": 20,
                "estimated_cost": "0.01",
            },
        )
        self._add_attempt(
            run_id, cases[0], status="FAILED",
            metrics={}, attempt_no=2,
        )
        response = self.client.get(f"/api/v1/runs/{run_id}/progress")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["run_id"], run_id)
        self.assertEqual(payload["task_id"], task_id)
        self.assertEqual(payload["frozen_case_count"], 1)
        self.assertEqual(payload["completed"], 1)
        self.assertEqual(payload["failed"], 1)
        self.assertEqual(payload["correct"], 1)
        self.assertEqual(payload["reward_sum"], 1.0)
        self.assertEqual(payload["average_reward"], 1.0)
        self.assertEqual(payload["tokens"]["agent"], 1200)
        self.assertEqual(payload["tokens"]["evaluator"], 120)
        self.assertEqual(payload["tokens"]["total"], 1320)
        self.assertEqual(payload["estimated_cost"], "0.01")
        self.assertEqual(payload["budget"]["max_tokens"], 500000)
        self.assertEqual(payload["budget"]["max_cost"], "3")

    def test_progress_average_reward_is_null_without_scored_attempts(self):
        _task_id, run_id, cases = self._create_run()
        response = self.client.get(f"/api/v1/runs/{run_id}/progress")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["completed"], 0)
        self.assertIsNone(payload["average_reward"])
        self.assertIsNone(payload["reward_sum"])
        self.assertEqual(payload["tokens"]["total"], 0)

    def test_progress_unknown_run_returns_404(self):
        response = self.client.get("/api/v1/runs/missing-run/progress")
        self.assertEqual(response.status_code, 404, response.text)
        self.assertEqual(response.json()["error"]["code"], "RUN_NOT_FOUND")

    def test_progress_requires_authentication(self):
        anonymous = TestClient(self.app)
        with anonymous:
            response = anonymous.get("/api/v1/runs/whatever/progress")
        self.assertEqual(response.status_code, 401, response.text)


class OverviewTest(_Harness):
    def test_overview_counts_active_and_recent_rewards(self):
        task_id, run_id, cases = self._create_run()
        self._add_attempt(
            run_id, cases[0], status="SUCCEEDED",
            metrics={"reward": 0.5, "correct": False, "estimated_cost": "0.01"},
        )
        with self.session_factory.begin() as session:
            task = session.get(EvaluationTaskORM, task_id)
            task.status = "SUCCEEDED"
            task.finished_at = datetime.now(timezone.utc)
        response = self.client.get("/api/v1/overview")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertEqual(payload["active_tasks"], 0)
        self.assertEqual(payload["completed_runs_24h"], 1)
        self.assertEqual(payload["average_reward_24h"], 0.5)

    def test_overview_average_is_null_without_completed_runs(self):
        response = self.client.get("/api/v1/overview")
        self.assertEqual(response.status_code, 200, response.text)
        payload = response.json()
        self.assertIsNone(payload["average_reward_24h"])
        self.assertEqual(payload["completed_runs_24h"], 0)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
