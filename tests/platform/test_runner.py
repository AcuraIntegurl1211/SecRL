import asyncio
import tempfile
import unittest
import hashlib
import json
from decimal import Decimal
from pathlib import Path

from secrl_platform.agents.builtin import DeterministicSmokeAgent, LegacyGatewayClient
from secrl_platform.agents.capabilities import (
    CapabilityBudgetError,
    CapabilityClaims,
    CapabilitySigner,
    InMemoryCapabilityBudgetStore,
)
from secrl_platform.agents.protocol import UsageSnapshot
from secrl_platform.agents.service import AgentServiceError, InvalidAgentAction
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.benchmarks.secrl import SecRLAdapter
from secrl_platform.models.gateway import ModelGateway, _conservative_input_token_bound
from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import (
    ModelRequest,
    ModelResponse,
    ProviderError,
    Usage,
)
from secrl_platform.runner.engine import CapabilityBudgetGuard, RunnerEngine
from secrl_platform.runner.process import RunnerProcess
from secrl_platform.runner.recovery import (
    RunLeaseHeld,
    RunLeaseLost,
    RunnerRepository,
)
from secrl_platform.runner.state import InvalidTransition, RunStateMachine
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import EvaluationTaskORM, RunORM


class RunStateTest(unittest.TestCase):
    def test_pause_only_becomes_paused_after_case_commit(self):
        machine = RunStateMachine("RUNNING")

        machine.request_pause()

        self.assertEqual(machine.state, "PAUSE_REQUESTED")
        machine.case_committed()
        self.assertEqual(machine.state, "PAUSED")

    def test_invalid_transition_is_rejected(self):
        machine = RunStateMachine("SUCCEEDED")

        with self.assertRaises(InvalidTransition):
            machine.transition("RUNNING")

    def test_failed_run_can_be_requeued_but_terminal_runs_cannot(self):
        machine = RunStateMachine("FAILED")
        machine.transition("QUEUED")
        self.assertEqual(machine.state, "QUEUED")

        for terminal in ("SUCCEEDED", "BUDGET_EXHAUSTED", "CANCELED"):
            with self.subTest(terminal=terminal):
                with self.assertRaises(InvalidTransition):
                    RunStateMachine(terminal).transition("QUEUED")


class GatewaySmokeAgent(DeterministicSmokeAgent):
    model_access = "platform_gateway"

    def __init__(self, gateway, token, run_id):
        super().__init__()
        self._gateway = gateway
        self._token = token
        self._run_id = run_id
        self._calls = 0

    async def act(self, observation):
        self._calls += 1
        await self._gateway.complete(
            gateway_model_request(
                self._token,
                request_id=f"runner-model-{self._calls}",
                run_id=self._run_id,
            )
        )
        return await super().act(observation)

    def usage(self):
        return UsageSnapshot(
            prompt_tokens=999,
            estimated_cost=Decimal("999"),
        )

    @property
    def model_gateway_binding(self):
        return hashlib.sha256(self._token.encode("utf-8")).hexdigest()


class ProviderRequestIdSmokeAgent(DeterministicSmokeAgent):
    @property
    def provider_request_ids(self):
        return ("provider-response-123",)


class BoundaryGatewayAgent(DeterministicSmokeAgent):
    model_access = "platform_gateway"

    def __init__(self, gateway, token, run_id):
        super().__init__()
        self._client = LegacyGatewayClient(
            gateway=gateway,
            model="fixture",
            capability_token=token,
            agent_revision_id=DeterministicSmokeAgent.revision().id,
            max_output_tokens=23_958,
        )
        self._provider_request_ids = []

    @property
    def provider_request_ids(self):
        return tuple(self._provider_request_ids)

    async def reset(self, episode):
        await super().reset(episode)
        self._client.bind_episode(episode)

    async def act(self, _observation):
        await asyncio.to_thread(
            self._client.create,
            messages=[{"role": "user", "content": "x" * 400_000}],
        )
        self._provider_request_ids = list(self._client.provider_request_ids)
        raise CapabilityBudgetError("simulated post-call budget boundary")

    @property
    def model_gateway_binding(self):
        return self._client.model_gateway_binding


class IncorrectlyBoundGatewayAgent(GatewaySmokeAgent):
    @property
    def model_gateway_binding(self):
        return hashlib.sha256(b"another-capability").hexdigest()


