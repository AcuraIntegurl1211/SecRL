from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import secrets
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from secrl_platform.agents.capabilities import (
    CapabilityBudgetError,
    CapabilityClaims,
    CapabilityScopeError,
    CapabilitySigner,
    ExpiredCapability,
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
_LOGGER = logging.getLogger(__name__)


def _safe_provider_error_details(error: ProviderError) -> dict[str, Any]:
    return {
        "usage_may_have_occurred": error.usage_may_have_occurred,
        "safe_to_retry": error.safe_to_retry,
        "http_status": error.http_status,
        "content_type": error.content_type,
        "provider_request_id": error.provider_request_id,
        "request_id": error.request_id,
        "response_shape": error.response_shape,
    }


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
        capability_rotator: "CapabilityTokenRotator | None" = None,
    ) -> None:
        self._repository = repository
        self._artifact_store = artifact_store
        self._adapter = adapter
        self._runtime_factory = runtime_factory
        self._after_artifact_write = after_artifact_write
        self._model_budget_guard = model_budget_guard
        self._capability_rotator = capability_rotator

    async def run(self, task_id: str, run_id: str) -> str:
        status = self._repository.prepare_for_run(task_id, run_id)
        if status != "RUNNING":
            return status
        try:
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
        except Exception as error:
            _LOGGER.error(
                "runner pre-engine configuration rejected exception_type=%s",
                type(error).__name__,
            )
            return self._repository.fail_configuration(
                task_id=task_id,
                run_id=run_id,
                code="RUNNER_CONFIGURATION_ERROR",
            )
        while self._repository.checkpoint(task_id, run_id) < len(cases):
            self._repository.heartbeat(run_id)
            if self._repository.budget_reached(task_id, run_id):
                return self._repository.mark_budget_exhausted(task_id, run_id)
            index = self._repository.checkpoint(task_id, run_id)
            stored_case = cases[index]
            attempt = self._repository.start_attempt(run_id, stored_case.record_id)
            try:
                if self._capability_rotator is not None:
                    self._capability_rotator.refresh_if_needed()
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
                (
                    trajectory,
                    result,
                    usage,
                    budget_exhausted,
                    provider_request_ids,
                    restricted_outputs,
                ) = await self._run_case(
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
                safely_retryable = (
                    exc.safe_to_retry
                    if isinstance(exc, ProviderError)
                    else exc.transient
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
                    details=(
                        _safe_provider_error_details(exc)
                        if isinstance(exc, ProviderError)
                        else None
                    ),
                )
            except CapabilityBudgetError:
                return self._repository.fail_attempt(
                    task_id=task_id,
                    run_id=run_id,
                    attempt_id=attempt.id,
                    code="CAPABILITY_BUDGET_ERROR",
                    retryable=False,
                )
            except Exception as exc:
                missing_path = getattr(exc, "filename", None)
                missing_name = (
                    os.path.basename(missing_path)
                    if isinstance(missing_path, str)
                    else None
                )
                _LOGGER.error(
                    "agent runtime error exception_type=%s missing_name=%s",
                    type(exc).__name__,
                    missing_name,
                )
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
            restricted_artifacts = tuple(
                self._artifact_store.put_bytes(kind, content, media_type="application/json")
                for kind, content in restricted_outputs
            )
            try:
                if self._after_artifact_write is not None:
                    self._after_artifact_write(stored_case.external_id, artifact)
                self._artifact_store.verify(artifact)
                for restricted_artifact in restricted_artifacts:
                    self._artifact_store.verify(restricted_artifact)
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
                restricted_artifacts=restricted_artifacts,
                result={
                    **result.model_dump(mode="json"),
                    "steps": len(trajectory["exchanges"]),
                },
                usage=usage,
                budget_anchor=budget_anchor,
                budget_exhausted=budget_exhausted,
                case_count=len(cases),
                provider_request_ids=provider_request_ids,
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
    ) -> tuple[
        dict[str, Any],
        EvaluationResult,
        UsageSnapshot,
        bool,
        tuple[str, ...],
        tuple[tuple[str, bytes], ...],
    ]:
        lease = self._adapter.prepare_scenario(case.scenario)
        episode = None
        exchanges: list[dict[str, Any]] = []
        result: EvaluationResult | None = None
        usage = UsageSnapshot()
        agent_usage_captured = False
        budget_exhausted = False
        restricted_outputs: tuple[tuple[str, bytes], ...] = ()
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
            bind_attempt = getattr(self._adapter, "bind_attempt", None)
            if callable(bind_attempt):
                bind_attempt(
                    run_id=run_id,
                    case_id=stored_case.external_id,
                    attempt_id=attempt_id,
                )
            await runtime.reset(context)
            runtime_started = True
            for sequence in range(1, context.max_steps + 1):
                if self._capability_rotator is not None:
                    # A single case can outlive the token lifetime on its own;
                    # re-check between steps, while the previous token is still
                    # valid enough to refresh.
                    self._capability_rotator.refresh_if_needed()
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
                    if budget_guard is not None:
                        if budget_baseline is None:
                            raise RuntimeError("model budget baseline is missing")
                        usage = budget_guard.usage_since(budget_baseline)
                        agent_usage_captured = True
                    result = await asyncio.to_thread(
                        self._adapter.evaluate,
                        episode,
                        submission,
                    )
                    take_restricted = getattr(
                        self._adapter,
                        "take_restricted_artifacts",
                        None,
                    )
                    if callable(take_restricted):
                        restricted_outputs = tuple(take_restricted(episode))
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
                        if not agent_usage_captured:
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
        provider_request_ids = _safe_runtime_provider_request_ids(runtime)
        trajectory = {
            "protocol_version": "1",
            "run_id": run_id,
            "attempt_id": attempt_id,
            "case_id": stored_case.external_id,
            "exchanges": exchanges,
            "result": result.model_dump(mode="json"),
            "usage": usage.model_dump(mode="json"),
            "provider_request_ids": list(provider_request_ids),
        }
        return (
            trajectory,
            result,
            usage,
            budget_exhausted,
            provider_request_ids,
            restricted_outputs,
        )

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


