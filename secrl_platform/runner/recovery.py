from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.agents.protocol import AgentRevisionRef, UsageSnapshot
from secrl_platform.benchmarks.protocol import Scope
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.models.evaluator import official_secrl_profile
from secrl_platform.runner.state import RunStateMachine
from secrl_platform.storage.artifacts import ArtifactRef, LocalArtifactStore
from secrl_platform.storage.orm import (
    AgentRevisionORM,
    AppSettingORM,
    ArtifactORM,
    BenchmarkRevisionORM,
    CaseAttemptORM,
    CaseRecordORM,
    DatasetVersionORM,
    EvaluationTaskORM,
    RunORM,
    ScenarioORM,
    utc_now,
)
from secrl_platform.storage.repositories import canonical_json


@dataclass(frozen=True)
class RunHandle:
    task_id: str
    run_id: str


@dataclass(frozen=True)
class StoredCase:
    record_id: str
    external_id: str
    ordinal: int


@dataclass(frozen=True)
class AttemptHandle:
    id: str
    number: int


class RunLeaseHeld(RuntimeError):
    pass


class RunLeaseLost(RuntimeError):
    pass


class RunnerRepository:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        owner_id: str | None = None,
        now: Callable[[], int | float] = time.time,
        lease_seconds: int = 30,
    ) -> None:
        if lease_seconds <= 0:
            raise ValueError("runner lease duration must be positive")
        self._session_factory = session_factory
        self._owner_id = owner_id or str(uuid.uuid4())
        self._now = now
        self._lease_seconds = lease_seconds
        self._fences: dict[str, int] = {}

    @property
    def heartbeat_interval(self) -> float:
        return max(self._lease_seconds / 3, 0.1)

    def create_benchmark_run(
        self,
        *,
        name: str,
        adapter: Any,
        agent_revision: AgentRevisionRef,
        case_ids: tuple[str, ...] | None = None,
        budget: dict[str, Any] | None = None,
        model_config_revision_id: str | None = None,
        model_config_sha256: str | None = None,
        run_limits: dict[str, int] | None = None,
        agent_parameters: dict[str, Any] | None = None,
        selection: dict[str, Any] | None = None,
    ) -> RunHandle:
        if case_ids is not None and len(case_ids) != len(set(case_ids)):
            raise ValueError("runner task scope contains duplicate cases")
        scope = Scope(case_ids=case_ids) if case_ids is not None else Scope.all()
        cases = adapter.enumerate_cases(adapter.dataset_ref(), scope)
        if not cases:
            raise ValueError("runner task scope must include at least one case")
        if len({case.id for case in cases}) != len(cases):
            raise ValueError("runner task scope contains duplicate cases")
        frozen_limits = dict(
            run_limits
            or {"max_steps": 32, "max_str_len": 100_000, "max_entry_return": 15}
        )
        if set(frozen_limits) != {"max_steps", "max_str_len", "max_entry_return"}:
            raise ValueError("run limits are incomplete")
        if any(not isinstance(value, int) or value < 1 for value in frozen_limits.values()):
            raise ValueError("run limits must be positive integers")
        manifest = adapter.manifest()
        with self._session_factory.begin() as session:
            benchmark_sha256 = _sha256(
                {"manifest": manifest.model_dump(mode="json"), "kind": "benchmark"}
            )
            benchmark = session.scalar(
                select(BenchmarkRevisionORM).where(
                    BenchmarkRevisionORM.sha256 == benchmark_sha256
                )
            )
            if benchmark is None:
                benchmark = BenchmarkRevisionORM(
                    adapter_name=manifest.benchmark_id,
                    manifest_json=canonical_json(manifest.model_dump(mode="json")),
                    tool_schema_json=canonical_json(
                        [
                            tool.model_dump(mode="json")
                            for tool in adapter.tool_definitions()
                        ]
                    ),
                    evaluation_protocol_json=canonical_json(
                        {"protocol_version": manifest.protocol_version}
                    ),
                    sha256=benchmark_sha256,
                )
                session.add(benchmark)
                session.flush()
            dataset_ref = adapter.dataset_ref()
            dataset = session.scalar(
                select(DatasetVersionORM).where(
                    DatasetVersionORM.sha256 == dataset_ref.sha256
                )
            )
            if dataset is None:
                dataset = DatasetVersionORM(
                    benchmark_revision_id=benchmark.id,
                    name=dataset_ref.dataset_id,
                    manifest_json=canonical_json(
                        dataset_ref.model_dump(mode="json")
                    ),
                    split=str(getattr(manifest, "dataset_split", "all")),
                    status="PUBLISHED",
                    sha256=dataset_ref.sha256,
                )
                session.add(dataset)
                session.flush()
            elif dataset.benchmark_revision_id != benchmark.id:
                raise ValueError("dataset revision is bound to a different benchmark revision")
            scenarios: dict[str, ScenarioORM] = {}
            case_records: list[CaseRecordORM] = []
            for ordinal, case in enumerate(cases):
                scenario = scenarios.get(case.scenario.id)
                if scenario is None:
                    scenario = ScenarioORM(
                        dataset_version_id=dataset.id,
                        external_id=case.scenario.id,
                        metadata_json=canonical_json(case.scenario.metadata),
                    )
                    session.add(scenario)
                    session.flush()
                    scenarios[case.scenario.id] = scenario
                record = CaseRecordORM(
                    dataset_version_id=dataset.id,
                    scenario_id=scenario.id,
                    external_id=case.id,
                    ordinal=ordinal,
                    payload_json=canonical_json(case.model_dump(mode="json")),
                )
                session.add(record)
                case_records.append(record)
            session.flush()
            agent = session.scalar(
                select(AgentRevisionORM).where(
                    AgentRevisionORM.sha256 == agent_revision.manifest_sha256
                )
            )
            if agent is None:
                agent = AgentRevisionORM(
                    name=agent_revision.manifest.name,
                    kind=(
                        "BUILT_IN"
                        if agent_revision.manifest.runtime == "built_in"
                        else "SERVICE"
                    ),
                    manifest_json=canonical_json(
                        agent_revision.manifest.model_dump(mode="json")
                    ),
                    parameter_schema_json=canonical_json(
                        agent_revision.manifest.parameter_schema
                    ),
                    sha256=agent_revision.manifest_sha256,
                )
                session.add(agent)
                session.flush()
            frozen_budget = dict(budget or {})
            task_spec = {
                "name": name,
                "benchmark_id": manifest.benchmark_id,
                "dataset_sha256": dataset_ref.sha256,
                "agent_revision_id": agent_revision.id,
                "agent_revision_sha256": agent_revision.manifest_sha256,
                "case_ids": [case.id for case in cases],
                "case_record_ids": [record.id for record in case_records],
                "budget": frozen_budget,
                "limits": frozen_limits,
                "agent_parameters": dict(agent_parameters or {}),
            }
            if selection is not None:
                task_spec["selection"] = dict(selection)
            if model_config_revision_id is not None:
                if model_config_sha256 is None:
                    raise ValueError("model config hash is required")
                task_spec["model_config_revision_id"] = model_config_revision_id
                task_spec["model_config_sha256"] = model_config_sha256
            if manifest.benchmark_id == "secrl":
                task_spec["evaluator_profile"] = official_secrl_profile(
                    formal=True,
                    model_revision=model_config_sha256 or "static-evaluator-v1",
                ).model_dump(mode="json")
            task = EvaluationTaskORM(
                name=name,
                benchmark_revision_id=benchmark.id,
                dataset_version_id=dataset.id,
                agent_revision_id=agent.id,
                model_config_revision_id=model_config_revision_id,
                task_spec_json=canonical_json(task_spec),
                status="QUEUED",
                budget_json=canonical_json(frozen_budget),
            )
            session.add(task)
            session.flush()
            run_spec = {
                "task_spec": task_spec,
                "scenario_id": cases[0].scenario.id,
                "limits": frozen_limits,
            }
            run_spec_json = canonical_json(run_spec)
            run = RunORM(
                task_id=task.id,
                scenario_id=scenarios[cases[0].scenario.id].id,
                status="QUEUED",
                run_spec_json=run_spec_json,
                run_spec_sha256=hashlib.sha256(
                    run_spec_json.encode("utf-8")
                ).hexdigest(),
                next_case_index=0,
                pause_requested=False,
                cancel_requested=False,
            )
            session.add(run)
            session.flush()
            return RunHandle(task_id=task.id, run_id=run.id)

    def create_protocol_smoke_run(
        self,
        *,
        name: str,
        adapter: ProtocolSmokeAdapter,
        agent_revision: AgentRevisionRef,
        case_ids: tuple[str, ...] | None = None,
        budget: dict[str, Any] | None = None,
        model_config_revision_id: str | None = None,
        model_config_sha256: str | None = None,
    ) -> RunHandle:
        return self.create_benchmark_run(
            name=name,
            adapter=adapter,
            agent_revision=agent_revision,
            case_ids=case_ids,
            budget=budget,
            model_config_revision_id=model_config_revision_id,
            model_config_sha256=model_config_sha256,
            run_limits={"max_steps": 32, "max_str_len": 100_000, "max_entry_return": 15},
        )

    def run_limits(self, task_id: str, run_id: str) -> dict[str, int]:
        with self._session_factory() as session:
            _task, run = self._get_task_run(session, task_id, run_id)
            actual = hashlib.sha256(run.run_spec_json.encode("utf-8")).hexdigest()
            if actual != run.run_spec_sha256:
                raise ValueError("frozen RunSpec hash mismatch")
            payload = json.loads(run.run_spec_json)
        limits = payload.get("limits")
        if not isinstance(limits, dict) or set(limits) != {
            "max_steps",
            "max_str_len",
            "max_entry_return",
        }:
            raise ValueError("frozen RunSpec limits are invalid")
        if any(not isinstance(value, int) or value < 1 for value in limits.values()):
            raise ValueError("frozen RunSpec limits are invalid")
        return dict(limits)

    def prepare_for_run(self, task_id: str, run_id: str) -> str:
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            if task.status in {
                "SUCCEEDED",
                "BUDGET_EXHAUSTED",
                "CANCELED",
                "PAUSED",
            }:
                return task.status
            self._acquire_lease(session, run_id)
            interrupted = session.scalars(
                select(CaseAttemptORM).where(
                    CaseAttemptORM.run_id == run_id,
                    CaseAttemptORM.status == "RUNNING",
                )
            ).all()
            for attempt in interrupted:
                attempt.status = "FAILED"
                attempt.is_final = False
                attempt.error_json = canonical_json(
                    {"code": "RUNNER_INTERRUPTED", "retryable": True}
                )
            if task.status == "FAILED":
                _transition_task(task, "QUEUED")
                task.finished_at = None
            if task.status == "QUEUED":
                _transition_task(task, "RUNNING")
                task.started_at = task.started_at or utc_now()
            run.status = "RUNNING"
            return "RUNNING" if task.status == "PAUSE_REQUESTED" else task.status

    def cases(self, task_id: str, run_id: str) -> list[StoredCase]:
        with self._session_factory() as session:
            task, _run = self._get_task_run(session, task_id, run_id)
            task_spec = json.loads(task.task_spec_json)
            selected_ids = tuple(task_spec.get("case_ids", ()))
            record_ids = tuple(task_spec.get("case_record_ids", ()))
            scope = (
                CaseRecordORM.id.in_(record_ids)
                if record_ids
                else CaseRecordORM.external_id.in_(selected_ids)
            )
            records = session.scalars(
                select(CaseRecordORM)
                .where(
                    CaseRecordORM.dataset_version_id == task.dataset_version_id,
                    scope,
                )
                .order_by(CaseRecordORM.ordinal)
            ).all()
            if len(records) != len(selected_ids) or tuple(
                record.external_id for record in records
            ) != selected_ids:
                raise ValueError("frozen task Case selection does not match stored records")
            return [
                StoredCase(
                    record_id=record.id,
                    external_id=record.external_id,
                    ordinal=record.ordinal,
                )
                for record in records
            ]

    def checkpoint(self, task_id: str, run_id: str) -> int:
        with self._session_factory() as session:
            _task, run = self._get_task_run(session, task_id, run_id)
            return run.next_case_index

    def start_attempt(self, run_id: str, case_record_id: str) -> AttemptHandle:
        with self._session_factory.begin() as session:
            self._require_lease(session, run_id)
            number = (
                session.scalar(
                    select(func.count(CaseAttemptORM.id)).where(
                        CaseAttemptORM.run_id == run_id,
                        CaseAttemptORM.case_id == case_record_id,
                    )
                )
                or 0
            ) + 1
            attempt = CaseAttemptORM(
                run_id=run_id,
                case_id=case_record_id,
                attempt_no=number,
                status="RUNNING",
                is_final=False,
                metrics_json="{}",
                trajectory_summary_json="{}",
            )
            session.add(attempt)
            session.flush()
            return AttemptHandle(id=attempt.id, number=number)

    def commit_case(
        self,
        *,
        task_id: str,
        run_id: str,
        attempt_id: str,
        artifact: ArtifactRef,
        restricted_artifacts: tuple[ArtifactRef, ...] = (),
        result: dict[str, Any],
        usage: UsageSnapshot,
        budget_anchor: UsageSnapshot | None,
        budget_exhausted: bool,
        case_count: int,
    ) -> str:
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            self._require_lease(session, run_id)
            attempt = session.get(CaseAttemptORM, attempt_id)
            if attempt is None or attempt.run_id != run_id:
                raise KeyError(attempt_id)
            if attempt.status != "RUNNING":
                raise RuntimeError("case attempt is not active")
            storage_path = (
                Path("sha256")
                / artifact.sha256[:2]
                / artifact.sha256[2:4]
                / artifact.sha256
            )
            if tuple(artifact.path.parts[-4:]) != storage_path.parts:
                raise ValueError("artifact path is not canonical for its digest")
            storage_key = str(storage_path)
            session.add(
                ArtifactORM(
                    storage_key=storage_key,
                    kind=artifact.kind,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size,
                    ref_type="case_attempt",
                    ref_id=attempt.id,
                )
            )
            for restricted_artifact in restricted_artifacts:
                restricted_path = (
                    Path("sha256")
                    / restricted_artifact.sha256[:2]
                    / restricted_artifact.sha256[2:4]
                    / restricted_artifact.sha256
                )
                if tuple(restricted_artifact.path.parts[-4:]) != restricted_path.parts:
                    raise ValueError("restricted artifact path is not canonical for its digest")
                session.add(
                    ArtifactORM(
                        storage_key=str(restricted_path),
                        kind=restricted_artifact.kind,
                        sha256=restricted_artifact.sha256,
                        size_bytes=restricted_artifact.size,
                        ref_type="case_attempt",
                        ref_id=attempt.id,
                        visibility="RESTRICTED",
                    )
                )
            metrics = {
                **result,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "cached_tokens": usage.cached_tokens,
                "reasoning_tokens": usage.reasoning_tokens,
                "estimated_cost": str(usage.estimated_cost),
            }
            if budget_anchor is not None:
                metrics.update(
                    {
                        "model_ledger_tokens": budget_anchor.prompt_tokens,
                        "model_ledger_cost": str(budget_anchor.estimated_cost),
                    }
                )
            attempt.status = "SUCCEEDED"
            attempt.is_final = True
            attempt.metrics_json = canonical_json(metrics)
            attempt.trajectory_summary_json = canonical_json(
                {"artifact_sha256": artifact.sha256}
            )
            run.next_case_index += 1
            if run.cancel_requested:
                _transition_task(task, "CANCELED")
                run.status = "CANCELED"
                task.finished_at = utc_now()
            elif budget_exhausted:
                _transition_task(task, "BUDGET_EXHAUSTED")
                run.status = "FAILED"
                task.finished_at = utc_now()
            elif run.next_case_index >= case_count:
                _transition_task(task, "SUCCEEDED")
                run.status = "SUCCEEDED"
                task.finished_at = utc_now()
            elif self._budget_reached(session, task, run_id):
                _transition_task(task, "BUDGET_EXHAUSTED")
                run.status = "FAILED"
                task.finished_at = utc_now()
            elif task.status == "PAUSE_REQUESTED" or run.pause_requested:
                _transition_task(task, "PAUSED")
                run.status = "QUEUED"
                run.pause_requested = False
            if task.status != "RUNNING":
                self._release_lease(session, run_id)
            else:
                self._renew_lease(session, run_id)
            return task.status

    def request_pause(self, task_id: str, run_id: str) -> None:
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            if task.status != "RUNNING":
                raise ValueError("only a running task can be paused")
            _transition_task(task, "PAUSE_REQUESTED")
            run.pause_requested = True

    def resume(self, task_id: str, run_id: str) -> None:
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            if task.status != "PAUSED":
                raise ValueError("only a paused task can be resumed")
            _transition_task(task, "QUEUED")
            run.status = "QUEUED"

    def request_cancel(self, task_id: str, run_id: str) -> None:
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            if task.status in {"QUEUED", "PAUSED"}:
                _transition_task(task, "CANCELED")
                task.finished_at = utc_now()
                run.status = "CANCELED"
            elif task.status in {"RUNNING", "PAUSE_REQUESTED"}:
                run.cancel_requested = True
            else:
                raise ValueError("task cannot be canceled from its current state")

    def mark_budget_exhausted(self, task_id: str, run_id: str) -> str:
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            self._require_lease(session, run_id)
            _transition_task(task, "BUDGET_EXHAUSTED")
            task.finished_at = utc_now()
            run.status = "FAILED"
            self._release_lease(session, run_id)
            return task.status

    def fail_attempt(
        self,
        *,
        task_id: str,
        run_id: str,
        attempt_id: str,
        code: str,
        retryable: bool | None = None,
        details: dict[str, Any] | None = None,
    ) -> str:
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            self._require_lease(session, run_id)
            attempt = session.get(CaseAttemptORM, attempt_id)
            if attempt is None or attempt.run_id != run_id:
                raise KeyError(attempt_id)
            attempt.status = "FAILED"
            attempt.is_final = False
            error = {"code": code}
            if retryable is not None:
                error["retryable"] = retryable
            if details:
                error.update(
                    {
                        key: value
                        for key, value in details.items()
                        if key
                        in {
                            "usage_may_have_occurred",
                            "safe_to_retry",
                            "http_status",
                            "content_type",
                            "provider_request_id",
                            "request_id",
                            "response_shape",
                        }
                        and value is not None
                    }
                )
            attempt.error_json = canonical_json(error)
            _transition_task(task, "FAILED")
            task.finished_at = utc_now()
            run.status = "FAILED"
            self._release_lease(session, run_id)
            return task.status

    def fail_configuration(
        self,
        *,
        task_id: str,
        run_id: str,
        code: str,
    ) -> str:
        """Fail a queued run after safe, pre-execution configuration validation."""
        with self._session_factory.begin() as session:
            task, run = self._get_task_run(session, task_id, run_id)
            if task.status in {
                "SUCCEEDED",
                "BUDGET_EXHAUSTED",
                "CANCELED",
            }:
                return task.status

            task_spec: dict[str, Any] = {}
            try:
                parsed_task_spec = json.loads(task.task_spec_json)
                if isinstance(parsed_task_spec, dict):
                    task_spec = parsed_task_spec
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            run_spec: dict[str, Any] = {}
            try:
                parsed_run_spec = json.loads(run.run_spec_json)
                if isinstance(parsed_run_spec, dict):
                    run_spec = parsed_run_spec
            except (TypeError, ValueError, json.JSONDecodeError):
                pass
            run_task_spec = run_spec.get("task_spec")
            if not isinstance(run_task_spec, dict):
                run_task_spec = {}
            record_ids = tuple(
                task_spec.get("case_record_ids", ())
                or run_task_spec.get("case_record_ids", ())
            )
            if record_ids and session.get(CaseRecordORM, record_ids[0]) is not None:
                attempt_no = (
                    session.scalar(
                        select(func.count(CaseAttemptORM.id)).where(
                            CaseAttemptORM.run_id == run_id,
                            CaseAttemptORM.case_id == record_ids[0],
                        )
                    )
                    or 0
                ) + 1
                session.add(
                    CaseAttemptORM(
                        run_id=run_id,
                        case_id=record_ids[0],
                        attempt_no=attempt_no,
                        status="FAILED",
                        is_final=False,
                        error_json=canonical_json(
                            {"code": code, "retryable": False}
                        ),
                        metrics_json="{}",
                        trajectory_summary_json="{}",
                    )
                )
            if task.status != "FAILED":
                _transition_task(task, "FAILED")
            task.finished_at = utc_now()
            run.status = "FAILED"
            if run_id in self._fences:
                self._release_lease(session, run_id)
            return task.status

    def retry_attempt(
        self,
        *,
        run_id: str,
        attempt_id: str,
        code: str,
    ) -> None:
        with self._session_factory.begin() as session:
            self._require_lease(session, run_id)
            attempt = session.get(CaseAttemptORM, attempt_id)
            if attempt is None or attempt.run_id != run_id:
                raise KeyError(attempt_id)
            if attempt.status != "RUNNING":
                raise RuntimeError("case attempt is not active")
            attempt.status = "FAILED"
            attempt.is_final = False
            attempt.error_json = canonical_json(
                {"code": code, "retryable": True}
            )
            self._renew_lease(session, run_id)

    def heartbeat(self, run_id: str) -> None:
        with self._session_factory.begin() as session:
            self._renew_lease(session, run_id)

    def budget_reached(self, task_id: str, run_id: str) -> bool:
        with self._session_factory() as session:
            task, _run = self._get_task_run(session, task_id, run_id)
            return self._budget_reached(session, task, run_id)

    def budget_spec(self, task_id: str) -> dict[str, Any]:
        with self._session_factory() as session:
            task = session.get(EvaluationTaskORM, task_id)
            if task is None:
                raise KeyError(task_id)
            payload = json.loads(task.budget_json)
            if not isinstance(payload, dict):
                raise ValueError("task budget must be an object")
            return payload

    def model_budget_anchor(self, task_id: str, run_id: str) -> UsageSnapshot:
        with self._session_factory() as session:
            self._get_task_run(session, task_id, run_id)
            attempts = session.scalars(
                select(CaseAttemptORM).where(
                    CaseAttemptORM.run_id == run_id,
                    CaseAttemptORM.is_final.is_(True),
                )
            ).all()
            tokens = 0
            cost = Decimal(0)
            for attempt in attempts:
                metrics = json.loads(attempt.metrics_json)
                tokens = max(tokens, int(metrics.get("model_ledger_tokens", 0)))
                cost = max(
                    cost,
                    Decimal(str(metrics.get("model_ledger_cost", "0"))),
                )
            return UsageSnapshot(prompt_tokens=tokens, estimated_cost=cost)

    def task_status(self, task_id: str) -> str:
        with self._session_factory() as session:
            task = session.get(EvaluationTaskORM, task_id)
            if task is None:
                raise KeyError(task_id)
            return task.status

    def final_attempt_count(self, task_id: str, external_id: str) -> int:
        return self._attempt_count(task_id, external_id, final_only=True)

    def attempt_count(self, task_id: str, external_id: str) -> int:
        return self._attempt_count(task_id, external_id, final_only=False)

    def final_result_count(self, task_id: str) -> int:
        with self._session_factory() as session:
            return int(
                session.scalar(
                    select(func.count(CaseAttemptORM.id))
                    .join(RunORM, RunORM.id == CaseAttemptORM.run_id)
                    .where(RunORM.task_id == task_id, CaseAttemptORM.is_final.is_(True))
                )
                or 0
            )

    def attempt_errors(self, task_id: str) -> tuple[dict[str, Any], ...]:
        with self._session_factory() as session:
            attempts = session.scalars(
                select(CaseAttemptORM)
                .join(RunORM, RunORM.id == CaseAttemptORM.run_id)
                .where(
                    RunORM.task_id == task_id,
                    CaseAttemptORM.error_json.is_not(None),
                )
                .order_by(CaseAttemptORM.created_at, CaseAttemptORM.id)
            ).all()
            return tuple(json.loads(attempt.error_json) for attempt in attempts)

    def artifact_refs(
        self,
        task_id: str,
        store: LocalArtifactStore,
    ) -> list[ArtifactRef]:
        with self._session_factory() as session:
            artifacts = session.scalars(
                select(ArtifactORM)
                .join(CaseAttemptORM, CaseAttemptORM.id == ArtifactORM.ref_id)
                .join(RunORM, RunORM.id == CaseAttemptORM.run_id)
                .where(
                    RunORM.task_id == task_id,
                    ArtifactORM.ref_type == "case_attempt",
                )
                .order_by(ArtifactORM.created_at, ArtifactORM.id)
            ).all()
            return [
                ArtifactRef(
                    kind=artifact.kind,
                    sha256=artifact.sha256,
                    size=artifact.size_bytes,
                    path=store.root / artifact.storage_key,
                    media_type="application/json",
                )
                for artifact in artifacts
            ]

    def trajectory_payloads(
        self,
        task_id: str,
        store: LocalArtifactStore,
    ) -> list[dict[str, Any]]:
        payloads = []
        for ref in self.artifact_refs(task_id, store):
            store.verify(ref)
            payloads.append(json.loads(ref.path.read_text(encoding="utf-8")))
        return payloads

    def unreferenced_artifacts(self, store: LocalArtifactStore) -> tuple[Path, ...]:
        with self._session_factory() as session:
            referenced = set(session.scalars(select(ArtifactORM.storage_key)).all())
        files = (
            path
            for path in (store.root / "sha256").rglob("*")
            if path.is_file()
        )
        return tuple(
            sorted(
                path
                for path in files
                if str(path.relative_to(store.root)) not in referenced
            )
        )

    def _attempt_count(
        self,
        task_id: str,
        external_id: str,
        *,
        final_only: bool,
    ) -> int:
        with self._session_factory() as session:
            query = (
                select(func.count(CaseAttemptORM.id))
                .join(RunORM, RunORM.id == CaseAttemptORM.run_id)
                .join(CaseRecordORM, CaseRecordORM.id == CaseAttemptORM.case_id)
                .where(
                    RunORM.task_id == task_id,
                    CaseRecordORM.external_id == external_id,
                )
            )
            if final_only:
                query = query.where(CaseAttemptORM.is_final.is_(True))
            return int(session.scalar(query) or 0)

    def _acquire_lease(self, session: Session, run_id: str) -> int:
        key = _lease_key(run_id)
        now = float(self._now())
        initial_payload = canonical_json(
            {
                "expires_at": now + self._lease_seconds,
                "fence": 1,
                "owner_id": self._owner_id,
            }
        )
        inserted = session.execute(
            sqlite_insert(AppSettingORM)
            .values(
                id=str(uuid.uuid4()),
                key=key,
                value_json=initial_payload,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
            .on_conflict_do_nothing(index_elements=["key"])
        )
        if inserted.rowcount == 1:
            self._fences[run_id] = 1
            return 1
        lease = session.scalar(
            select(AppSettingORM).where(AppSettingORM.key == key)
        )
        if lease is None:
            raise RunLeaseHeld(f"run {run_id} lease could not be acquired")
        current = json.loads(lease.value_json)
        current_owner = current.get("owner_id")
        expires_at = float(current.get("expires_at", 0))
        if current_owner != self._owner_id and expires_at > now:
            raise RunLeaseHeld(f"run {run_id} is leased by another worker")
        current_fence = int(current.get("fence", 0))
        fence = (
            current_fence
            if current_owner == self._owner_id and expires_at > now
            else current_fence + 1
        )
        lease.value_json = canonical_json(
            {
                "expires_at": now + self._lease_seconds,
                "fence": fence,
                "owner_id": self._owner_id,
            }
        )
        self._fences[run_id] = fence
        session.flush()
        return fence

    def _renew_lease(self, session: Session, run_id: str) -> None:
        lease = self._require_lease(session, run_id)
        payload = json.loads(lease.value_json)
        payload["expires_at"] = float(self._now()) + self._lease_seconds
        lease.value_json = canonical_json(payload)

    def _require_lease(self, session: Session, run_id: str) -> AppSettingORM:
        fence = self._fences.get(run_id)
        lease = session.scalar(
            select(AppSettingORM).where(AppSettingORM.key == _lease_key(run_id))
        )
        if lease is None or fence is None:
            raise RunLeaseLost(f"run {run_id} lease is unavailable")
        payload = json.loads(lease.value_json)
        if (
            payload.get("owner_id") != self._owner_id
            or int(payload.get("fence", -1)) != fence
            or float(payload.get("expires_at", 0)) <= float(self._now())
        ):
            raise RunLeaseLost(f"run {run_id} lease was lost")
        return lease

    def _release_lease(self, session: Session, run_id: str) -> None:
        lease = self._require_lease(session, run_id)
        session.delete(lease)
        self._fences.pop(run_id, None)

    @staticmethod
    def _get_task_run(
        session: Session,
        task_id: str,
        run_id: str,
    ) -> tuple[EvaluationTaskORM, RunORM]:
        task = session.get(EvaluationTaskORM, task_id)
        run = session.get(RunORM, run_id)
        if task is None or run is None or run.task_id != task_id:
            raise KeyError((task_id, run_id))
        return task, run

    @staticmethod
    def _budget_reached(
        session: Session,
        task: EvaluationTaskORM,
        run_id: str,
    ) -> bool:
        budget = json.loads(task.budget_json)
        final_attempts = session.scalars(
            select(CaseAttemptORM).where(
                CaseAttemptORM.run_id == run_id,
                CaseAttemptORM.is_final.is_(True),
            )
        ).all()
        max_cases = budget.get("max_cases")
        if max_cases is not None and len(final_attempts) >= int(max_cases):
            return True
        tokens = 0
        cost = Decimal(0)
        for attempt in final_attempts:
            metrics = json.loads(attempt.metrics_json)
            tokens += int(metrics.get("prompt_tokens", 0)) + int(
                metrics.get("completion_tokens", 0)
            )
            cost += Decimal(str(metrics.get("estimated_cost", "0")))
        max_tokens = budget.get("max_tokens")
        if max_tokens is not None and tokens > 0 and tokens >= int(max_tokens):
            return True
        max_cost = budget.get("max_cost")
        return (
            max_cost is not None
            and cost > 0
            and cost >= Decimal(str(max_cost))
        )


def _sha256(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _lease_key(run_id: str) -> str:
    return f"runner.lease.{run_id}"


def _transition_task(task: EvaluationTaskORM, target: str) -> None:
    machine = RunStateMachine(task.status)
    machine.transition(target)
    task.status = machine.state
