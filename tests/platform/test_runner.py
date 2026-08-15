import tempfile
import unittest
import hashlib
from decimal import Decimal
from pathlib import Path

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.capabilities import (
    CapabilityClaims,
    CapabilitySigner,
    InMemoryCapabilityBudgetStore,
)
from secrl_platform.agents.protocol import UsageSnapshot
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.models.gateway import ModelGateway, _conservative_input_token_bound
from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import ModelRequest, ModelResponse, Usage
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


def gateway_model_request(token, *, request_id, run_id="guarded-run"):
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
        max_output_tokens=1,
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

            self.assertEqual(status, "BUDGET_EXHAUSTED")
            self.assertEqual(repo.final_result_count(handle.task_id), 1)
            self.assertEqual(provider.calls, 1)

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
