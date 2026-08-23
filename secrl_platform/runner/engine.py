from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from secrl_platform.agents.capabilities import (
    CapabilityBudgetError,
    CapabilitySigner,
)
from secrl_platform.agents.protocol import AgentRuntime, EpisodeContext, UsageSnapshot
from secrl_platform.agents.service import AgentServiceError
from secrl_platform.benchmarks.protocol import (
    EvaluationResult,
    Scope,
    Submission,
    SubmitAction,
    ToolCallAction,
)
from secrl_platform.models.providers import ProviderError
from secrl_platform.runner.recovery import RunnerRepository, StoredCase
from secrl_platform.storage.artifacts import (
    ArtifactIntegrityError,
    ArtifactRef,
    LocalArtifactStore,
)
from secrl_platform.storage.repositories import canonical_json


MAX_PLATFORM_ATTEMPTS = 3


class RunnerEngine:
    def __init__(
        self,
        *,
        repository: RunnerRepository,
        artifact_store: LocalArtifactStore,
        adapter: Any,
        runtime_factory: Callable[[], AgentRuntime],
        after_artifact_write: Callable[[str, ArtifactRef], None] | None = None,
        model_budget_guard: "CapabilityBudgetGuard | None" = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._adapter = adapter
        self._runtime_factory = runtime_factory
        self._after_artifact_write = after_artifact_write
        self._model_budget_guard = model_budget_guard

    async def run(self, task_id: str, run_id: str) -> str:
        status = self._repository.prepare_for_run(task_id, run_id)
        if status != "RUNNING":
            return status
        cases = self._repository.cases(task_id, run_id)
        task_budget = self._repository.budget_spec(task_id)
        run_limits = self._repository.run_limits(task_id, run_id)
        case_by_id = {
            case.id: case
            for case in self._adapter.enumerate_cases(
                self._adapter.dataset_ref(),
                Scope(case_ids=tuple(case.external_id for case in cases)),
            )
        }
        while self._repository.checkpoint(task_id, run_id) < len(cases):
            self._repository.heartbeat(run_id)
            if self._repository.budget_reached(task_id, run_id):
                return self._repository.mark_budget_exhausted(task_id, run_id)
            index = self._repository.checkpoint(task_id, run_id)
            stored_case = cases[index]
            attempt = self._repository.start_attempt(run_id, stored_case.record_id)
            try:
                runtime = self._runtime_factory()
                model_access = getattr(runtime, "model_access", None)
                if model_access not in {"none", "platform_gateway"}:
                    raise RuntimeError("agent runtime model access is not declared")
                guard = self._model_budget_guard if model_access == "platform_gateway" else None
                if model_access == "platform_gateway" and guard is None:
                    raise RuntimeError("platform model gateway budget guard is required")
                if guard is not None:
                    guard.validate(run_id=run_id, task_budget=task_budget)
                    guard.validate_runtime(runtime)
                    guard.validate_anchor(
                        self._repository.model_budget_anchor(task_id, run_id)
                    )
                budget_baseline = guard.usage() if guard is not None else None
                trajectory, result, usage, budget_exhausted = await self._run_case(
                    run_id=run_id,
                    stored_case=stored_case,
                    case=case_by_id[stored_case.external_id],
                    attempt_id=attempt.id,
                    runtime=runtime,
                    budget_guard=guard,
                    budget_baseline=budget_baseline,
                    max_steps=run_limits["max_steps"],
                )
                budget_anchor = guard.usage() if guard is not None else None
            except (AgentServiceError, ProviderError) as exc:
                safely_retryable = exc.transient and not (
                    isinstance(exc, ProviderError) and exc.usage_may_have_occurred
                )
                if safely_retryable and attempt.number < MAX_PLATFORM_ATTEMPTS:
                    self._repository.retry_attempt(
                        run_id=run_id,
                        attempt_id=attempt.id,
                        code=exc.code,
                    )
                    continue
                return self._repository.fail_attempt(
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt.id,
                    code=exc.code,
                    retryable=safely_retryable,
                )
            except CapabilityBudgetError:
                return self._repository.fail_attempt(
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt.id,
                    code="CAPABILITY_BUDGET_ERROR",
                    retryable=False,
                )
            except Exception:
                return self._repository.fail_attempt(
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt.id,
                    code="AGENT_RUNTIME_ERROR",
                )
            artifact = self._artifact_store.put_bytes(
                "trajectory",
                canonical_json(trajectory).encode("utf-8"),
                media_type="application/json",
            )
            try:
                if self._after_artifact_write is not None:
                    self._after_artifact_write(stored_case.external_id, artifact)
                self._artifact_store.verify(artifact)
            except ArtifactIntegrityError:
                return self._repository.fail_attempt(
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt.id,
                    code="ARTIFACT_INTEGRITY_ERROR",
                )
            except Exception:
                return self._repository.fail_attempt(
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt.id,
                    code="ARTIFACT_COMMIT_ERROR",
                )
            status = self._repository.commit_case(
                task_id=task_id,
                run_id=run_id,
                attempt_id=attempt.id,
                artifact=artifact,
                result=result.model_dump(mode="json"),
                usage=usage,
                budget_anchor=budget_anchor,
                budget_exhausted=budget_exhausted,
                case_count=len(cases),
            )
            if status != "RUNNING":
                return status
        return self._repository.task_status(task_id)

    async def _run_case(
        self,
        *,
        run_id: str,
        stored_case: StoredCase,
        case,
        attempt_id: str,
        runtime: AgentRuntime,
        budget_guard: "CapabilityBudgetGuard | None",
        budget_baseline: UsageSnapshot | None,
        max_steps: int,
    ) -> tuple[dict[str, Any], EvaluationResult, UsageSnapshot, bool]:
        lease = self._adapter.prepare_scenario(case.scenario)
        episode = None
        exchanges: list[dict[str, Any]] = []
        result: EvaluationResult | None = None
        usage = UsageSnapshot()
        budget_exhausted = False
        runtime_started = False
        heartbeat = asyncio.create_task(self._heartbeat_loop(run_id))
        try:
            observation = self._adapter.start_episode(case, lease)
            if observation.ref is None:
                raise RuntimeError("benchmark did not return an episode reference")
            episode = observation.ref
            context = EpisodeContext(
                run_id=run_id,
                case_id=stored_case.external_id,
                attempt_id=attempt_id,
                public_input=case.public_input,
                tools=tuple(self._adapter.tool_definitions()),
                max_steps=max_steps,
            )
            await runtime.reset(context)
            runtime_started = True
            for sequence in range(1, context.max_steps + 1):
                if budget_guard is not None and budget_guard.exhausted():
                    budget_exhausted = True
                    result = EvaluationResult(
                        reward=0.0,
                        correct=False,
                        metrics={"budget_exhausted": 1.0},
                    )
                    break
                try:
                    action = await runtime.act(observation)
                except CapabilityBudgetError:
                    budget_exhausted = True
                    result = EvaluationResult(
                        reward=0.0,
                        correct=False,
                        metrics={"budget_exhausted": 1.0},
                    )
                    break
                next_observation = self._adapter.execute_action(episode, action)
                exchanges.append(
                    {
                        "sequence": sequence,
                        "observation": _canonical_observation(observation),
                        "action": action.model_dump(mode="json"),
                        "result_observation": _canonical_observation(next_observation),
                    }
                )
                submission = _submission_for(action)
                if submission is not None:
                    result = self._adapter.evaluate(episode, submission)
                    break
                if next_observation.terminal:
                    result = EvaluationResult(
                        reward=0.0,
                        correct=False,
                        metrics={"terminal_without_submission": 1.0},
                    )
                    break
                observation = next_observation
            if result is None:
                result = EvaluationResult(
                    reward=0.0,
                    correct=False,
                    metrics={"runner_max_steps": 1.0},
                )
        finally:
            heartbeat.cancel()
            heartbeat_error: Exception | None = None
            try:
                await heartbeat
            except asyncio.CancelledError:
                pass
            except Exception as exc:
                heartbeat_error = exc
            try:
                if runtime_started:
                    if budget_guard is not None:
                        if budget_baseline is None:
                            raise RuntimeError("model budget baseline is missing")
                        usage = budget_guard.usage_since(budget_baseline)
                    else:
                        usage = runtime.usage()
                        if usage.total or usage.estimated_cost:
                            raise RuntimeError(
                                "runtime reported model usage outside platform gateway"
                            )
            finally:
                try:
                    await runtime.close()
                finally:
                    try:
                        if episode is not None:
                            self._adapter.close_episode(episode)
                    finally:
                        self._adapter.release_scenario(lease)
            if heartbeat_error is not None:
                raise heartbeat_error
        trajectory = {
            "protocol_version": "1",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "case_id": stored_case.external_id,
            "exchanges": exchanges,
            "result": result.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
        }
        return trajectory, result, usage, budget_exhausted

    async def _heartbeat_loop(self, run_id: str) -> None:
        while True:
            await asyncio.sleep(self._repository.heartbeat_interval)
            self._repository.heartbeat(run_id)


def _submission_for(action) -> Submission | None:
    if isinstance(action, SubmitAction):
        return Submission(answer=action.answer)
    if isinstance(action, ToolCallAction) and action.tool == "submit":
        answer = action.arguments.get("answer")
        if isinstance(answer, str):
            return Submission(answer=answer)
    return None


def _canonical_observation(observation) -> dict[str, Any]:
    payload = observation.model_dump(mode="json", exclude={"ref"})
    return json.loads(canonical_json(payload))


class CapabilityBudgetGuard:
    def __init__(
        self,
        *,
        signer: CapabilitySigner,
        token: str,
        run_id: str,
        agent_revision_id: str,
    ) -> None:
        self._signer = signer
        self._token = token
        self._run_id = run_id
        self._agent_revision_id = agent_revision_id
        self._claims = signer.verify(
            token,
            expected_run=run_id,
            expected_agent=agent_revision_id,
        )
        self._binding = hashlib.sha256(token.encode("utf-8")).hexdigest()

    def validate(self, *, run_id: str, task_budget: dict[str, Any]) -> None:
        if run_id != self._run_id:
            raise CapabilityBudgetError("model budget guard run mismatch")
        max_tokens = int(task_budget.get("max_tokens", 0))
        max_cost = Decimal(str(task_budget.get("max_cost", "0")))
        if (
            self._claims.max_tokens != max_tokens
            or self._claims.max_cost != max_cost
        ):
            raise CapabilityBudgetError(
                "capability limits do not match the frozen task budget"
            )

    def validate_runtime(self, runtime: AgentRuntime) -> None:
        if getattr(runtime, "model_gateway_binding", None) != self._binding:
            raise CapabilityBudgetError(
                "agent runtime is not bound to the authorized model gateway capability"
            )

    def validate_anchor(self, anchor: UsageSnapshot) -> None:
        current = self.usage()
        if (
            current.prompt_tokens < anchor.prompt_tokens
            or current.estimated_cost < anchor.estimated_cost
        ):
            raise CapabilityBudgetError(
                "model budget ledger is behind the persisted run anchor"
            )

    def exhausted(self) -> bool:
        snapshot = self._snapshot()
        return (
            snapshot.consumed_tokens > 0
            and snapshot.consumed_tokens >= self._claims.max_tokens
        ) or (
            snapshot.consumed_cost > 0
            and snapshot.consumed_cost >= self._claims.max_cost
        )

    def usage(self) -> UsageSnapshot:
        snapshot = self._snapshot()
        return UsageSnapshot(
            prompt_tokens=snapshot.consumed_tokens,
            estimated_cost=snapshot.consumed_cost,
        )

    def usage_since(self, baseline: UsageSnapshot) -> UsageSnapshot:
        current = self.usage()
        if (
            current.prompt_tokens < baseline.prompt_tokens
            or current.estimated_cost < baseline.estimated_cost
        ):
            raise CapabilityBudgetError("model budget ledger moved backwards")
        return UsageSnapshot(
            prompt_tokens=current.prompt_tokens - baseline.prompt_tokens,
            estimated_cost=current.estimated_cost - baseline.estimated_cost,
        )

    def _snapshot(self):
        return self._signer.budget_snapshot(
            self._token,
            expected_run=self._run_id,
            expected_agent=self._agent_revision_id,
        )