class ResettableCapabilityBudgetStore(InMemoryCapabilityBudgetStore):
    def reset(self):
        self._states.clear()


class ExplodingSmokeAgent(DeterministicSmokeAgent):
    async def act(self, _observation):
        raise RuntimeError("sensitive provider detail must not persist")


class FailingStartAdapter:
    def __init__(self, delegate):
        self._delegate = delegate
        self.released = False

    def __getattr__(self, name):
        return getattr(self._delegate, name)

    def start_episode(self, _case, _lease):
        raise RuntimeError("episode startup failed")

    def release_scenario(self, lease):
        self.released = True
        self._delegate.release_scenario(lease)


def gateway_model_request(
    token,
    *,
    request_id,
    run_id="guarded-run",
    max_output_tokens=1,
):
    return ModelRequest(
        provider_adapter_version="v1",
        model_role="agent",
        model="fixture",
        messages=({"role": "user", "content": "runner"},),
        run_id=run_id,
        case_id="smoke-001",
        attempt_id="guarded-attempt",
        agent_revision_id=DeterministicSmokeAgent.revision().id,
        capability_token=token,
        request_id=request_id,
        max_output_tokens=max_output_tokens,
    )


class RunnerEngineTest(unittest.IsolatedAsyncioTestCase):
    def create_harness(self, root, *, budget=None, case_ids=None):
        adapter = ProtocolSmokeAdapter.load_default()
        repo = RunnerRepository(
            create_engine_and_session(root / "platform.sqlite3", create=True)
        )
        handle = repo.create_protocol_smoke_run(
            name="runner-test",
            adapter=adapter,
            agent_revision=DeterministicSmokeAgent.revision(),
            case_ids=case_ids or ("smoke-001", "smoke-002", "smoke-003"),
            budget=budget,
        )
        return adapter, repo, handle, LocalArtifactStore(root / "artifacts")

    def test_real_single_case_budget_boundary_is_success_after_commit(self):
        """A committed final Case must win over a post-call budget signal."""
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(
                Path(directory),
                budget={
                    "max_cases": 1,
                    "max_tokens": 500_000,
                    "max_cost": "0.10",
                },
                case_ids=("smoke-001",),
            )
            self.assertEqual(
                repo.prepare_for_run(handle.task_id, handle.run_id),
                "RUNNING",
            )
            stored_case = repo.cases(handle.task_id, handle.run_id)[0]
            attempt = repo.start_attempt(handle.run_id, stored_case.record_id)
            artifact = store.put_bytes(
                "trajectory",
                b'{"protocol_version":"1","exchanges":[]}',
                media_type="application/json",
            )

            status = repo.commit_case(
                task_id=handle.task_id,
                run_id=handle.run_id,
                attempt_id=attempt.id,
                artifact=artifact,
                result={"reward": 0.0, "correct": False, "steps": 19},
                usage=UsageSnapshot(
                    prompt_tokens=411_410,
                    completion_tokens=0,
                    estimated_cost=Decimal("0.06095152"),
                ),
                budget_anchor=UsageSnapshot(
                    prompt_tokens=411_410,
                    completion_tokens=0,
                    estimated_cost=Decimal("0.06095152"),
                ),
                budget_exhausted=True,
                case_count=1,
            )

            self.assertEqual(status, "SUCCEEDED")
            self.assertEqual(repo.task_status(handle.task_id), "SUCCEEDED")
            self.assertEqual(repo.checkpoint(handle.task_id, handle.run_id), 1)
            self.assertEqual(repo.final_result_count(handle.task_id), 1)

    async def test_legacy_v010_runspec_without_scope_metadata_still_recovers(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self.create_harness(
                root,
                case_ids=("smoke-001", "smoke-002"),
            )
            with repo._session_factory.begin() as session:
                run = session.get(RunORM, handle.run_id)
                self.assertIsNotNone(run)
                task = session.get(EvaluationTaskORM, handle.task_id)
                self.assertIsNotNone(task)
                legacy_spec = json.loads(task.task_spec_json)
                legacy_spec.pop("selection", None)
                legacy_spec.pop("case_record_ids", None)
                legacy_spec.pop("case_count", None)
                legacy_spec.pop("incident_count", None)
                task.task_spec_json = json.dumps(legacy_spec, sort_keys=True, separators=(",", ":"))
                legacy_run_spec = json.loads(run.run_spec_json)
                legacy_run_spec["task_spec"] = legacy_spec
                run.run_spec_json = json.dumps(legacy_run_spec, sort_keys=True, separators=(",", ":"))
                run.run_spec_sha256 = hashlib.sha256(run.run_spec_json.encode("utf-8")).hexdigest()

            self.assertEqual(
                tuple(case.external_id for case in repo.cases(handle.task_id, handle.run_id)),
                ("smoke-001", "smoke-002"),
            )
            self.assertEqual(
                await RunnerEngine(
                    repository=repo,
                    artifact_store=store,
                    adapter=adapter,
                    runtime_factory=DeterministicSmokeAgent,
                ).run(handle.task_id, handle.run_id),
                "SUCCEEDED",
            )

    async def test_provider_request_id_is_persisted_without_raw_response(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self.create_harness(
                root,
                case_ids=("smoke-001",),
            )

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=ProviderRequestIdSmokeAgent,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "SUCCEEDED")
            artifacts = list((root / "artifacts").glob("sha256/*/*/*"))
            self.assertEqual(len(artifacts), 1)
            trajectory = json.loads(artifacts[0].read_text(encoding="utf-8"))
            self.assertEqual(
                trajectory["provider_request_ids"],
                ["provider-response-123"],
            )
            self.assertNotIn("raw_response", trajectory)

    async def test_real_budget_boundary_settles_gateway_and_prioritizes_completion(self):
        for case_ids, expected_status in (
            (("smoke-001",), "SUCCEEDED"),
            (("smoke-001", "smoke-002"), "BUDGET_EXHAUSTED"),
        ):
            with self.subTest(case_ids=case_ids):
                with tempfile.TemporaryDirectory() as directory:
                    root = Path(directory)
                    adapter, repo, handle, store = self.create_harness(
                        root,
                        budget={
                            "max_cases": 1,
                            "max_tokens": 500_000,
                            "max_cost": "0.10",
                        },
                        case_ids=case_ids,
                    )
                    revision_id = DeterministicSmokeAgent.revision().id
                    signer = CapabilitySigner(
                        b"g" * 32,
                        now=lambda: 1_000,
                        budget_store=InMemoryCapabilityBudgetStore(),
                    )
                    token = signer.issue(
                        CapabilityClaims(
                            run_id=handle.run_id,
                            agent_revision_id=revision_id,
                            allowed_model_roles=("agent",),
                            max_tokens=500_000,
                            max_cost=Decimal("0.10"),
                            issued_at=1_000,
                            expires_at=1_300,
                            nonce="real-budget-boundary",
                        )
                    )

                    class Provider:
                        calls = 0

                        async def complete(self, _request):
                            self.calls += 1
                            return ModelResponse(
                                text="unused",
                                usage=Usage(prompt=387_452, completion=23_958),
                                provider_request_id="provider-boundary-123",
                            )

                    provider = Provider()
                    gateway = ModelGateway(
                        provider=provider,
                        pricing=Pricing(
                            input_per_million="0.14",
                            output_per_million="0.28",
                        ),
                        capability_signer=signer,
                    )
                    guard = CapabilityBudgetGuard(
                        signer=signer,
                        token=token,
                        run_id=handle.run_id,
                        agent_revision_id=revision_id,
                    )

                    status = await RunnerEngine(
                        repository=repo,
                        artifact_store=store,
                        adapter=adapter,
                        runtime_factory=lambda: BoundaryGatewayAgent(
                            gateway,
                            token,
                            handle.run_id,
                        ),
                        model_budget_guard=guard,
                    ).run(handle.task_id, handle.run_id)

                    self.assertEqual(
                        status,
                        expected_status,
                        repo.attempt_errors(handle.task_id),
                    )
                    self.assertEqual(provider.calls, 1)
                    self.assertEqual(repo.final_result_count(handle.task_id), 1)
                    settled = signer.budget_snapshot(
                        token,
                        expected_run=handle.run_id,
                        expected_agent=revision_id,
                    )
                    self.assertEqual(settled.reserved_tokens, 0)
                    self.assertEqual(settled.reserved_cost, Decimal("0"))
                    self.assertEqual(settled.consumed_tokens, 411_410)
                    self.assertEqual(
                        settled.consumed_cost,
                        Decimal("0.06095152"),
                    )
                    artifact = next((root / "artifacts").glob("sha256/*/*/*"))
                    trajectory = json.loads(artifact.read_text(encoding="utf-8"))
                    self.assertEqual(
                        trajectory["provider_request_ids"],
                        ["provider-boundary-123"],
                    )
                    self.assertNotIn("raw_response", trajectory)

    async def test_pause_and_resume_happen_only_at_committed_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(Path(directory))
            pause_requested = False

            def request_pause(_case_id, _artifact):
                nonlocal pause_requested
                if not pause_requested:
                    pause_requested = True
                    repo.request_pause(handle.task_id, handle.run_id)

            engine = RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
                after_artifact_write=request_pause,
            )
            self.assertEqual(await engine.run(handle.task_id, handle.run_id), "PAUSED")
            self.assertEqual(repo.final_result_count(handle.task_id), 1)
            self.assertEqual(repo.checkpoint(handle.task_id, handle.run_id), 1)

            repo.resume(handle.task_id, handle.run_id)
            resumed = RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
            )
            self.assertEqual(
                await resumed.run(handle.task_id, handle.run_id),
                "SUCCEEDED",
            )
            self.assertEqual(repo.final_result_count(handle.task_id), 3)

    def test_run_freezes_cases_from_multiple_incidents_without_duplicates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = SecRLAdapter()
            all_cases = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())
            selected = (all_cases[0].id, all_cases[-1].id)
            repo = RunnerRepository(
                create_engine_and_session(root / "platform.sqlite3", create=True)
            )
            handle = repo.create_benchmark_run(
                name="multi-incident",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=selected,
            )

            stored = repo.cases(handle.task_id, handle.run_id)

            self.assertEqual([case.external_id for case in stored], list(selected))
            self.assertEqual(len({case.ordinal for case in stored}), 2)

    def test_run_rejects_duplicate_selected_cases(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter = ProtocolSmokeAdapter.load_default()
            repo = RunnerRepository(
                create_engine_and_session(Path(directory) / "platform.sqlite3", create=True)
            )
            with self.assertRaisesRegex(ValueError, "duplicate"):
                repo.create_protocol_smoke_run(
                    name="duplicate",
                    adapter=adapter,
                    agent_revision=DeterministicSmokeAgent.revision(),
                    case_ids=("smoke-001", "smoke-001"),
                )

    async def test_case_and_token_budgets_commit_current_evidence_then_stop(self):
        for budget, runtime_factory in (({"max_cases": 1}, DeterministicSmokeAgent),):
            with self.subTest(budget=budget):
                with tempfile.TemporaryDirectory() as directory:
                    adapter, repo, handle, store = self.create_harness(
                        Path(directory), budget=budget
                    )
                    engine = RunnerEngine(
                        repository=repo,
                        artifact_store=store,
                        adapter=adapter,
                        runtime_factory=runtime_factory,
                    )

                    status = await engine.run(handle.task_id, handle.run_id)

                    self.assertEqual(status, "BUDGET_EXHAUSTED")
                    self.assertEqual(repo.final_result_count(handle.task_id), 1)
                    self.assertEqual(repo.checkpoint(handle.task_id, handle.run_id), 1)

    async def test_max_cases_equal_to_or_above_selected_cases_succeeds(self):
        for case_ids, max_cases in (
            (("smoke-001",), 1),
            (("smoke-001", "smoke-002"), 2),
            (("smoke-001", "smoke-002"), 3),
        ):
            with self.subTest(case_ids=case_ids, max_cases=max_cases):
                with tempfile.TemporaryDirectory() as directory:
                    adapter, repo, handle, store = self.create_harness(
                        Path(directory),
                        budget={"max_cases": max_cases},
                        case_ids=case_ids,
                    )
                    status = await RunnerEngine(
                        repository=repo,
                        artifact_store=store,
                        adapter=adapter,
                        runtime_factory=DeterministicSmokeAgent,
                    ).run(handle.task_id, handle.run_id)

                    self.assertEqual(status, "SUCCEEDED")
                    self.assertEqual(repo.final_result_count(handle.task_id), len(case_ids))

    async def test_hard_budget_exhaustion_wins_over_pause_request(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(
                Path(directory),
                budget={"max_cases": 1},
                case_ids=("smoke-001", "smoke-002"),
            )

            def request_pause(_case_id, _artifact):
                repo.request_pause(handle.task_id, handle.run_id)

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
                after_artifact_write=request_pause,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "BUDGET_EXHAUSTED")
            self.assertEqual(repo.final_result_count(handle.task_id), 1)

    async def test_model_budget_uses_platform_gateway_ledger_before_each_call(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ProtocolSmokeAdapter.load_default()
            repo = RunnerRepository(
                create_engine_and_session(root / "platform.sqlite3", create=True)
            )
            unsigned = gateway_model_request("placeholder", request_id="bound")
            token_limit = _conservative_input_token_bound(unsigned) + 1
            handle = repo.create_protocol_smoke_run(
                name="gateway-budget",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001",),
                budget={"max_tokens": token_limit, "max_cost": "0"},
            )
            signer = CapabilitySigner(
                b"b" * 32,
                now=lambda: 1_000,
                budget_store=InMemoryCapabilityBudgetStore(),
            )
            claims = CapabilityClaims(
                run_id=handle.run_id,
                agent_revision_id=DeterministicSmokeAgent.revision().id,
                allowed_model_roles=("agent",),
                max_tokens=token_limit,
                max_cost=Decimal("0"),
                issued_at=1_000,
                expires_at=1_300,
                nonce="runner-budget",
            )
            token = signer.issue(claims)

            class Provider:
                calls = 0

                async def complete(self, _request):
                    self.calls += 1
                    return ModelResponse(
                        text="ok",
                        usage=Usage(prompt=token_limit - 1, completion=1),
                    )

            provider = Provider()
            gateway = ModelGateway(
                provider=provider,
                pricing=Pricing(input_per_million=0, output_per_million=0),
                capability_signer=signer,
            )
            guard = CapabilityBudgetGuard(
                signer=signer,
                token=token,
                run_id=handle.run_id,
                agent_revision_id=DeterministicSmokeAgent.revision().id,
            )

            status = await RunnerEngine(
                repository=repo,
                artifact_store=LocalArtifactStore(root / "artifacts"),
                adapter=adapter,
                runtime_factory=lambda: GatewaySmokeAgent(
                    gateway, token, handle.run_id
                ),
                model_budget_guard=guard,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "SUCCEEDED")
            self.assertEqual(repo.final_result_count(handle.task_id), 1)
            self.assertEqual(provider.calls, 1)
            settled = signer.budget_snapshot(
                token,
                expected_run=handle.run_id,
                expected_agent=DeterministicSmokeAgent.revision().id,
            )
            self.assertEqual(settled.reserved_tokens, 0)
            self.assertEqual(settled.reserved_cost, Decimal("0"))
            self.assertEqual(settled.consumed_tokens, token_limit)

    async def test_model_budget_rejection_before_dispatch_commits_final_case(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self.create_harness(
                root,
                budget={"max_tokens": 1_000, "max_cost": "0"},
                case_ids=("smoke-001",),
            )
            signer = CapabilitySigner(
                b"f" * 32,
                now=lambda: 1_000,
                budget_store=InMemoryCapabilityBudgetStore(),
            )
            token = signer.issue(
                CapabilityClaims(
                    run_id=handle.run_id,
                    agent_revision_id=DeterministicSmokeAgent.revision().id,
                    allowed_model_roles=("agent",),
                    max_tokens=1_000,
                    max_cost=Decimal("0"),
                    issued_at=1_000,
                    expires_at=1_300,
                    nonce="pre-dispatch-budget-rejection",
                )
            )

            class Provider:
                calls = 0

                async def complete(self, _request):
                    self.calls += 1
                    return ModelResponse(text="unexpected", usage=Usage(prompt=1, completion=1))

            provider = Provider()
            gateway = ModelGateway(
                provider=provider,
                pricing=Pricing(input_per_million=1, output_per_million=1),
                capability_signer=signer,
            )
            guard = CapabilityBudgetGuard(
                signer=signer,
                token=token,
                run_id=handle.run_id,
                agent_revision_id=DeterministicSmokeAgent.revision().id,
            )

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=lambda: GatewaySmokeAgent(
                    gateway, token, handle.run_id
                ),
                model_budget_guard=guard,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "SUCCEEDED")
            self.assertEqual(repo.final_result_count(handle.task_id), 1)
            self.assertEqual(provider.calls, 0)

    async def test_transient_agent_service_error_retries_with_typed_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(
                Path(directory), case_ids=("smoke-001",)
            )
            calls = 0

            class TransientThenHealthyAgent(DeterministicSmokeAgent):
                async def act(self, observation):
                    nonlocal calls
                    calls += 1
                    if calls == 1:
                        raise AgentServiceError("temporary outage", code="UNAVAILABLE")
                    return await super().act(observation)

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=TransientThenHealthyAgent,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "SUCCEEDED")
            self.assertEqual(repo.final_result_count(handle.task_id), 1)
            self.assertEqual(
                repo.attempt_errors(handle.task_id),
                ({"code": "UNAVAILABLE", "retryable": True},),
            )

    async def test_ambiguous_provider_failure_is_not_retried_by_runner(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(
                Path(directory), case_ids=("smoke-001",)
            )
            calls = 0

            class AmbiguousProviderAgent(DeterministicSmokeAgent):
                async def act(self, _observation):
                    nonlocal calls
                    calls += 1
                    raise ProviderError("TIMEOUT", usage_may_have_occurred=True)

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=AmbiguousProviderAgent,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertEqual(calls, 1)
            self.assertEqual(
                repo.attempt_errors(handle.task_id),
                ({
                    "code": "TIMEOUT",
                    "retryable": False,
                    "safe_to_retry": False,
                    "usage_may_have_occurred": True,
                },),
            )

    async def test_permanent_agent_action_error_fails_without_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(
                Path(directory), case_ids=("smoke-001",)
            )
            calls = 0

            class InvalidActionAgent(DeterministicSmokeAgent):
                async def act(self, _observation):
                    nonlocal calls
                    calls += 1
                    raise InvalidAgentAction("unapproved tool")

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=InvalidActionAgent,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertEqual(calls, 1)
            self.assertEqual(
                repo.attempt_errors(handle.task_id),
                ({"code": "INVALID_ACTION", "retryable": False},),
            )

    async def test_model_runtime_must_be_bound_to_guard_capability(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self.create_harness(
                root,
                budget={"max_tokens": 100, "max_cost": "0"},
                case_ids=("smoke-001",),
            )
            signer = CapabilitySigner(
                b"c" * 32,
                now=lambda: 1_000,
                budget_store=InMemoryCapabilityBudgetStore(),
            )
            token = signer.issue(
                CapabilityClaims(
                    run_id=handle.run_id,
                    agent_revision_id=DeterministicSmokeAgent.revision().id,
                    allowed_model_roles=("agent",),
                    max_tokens=100,
                    max_cost=Decimal("0"),
                    issued_at=1_000,
                    expires_at=1_300,
                    nonce="runtime-binding",
                )
            )

            class Provider:
                calls = 0

                async def complete(self, _request):
                    self.calls += 1
                    return ModelResponse(text="ok", usage=Usage(prompt=1, completion=1))

            provider = Provider()
            gateway = ModelGateway(
                provider=provider,
                pricing=Pricing(input_per_million=0, output_per_million=0),
                capability_signer=signer,
            )
            guard = CapabilityBudgetGuard(
                signer=signer,
                token=token,
                run_id=handle.run_id,
                agent_revision_id=DeterministicSmokeAgent.revision().id,
            )

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=lambda: IncorrectlyBoundGatewayAgent(
                    gateway, token, handle.run_id
                ),
                model_budget_guard=guard,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertEqual(provider.calls, 0)

    def test_model_budget_usage_is_recorded_as_per_case_delta(self):
        signer = CapabilitySigner(
            b"d" * 32,
            now=lambda: 1_000,
            budget_store=InMemoryCapabilityBudgetStore(),
        )
        revision_id = DeterministicSmokeAgent.revision().id
        token = signer.issue(
            CapabilityClaims(
                run_id="delta-run",
                agent_revision_id=revision_id,
                allowed_model_roles=("agent",),
                max_tokens=20,
                max_cost=Decimal("5"),
                issued_at=1_000,
                expires_at=1_300,
                nonce="per-case-delta",
            )
        )
        guard = CapabilityBudgetGuard(
            signer=signer,
            token=token,
            run_id="delta-run",
            agent_revision_id=revision_id,
        )
        first_baseline = guard.usage()
        signer.authorize_usage(
            token,
            additional_tokens=3,
            additional_cost=Decimal("0.25"),
            expected_run="delta-run",
            expected_agent=revision_id,
            model_role="agent",
            request_id="delta-first",
        )
        self.assertEqual(
            guard.usage_since(first_baseline),
            UsageSnapshot(prompt_tokens=3, estimated_cost=Decimal("0.25")),
        )

        second_baseline = guard.usage()
        signer.authorize_usage(
            token,
            additional_tokens=4,
            additional_cost=Decimal("0.50"),
            expected_run="delta-run",
            expected_agent=revision_id,
            model_role="agent",
            request_id="delta-second",
        )
        self.assertEqual(
            guard.usage_since(second_baseline),
            UsageSnapshot(prompt_tokens=4, estimated_cost=Decimal("0.50")),
        )

    async def test_model_ledger_rollback_is_rejected_before_next_case_dispatch(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self.create_harness(
                root,
                budget={"max_tokens": 100, "max_cost": "0"},
                case_ids=("smoke-001", "smoke-002"),
            )
            budget_store = ResettableCapabilityBudgetStore()
            signer = CapabilitySigner(
                b"e" * 32,
                now=lambda: 1_000,
                budget_store=budget_store,
            )
            token = signer.issue(
                CapabilityClaims(
                    run_id=handle.run_id,
                    agent_revision_id=DeterministicSmokeAgent.revision().id,
                    allowed_model_roles=("agent",),
                    max_tokens=100,
                    max_cost=Decimal("0"),
                    issued_at=1_000,
                    expires_at=1_300,
                    nonce="ledger-rollback",
                )
            )

            class Provider:
                calls = 0

                async def complete(self, _request):
                    self.calls += 1
                    return ModelResponse(text="ok", usage=Usage(prompt=1, completion=1))

            provider = Provider()
            gateway = ModelGateway(
                provider=provider,
                pricing=Pricing(input_per_million=0, output_per_million=0),
                capability_signer=signer,
            )
            guard = CapabilityBudgetGuard(
                signer=signer,
                token=token,
                run_id=handle.run_id,
                agent_revision_id=DeterministicSmokeAgent.revision().id,
            )
            calls_after_first_case = None

            def roll_back_ledger(case_id, _artifact):
                nonlocal calls_after_first_case
                if case_id == "smoke-001":
                    calls_after_first_case = provider.calls
                    budget_store.reset()

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=lambda: GatewaySmokeAgent(
                    gateway, token, handle.run_id
                ),
                model_budget_guard=guard,
                after_artifact_write=roll_back_ledger,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertIsNotNone(calls_after_first_case)
            self.assertEqual(provider.calls, calls_after_first_case)
            self.assertEqual(repo.final_result_count(handle.task_id), 1)

    async def test_multiple_tasks_reuse_frozen_revisions_without_unique_conflicts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, first, _store = self.create_harness(root)

            second = repo.create_protocol_smoke_run(
                name="second-task",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001",),
            )

            self.assertNotEqual(first.task_id, second.task_id)
            self.assertNotEqual(first.run_id, second.run_id)

    async def test_cancel_is_honored_after_current_case_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(Path(directory))

            def request_cancel(_case_id, _artifact):
                repo.request_cancel(handle.task_id, handle.run_id)

            engine = RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
                after_artifact_write=request_cancel,
            )

            status = await engine.run(handle.task_id, handle.run_id)

            self.assertEqual(status, "CANCELED")
            self.assertEqual(repo.final_result_count(handle.task_id), 1)
            self.assertEqual(repo.checkpoint(handle.task_id, handle.run_id), 1)

    async def test_queued_cancel_is_immediate_and_runs_no_case(self):
        with tempfile.TemporaryDirectory() as directory:
            _adapter, repo, handle, _store = self.create_harness(Path(directory))

            repo.request_cancel(handle.task_id, handle.run_id)

            self.assertEqual(repo.task_status(handle.task_id), "CANCELED")
            self.assertEqual(repo.final_result_count(handle.task_id), 0)

    async def test_tampered_artifact_is_rejected_before_database_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(Path(directory))

            def tamper(_case_id, artifact):
                artifact.path.write_bytes(b"tampered")

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
                after_artifact_write=tamper,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertEqual(repo.final_result_count(handle.task_id), 0)
            self.assertEqual(
                repo.attempt_errors(handle.task_id),
                ({"code": "ARTIFACT_INTEGRITY_ERROR"},),
            )

    async def test_live_run_lease_rejects_a_second_worker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_factory = create_engine_and_session(
                root / "platform.sqlite3", create=True
            )
            adapter = ProtocolSmokeAdapter.load_default()
            first = RunnerRepository(
                session_factory,
                owner_id="worker-1",
                now=lambda: 1_000,
            )
            handle = first.create_protocol_smoke_run(
                name="lease-test",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001",),
            )
            second = RunnerRepository(
                session_factory,
                owner_id="worker-2",
                now=lambda: 1_000,
            )

            self.assertEqual(
                first.prepare_for_run(handle.task_id, handle.run_id),
                "RUNNING",
            )
            with self.assertRaises(RunLeaseHeld):
                second.prepare_for_run(handle.task_id, handle.run_id)

    async def test_expired_worker_is_fenced_after_replacement_acquires_run(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            session_factory = create_engine_and_session(
                root / "platform.sqlite3", create=True
            )
            adapter = ProtocolSmokeAdapter.load_default()
            clock = [1_000]
            first = RunnerRepository(
                session_factory,
                owner_id="expired-worker",
                now=lambda: clock[0],
                lease_seconds=10,
            )
            handle = first.create_protocol_smoke_run(
                name="fence-test",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001",),
            )
            first.prepare_for_run(handle.task_id, handle.run_id)
            case = first.cases(handle.task_id, handle.run_id)[0]
            first.start_attempt(handle.run_id, case.record_id)

            clock[0] = 1_011
            replacement = RunnerRepository(
                session_factory,
                owner_id="replacement-worker",
                now=lambda: clock[0],
                lease_seconds=10,
            )
            self.assertEqual(
                replacement.prepare_for_run(handle.task_id, handle.run_id),
                "RUNNING",
            )
            with self.assertRaises(RunLeaseLost):
                first.heartbeat(handle.run_id)

    async def test_trajectory_reads_reverify_registered_artifact(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(
                Path(directory), case_ids=("smoke-001",)
            )
            await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
            ).run(handle.task_id, handle.run_id)
            ref = repo.artifact_refs(handle.task_id, store)[0]
            ref.path.write_bytes(b"later corruption")

            with self.assertRaises(Exception) as raised:
                repo.trajectory_payloads(handle.task_id, store)
            self.assertEqual(
                type(raised.exception).__name__,
                "ArtifactIntegrityError",
            )

    async def test_process_maps_platform_failure_without_persisting_exception_text(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(Path(directory))
            process = RunnerProcess(
                RunnerEngine(
                    repository=repo,
                    artifact_store=store,
                    adapter=adapter,
                    runtime_factory=ExplodingSmokeAgent,
                )
            )

            status = await process.run_once(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            errors = repo.attempt_errors(handle.task_id)
            self.assertEqual(errors, ({"code": "AGENT_RUNTIME_ERROR"},))
            self.assertNotIn("sensitive provider detail", repr(errors))

    async def test_runtime_construction_failure_closes_attempt_and_lease(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(Path(directory))

            def raise_during_construction():
                raise RuntimeError("sensitive constructor detail")

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=raise_during_construction,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertEqual(
                repo.attempt_errors(handle.task_id),
                ({"code": "AGENT_RUNTIME_ERROR"},),
            )

    async def test_run_spec_configuration_failure_after_prepare_does_not_stay_running(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(
                Path(directory), case_ids=("smoke-001",)
            )
            with repo._session_factory.begin() as session:
                run = session.get(RunORM, handle.run_id)
                run_spec = json.loads(run.run_spec_json)
                run_spec["limits"]["max_steps"] = 0
                run.run_spec_json = json.dumps(
                    run_spec, sort_keys=True, separators=(",", ":")
                )
                run.run_spec_sha256 = hashlib.sha256(
                    run.run_spec_json.encode("utf-8")
                ).hexdigest()

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertEqual(repo.task_status(handle.task_id), "FAILED")
            self.assertEqual(
                repo.attempt_errors(handle.task_id),
                ({"code": "RUNNER_CONFIGURATION_ERROR", "retryable": False},),
            )

    async def test_scenario_lease_is_released_when_episode_start_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            adapter, repo, handle, store = self.create_harness(Path(directory))
            failing = FailingStartAdapter(adapter)

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=failing,
                runtime_factory=DeterministicSmokeAgent,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertTrue(failing.released)


if __name__ == "__main__":
    unittest.main()