def _safe_runtime_provider_request_ids(runtime: AgentRuntime) -> tuple[str, ...]:
    values = getattr(runtime, "provider_request_ids", ())
    if not isinstance(values, (tuple, list)):
        return ()
    return tuple(
        value.strip()
        for value in values
        if isinstance(value, str) and 0 < len(value.strip()) <= 256
    )


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

    def apply_capability_token(self, token: str) -> None:
        """Rebind the guard to a refreshed token with identical claims."""
        self._claims = self._signer.verify(
            token,
            expected_run=self._run_id,
            expected_agent=self._agent_revision_id,
        )
        self._token = token
        self._binding = hashlib.sha256(token.encode("utf-8")).hexdigest()

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


class _CapabilityTokenSlot:
    """Bookkeeping for one long-lived capability token and its holders."""

    __slots__ = ("signer", "token", "claims", "lease_is_active", "targets")

    def __init__(
        self,
        *,
        signer: CapabilitySigner,
        token: str,
        claims: CapabilityClaims,
        lease_is_active: Callable[[str, str], bool] | None,
    ) -> None:
        self.signer = signer
        self.token = token
        self.claims = claims
        self.lease_is_active = lease_is_active
        self.targets: list[Callable[[str], None]] = []


class CapabilityTokenRotator:
    """Refreshes expiring capability tokens and propagates them to holders.

    Dispatch issues each capability once with a 300 second lifetime, but a
    multi-case evaluation routinely outlives it. The engine calls
    :meth:`refresh_if_needed` before every case attempt and between agent
    steps; tokens within the refresh threshold are re-signed via
    ``CapabilitySigner.refresh`` under an active run lease. When the previous
    token already expired, the tracked claims are re-issued under the same
    lease gate so a boundary landing past expiry cannot strand the run.
    """

    def __init__(
        self,
        *,
        now: Callable[[], int | float] = time.time,
        threshold_seconds: int = 60,
    ) -> None:
        if threshold_seconds <= 0:
            raise ValueError("capability refresh threshold must be positive")
        self._now = now
        self._threshold_seconds = threshold_seconds
        self._slots: dict[str, _CapabilityTokenSlot] = {}

    def register(
        self,
        slot: str,
        *,
        signer: CapabilitySigner,
        token: str,
        lease_is_active: Callable[[str, str], bool] | None,
    ) -> None:
        if slot in self._slots:
            raise ValueError(f"capability slot was already registered: {slot}")
        self._slots[slot] = _CapabilityTokenSlot(
            signer=signer,
            token=token,
            claims=signer.inspect(token),
            lease_is_active=lease_is_active,
        )

    def add_target(self, slot: str, apply_token: Callable[[str], None]) -> None:
        self._require_slot(slot).targets.append(apply_token)

    def current_token(self, slot: str) -> str:
        return self._require_slot(slot).token

    def refresh_if_needed(self) -> None:
        now = int(self._now())
        for name in self._slots:
            self._refresh_slot(name, now)

    def _refresh_slot(self, name: str, now: int) -> None:
        slot = self._slots[name]
        if slot.claims.expires_at - now > self._threshold_seconds:
            return
        try:
            refreshed = slot.signer.refresh(slot.token)
        except ExpiredCapability:
            refreshed = self._reissue_expired(slot)
        slot.claims = slot.signer.verify(refreshed)
        slot.token = refreshed
        for apply_token in slot.targets:
            apply_token(refreshed)

    def _reissue_expired(self, slot: _CapabilityTokenSlot) -> str:
        # signer.refresh() verifies the old token and therefore refuses an
        # already-expired one. Re-mint the tracked claims under the same
        # active-lease gate with the original lifetime preserved.
        if slot.lease_is_active is None or not slot.lease_is_active(
            slot.claims.run_id,
            slot.claims.agent_revision_id,
        ):
            raise CapabilityScopeError(
                "capability refresh requires an active run lease"
            )
        issued_at = int(self._now())
        reissued = slot.claims.model_copy(
            update={
                "issued_at": issued_at,
                "expires_at": issued_at
                + (slot.claims.expires_at - slot.claims.issued_at),
                "nonce": secrets.token_urlsafe(18),
            }
        )
        return slot.signer.issue(reissued)

    def _require_slot(self, name: str) -> _CapabilityTokenSlot:
        try:
            return self._slots[name]
        except KeyError:
            raise ValueError(f"unknown capability slot: {name}") from None
