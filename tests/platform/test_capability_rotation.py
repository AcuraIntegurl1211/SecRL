"""Capability token rotation tests for long-running evaluations.

Regression coverage for the v0.1.3 fix: capability tokens expire after 300
seconds, so a multi-case evaluation that outlives the initial token must have
its tokens refreshed under an active run lease and propagated to every holder
(budget guard, agent gateway client, evaluator gateway client) before the next
gateway call.
"""

from __future__ import annotations

import hashlib
import itertools
import tempfile
import time
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

from pydantic import SecretStr

from secrl_platform.agents.builtin import (
    DeterministicSmokeAgent,
    LegacyGatewayClient,
)
from secrl_platform.agents.capabilities import (
    CapabilityBudgetError,
    CapabilityClaims,
    CapabilityScopeError,
    CapabilitySigner,
    InMemoryCapabilityBudgetStore,
)
from secrl_platform.agents.protocol import EpisodeContext
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.config import Settings
from secrl_platform.models.evaluator import EvaluatorGatewayClient
from secrl_platform.models.gateway import ModelGateway
from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import ModelRequest, ModelResponse, Usage
from secrl_platform.runner.engine import (
    CapabilityBudgetGuard,
    CapabilityTokenRotator,
    RunnerEngine,
)
from secrl_platform.runner.process import capability_signer
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session


_REVISION_ID = DeterministicSmokeAgent.revision().id
_REQUEST_IDS = itertools.count()


def _claims(run_id: str, *, issued_at: int, expires_at: int, nonce: str) -> CapabilityClaims:
    return CapabilityClaims(
        run_id=run_id,
        agent_revision_id=_REVISION_ID,
        allowed_model_roles=("agent",),
        max_tokens=1_000_000,
        max_cost=Decimal("10"),
        issued_at=issued_at,
        expires_at=expires_at,
        nonce=nonce,
    )


def _model_request(token: SecretStr, run_id: str) -> ModelRequest:
    return ModelRequest(
        provider_adapter_version="v1",
        model_role="agent",
        model="fixture",
        messages=({"role": "user", "content": "runner"},),
        effective_parameters={},
        run_id=run_id,
        case_id="smoke-001",
        attempt_id="rotation-attempt",
        agent_revision_id=_REVISION_ID,
        capability_token=token,
        request_id=f"rotation-model-{next(_REQUEST_IDS)}",
        max_output_tokens=32,
    )


class _HolderGatewayAgent(DeterministicSmokeAgent):
    """Protocol-smoke agent whose gateway calls always use the current token.

    Each act advances the injected clock *after* the gateway call has fully
    settled (verify and budget reconciliation included), simulating wall time
    passing between steps rather than inside a single request.
    """

    model_access = "platform_gateway"

    def __init__(self, gateway, holder, run_id, *, clock=None, step_seconds=0):
        super().__init__()
        self._gateway = gateway
        self._holder = holder
        self._run_id = run_id
        self._calls = 0
        self._clock = clock
        self._step_seconds = step_seconds

    async def act(self, observation):
        self._calls += 1
        await self._gateway.complete(
            _model_request(SecretStr(self._holder["token"]), self._run_id)
        )
        if self._clock is not None:
            self._clock[0] += self._step_seconds
        return await super().act(observation)

    @property
    def model_gateway_binding(self):
        return hashlib.sha256(self._holder["token"].encode("utf-8")).hexdigest()


class _RecordingProvider:
    """Fake provider that records the token presented with every request."""

    def __init__(self) -> None:
        self.tokens: list[str] = []

    async def complete(self, request):
        self.tokens.append(request.capability_token.get_secret_value())
        return ModelResponse(text="ok", usage=Usage(prompt=10, completion=1))


class _LegacyRecordingGateway:
    def __init__(self) -> None:
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            text="ok",
            usage=SimpleNamespace(prompt=1, completion=1, cached=0, reasoning=0),
            estimated_cost=None,
            pricing_profile_sha256="0" * 64,
            provider_request_id=None,
        )


class CapabilityTokenRotatorTest(unittest.TestCase):
    """Unit coverage for the rotation policy itself."""

    def _signer(self, clock, *, lease_is_active=None):
        return CapabilitySigner(
            b"r" * 32,
            now=lambda: clock[0],
            budget_store=InMemoryCapabilityBudgetStore(),
            lease_is_active=lease_is_active,
        )

    def test_rotates_when_remaining_lifetime_reaches_threshold(self):
        clock = [245]
        signer = self._signer(clock, lease_is_active=lambda *_: True)
        original = _claims("run-1", issued_at=0, expires_at=300, nonce="initial")
        token = signer.issue(original)
        applied: list[str] = []
        rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
        rotator.register(
            "agent",
            signer=signer,
            token=token,
            lease_is_active=lambda *_: True,
        )
        rotator.add_target("agent", applied.append)

        rotator.refresh_if_needed()

        self.assertEqual(len(applied), 1)
        refreshed = signer.verify(applied[0])
        self.assertNotEqual(refreshed.nonce, "initial")
        self.assertEqual(refreshed.expires_at - refreshed.issued_at, 300)
        for field in (
            "run_id",
            "agent_revision_id",
            "allowed_model_roles",
            "max_tokens",
            "max_cost",
        ):
            self.assertEqual(getattr(refreshed, field), getattr(original, field))

    def test_skips_rotation_while_token_has_lifetime_left(self):
        clock = [239]
        signer = self._signer(clock, lease_is_active=lambda *_: True)
        token = signer.issue(_claims("run-1", issued_at=0, expires_at=300, nonce="initial"))
        applied: list[str] = []
        rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
        rotator.register("agent", signer=signer, token=token, lease_is_active=None)
        rotator.add_target("agent", applied.append)

        rotator.refresh_if_needed()

        self.assertEqual(applied, [])

    def test_reissues_expired_token_only_under_an_active_lease(self):
        clock = [400]
        signer = self._signer(clock, lease_is_active=lambda *_: True)
        original = _claims("run-1", issued_at=0, expires_at=300, nonce="initial")
        token = signer.issue(original)
        applied: list[str] = []
        rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
        rotator.register(
            "agent",
            signer=signer,
            token=token,
            lease_is_active=lambda run_id, agent_id: (run_id, agent_id)
            == ("run-1", _REVISION_ID),
        )
        rotator.add_target("agent", applied.append)

        rotator.refresh_if_needed()

        self.assertEqual(len(applied), 1)
        refreshed = signer.verify(applied[0])
        self.assertEqual(refreshed.issued_at, 400)
        self.assertEqual(refreshed.run_id, original.run_id)
        self.assertEqual(refreshed.max_tokens, original.max_tokens)

    def test_rejects_expired_token_without_an_active_lease(self):
        clock = [400]
        signer = self._signer(clock, lease_is_active=lambda *_: True)
        token = signer.issue(_claims("run-1", issued_at=0, expires_at=300, nonce="initial"))
        applied: list[str] = []
        rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
        rotator.register("agent", signer=signer, token=token, lease_is_active=lambda *_: False)
        rotator.add_target("agent", applied.append)

        with self.assertRaises(CapabilityScopeError):
            rotator.refresh_if_needed()
        self.assertEqual(applied, [])

    def test_rejects_refresh_when_no_lease_probe_is_wired(self):
        clock = [245]
        signer = self._signer(clock)  # no lease callback, mirrors production wiring today
        token = signer.issue(_claims("run-1", issued_at=0, expires_at=300, nonce="initial"))
        applied: list[str] = []
        rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
        rotator.register("agent", signer=signer, token=token, lease_is_active=None)
        rotator.add_target("agent", applied.append)

        with self.assertRaises(CapabilityScopeError):
            rotator.refresh_if_needed()
        self.assertEqual(applied, [])

    def test_slots_rotate_independently(self):
        clock = [245]
        signer = self._signer(clock, lease_is_active=lambda *_: True)
        agent_token = signer.issue(
            _claims("run-1", issued_at=240, expires_at=540, nonce="fresh-agent")
        )
        evaluator_claims = _claims("run-1", issued_at=0, expires_at=300, nonce="stale-evaluator")
        evaluator_token = signer.issue(evaluator_claims)
        agent_applied: list[str] = []
        evaluator_applied: list[str] = []
        rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
        rotator.register("agent", signer=signer, token=agent_token, lease_is_active=None)
        rotator.add_target("agent", agent_applied.append)
        rotator.register(
            "evaluator",
            signer=signer,
            token=evaluator_token,
            lease_is_active=lambda *_: True,
        )
        rotator.add_target("evaluator", evaluator_applied.append)

        rotator.refresh_if_needed()

        self.assertEqual(agent_applied, [])
        self.assertEqual(len(evaluator_applied), 1)


class ApplyCapabilityTokenTargetTest(unittest.TestCase):
    """Every long-lived token holder must accept a refreshed token in place."""

    def test_legacy_gateway_client_sends_refreshed_token(self):
        gateway = _LegacyRecordingGateway()
        client = LegacyGatewayClient(
            gateway=gateway,
            model="fixture-model",
            capability_token="token-a",
            agent_revision_id="secrl-react-v1",
            max_output_tokens=32,
        )
        episode = EpisodeContext(
            run_id="run-1",
            case_id="incident_1:0:hash",
            attempt_id="attempt-1",
            public_input={"context": "ctx", "question": "question"},
            tools=(),
            max_steps=4,
        )
        client.bind_episode(episode)

        client.create(messages=[{"role": "user", "content": "first"}])
        client.apply_capability_token("token-b")
        client.create(messages=[{"role": "user", "content": "second"}])

        presented = [
            request.capability_token.get_secret_value() for request in gateway.requests
        ]
        self.assertEqual(presented, ["token-a", "token-b"])
        self.assertEqual(
            client.model_gateway_binding,
            hashlib.sha256(b"token-b").hexdigest(),
        )

    def test_evaluator_gateway_client_sends_refreshed_token(self):
        gateway = _LegacyRecordingGateway()
        client = EvaluatorGatewayClient(
            gateway=gateway,
            model="fixture-model",
            capability_token="evaluator-a",
            agent_revision_id=_REVISION_ID,
            max_output_tokens=64,
        )
        client.bind_attempt(run_id="run-1", case_id="case-1", attempt_id="attempt-1")

        client.complete(prompt="first", parameters={})
        client.apply_capability_token("evaluator-b")
        client.complete(prompt="second", parameters={})

        presented = [
            request.capability_token.get_secret_value() for request in gateway.requests
        ]
        self.assertEqual(presented, ["evaluator-a", "evaluator-b"])

    def test_budget_guard_rebinds_runtime_validation_and_keeps_ledger(self):
        clock = [1_000]
        signer = CapabilitySigner(
            b"g" * 32,
            now=lambda: clock[0],
            budget_store=InMemoryCapabilityBudgetStore(),
        )
        original = CapabilityClaims(
            run_id="delta-run",
            agent_revision_id=_REVISION_ID,
            allowed_model_roles=("agent",),
            max_tokens=20,
            max_cost=Decimal("5"),
            issued_at=1_000,
            expires_at=1_300,
            nonce="guard-original",
        )
        token = signer.issue(original)
        guard = CapabilityBudgetGuard(
            signer=signer,
            token=token,
            run_id="delta-run",
            agent_revision_id=_REVISION_ID,
        )
        signer.authorize_usage(
            token,
            additional_tokens=3,
            additional_cost=Decimal("0.25"),
            expected_run="delta-run",
            expected_agent=_REVISION_ID,
            model_role="agent",
            request_id="before-rotation",
        )

        refreshed_claims = original.model_copy(
            update={"issued_at": 1_000, "expires_at": 1_300, "nonce": "guard-refreshed"}
        )
        guard.apply_capability_token(signer.issue(refreshed_claims))
        refreshed_token = signer.issue(refreshed_claims)

        stale_runtime = SimpleNamespace(
            model_gateway_binding=hashlib.sha256(token.encode("utf-8")).hexdigest()
        )
        fresh_runtime = SimpleNamespace(
            model_gateway_binding=hashlib.sha256(refreshed_token.encode("utf-8")).hexdigest()
        )
        with self.assertRaises(CapabilityBudgetError):
            guard.validate_runtime(stale_runtime)
        guard.validate_runtime(fresh_runtime)

        self.assertEqual(guard.usage().prompt_tokens, 3)
        settled = signer.budget_snapshot(
            refreshed_token,
            expected_run="delta-run",
            expected_agent=_REVISION_ID,
        )
        self.assertEqual(settled.consumed_tokens, 3)
        self.assertEqual(settled.reserved_tokens, 0)


class RunnerLeaseProbeTest(unittest.TestCase):
    """The repository probe that authorizes capability refresh."""

    def test_probe_tracks_owner_fence_and_expiry(self):
        with tempfile.TemporaryDirectory() as directory:
            clock = [1_000]
            session_factory = create_engine_and_session(
                Path(directory) / "platform.sqlite3", create=True
            )
            first = RunnerRepository(
                session_factory,
                owner_id="worker-1",
                now=lambda: clock[0],
                lease_seconds=10,
            )
            handle = first.create_protocol_smoke_run(
                name="lease-probe",
                adapter=ProtocolSmokeAdapter.load_default(),
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001",),
            )
            self.assertFalse(first.run_lease_is_active(handle.run_id, _REVISION_ID))

            self.assertEqual(
                first.prepare_for_run(handle.task_id, handle.run_id),
                "RUNNING",
            )
            self.assertTrue(first.run_lease_is_active(handle.run_id, "any-agent"))

            second = RunnerRepository(
                session_factory,
                owner_id="worker-2",
                now=lambda: clock[0],
                lease_seconds=10,
            )
            self.assertFalse(second.run_lease_is_active(handle.run_id, _REVISION_ID))

            clock[0] = 1_011
            self.assertFalse(first.run_lease_is_active(handle.run_id, _REVISION_ID))
            self.assertEqual(
                second.prepare_for_run(handle.task_id, handle.run_id),
                "RUNNING",
            )
            self.assertFalse(first.run_lease_is_active(handle.run_id, _REVISION_ID))
            self.assertTrue(second.run_lease_is_active(handle.run_id, _REVISION_ID))

    def test_unknown_run_is_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            repo = RunnerRepository(
                create_engine_and_session(
                    Path(directory) / "platform.sqlite3", create=True
                )
            )
            self.assertFalse(repo.run_lease_is_active("missing-run", _REVISION_ID))


class CapabilitySignerLeaseWiringTest(unittest.TestCase):
    """capability_signer must forward the lease probe into the signer."""

    def setUp(self) -> None:
        self._directory = tempfile.TemporaryDirectory()
        self.settings = Settings(
            data_dir=Path(self._directory.name),
            master_key="00" * 32,
            session_secret="s" * 32,
        )

    def tearDown(self) -> None:
        self._directory.cleanup()

    def _token(self, signer: CapabilitySigner) -> str:
        now = int(time.time())
        return signer.issue(
            CapabilityClaims(
                run_id="wired-run",
                agent_revision_id=_REVISION_ID,
                allowed_model_roles=("agent",),
                max_tokens=100,
                max_cost=Decimal("1"),
                issued_at=now,
                expires_at=now + 300,
                nonce="wiring",
            )
        )

    def test_factory_forwards_the_lease_probe(self):
        leases: set[tuple[str, str]] = set()
        signer = capability_signer(
            self.settings,
            lease_is_active=lambda run_id, agent_id: (run_id, agent_id) in leases,
        )
        token = self._token(signer)

        with self.assertRaises(CapabilityScopeError):
            signer.refresh(token)

        leases.add(("wired-run", _REVISION_ID))
        refreshed = signer.refresh(token)
        self.assertNotEqual(signer.verify(refreshed).nonce, "wiring")

    def test_factory_default_keeps_refresh_disabled(self):
        signer = capability_signer(self.settings)
        token = self._token(signer)
        with self.assertRaises(CapabilityScopeError):
            signer.refresh(token)


class CapabilityRotationEngineTest(unittest.IsolatedAsyncioTestCase):
    """Engine-level reproduction of the multi-case expiry failure."""

    def _build(self, root: Path, case_ids):
        adapter = ProtocolSmokeAdapter.load_default()
        repo = RunnerRepository(
            create_engine_and_session(root / "platform.sqlite3", create=True)
        )
        handle = repo.create_protocol_smoke_run(
            name="capability-rotation",
            adapter=adapter,
            agent_revision=DeterministicSmokeAgent.revision(),
            case_ids=tuple(case_ids),
            budget={"max_tokens": 1_000_000, "max_cost": "10"},
        )
        return adapter, repo, handle, LocalArtifactStore(root / "artifacts")

    async def test_refreshed_capabilities_keep_multi_case_run_alive_past_token_lifetime(
        self,
    ):
        """Three cases whose wall time crosses t=300 must all succeed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self._build(
                root, ("smoke-001", "smoke-002", "smoke-003")
            )
            clock = [0]
            signer = CapabilitySigner(
                b"r" * 32,
                now=lambda: clock[0],
                budget_store=InMemoryCapabilityBudgetStore(),
                lease_is_active=repo.run_lease_is_active,
            )
            token = signer.issue(
                _claims(handle.run_id, issued_at=0, expires_at=300, nonce="initial")
            )
            provider = _RecordingProvider()
            gateway = ModelGateway(
                provider=provider,
                pricing=Pricing(input_per_million=0, output_per_million=0),
                capability_signer=signer,
            )
            guard = CapabilityBudgetGuard(
                signer=signer,
                token=token,
                run_id=handle.run_id,
                agent_revision_id=_REVISION_ID,
            )
            holder = {"token": token}
            rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
            rotator.register(
                "agent",
                signer=signer,
                token=token,
                lease_is_active=repo.run_lease_is_active,
            )
            rotator.add_target("agent", lambda value: holder.update(token=value))
            rotator.add_target("agent", guard.apply_capability_token)

            def slow_cases(_case_id, _artifact):
                clock[0] = max(clock[0], 310)

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=lambda: _HolderGatewayAgent(
                    gateway, holder, handle.run_id, clock=clock, step_seconds=120
                ),
                model_budget_guard=guard,
                capability_rotator=rotator,
                after_artifact_write=slow_cases,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "SUCCEEDED", repo.attempt_errors(handle.task_id))
            # Three protocol-smoke steps per case, all past the original expiry.
            self.assertEqual(len(provider.tokens), 9)
            self.assertGreater(len(set(provider.tokens)), 1)
            self.assertNotEqual(provider.tokens[-1], provider.tokens[0])
            settled = signer.budget_snapshot(
                provider.tokens[-1],
                expected_run=handle.run_id,
                expected_agent=_REVISION_ID,
            )
            self.assertEqual(settled.reserved_tokens, 0)
            self.assertEqual(settled.consumed_tokens, 99)
            self.assertEqual(guard.usage().prompt_tokens, 99)

    async def test_rotation_between_steps_survives_expiry_within_one_case(self):
        """A single case whose later steps cross t=300 must still succeed."""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self._build(root, ("smoke-001",))
            clock = [0]
            signer = CapabilitySigner(
                b"s" * 32,
                now=lambda: clock[0],
                budget_store=InMemoryCapabilityBudgetStore(),
                lease_is_active=repo.run_lease_is_active,
            )
            token = signer.issue(
                _claims(handle.run_id, issued_at=0, expires_at=300, nonce="initial")
            )
            provider = _RecordingProvider()
            gateway = ModelGateway(
                provider=provider,
                pricing=Pricing(input_per_million=0, output_per_million=0),
                capability_signer=signer,
            )
            guard = CapabilityBudgetGuard(
                signer=signer,
                token=token,
                run_id=handle.run_id,
                agent_revision_id=_REVISION_ID,
            )
            holder = {"token": token}
            rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
            rotator.register(
                "agent",
                signer=signer,
                token=token,
                lease_is_active=repo.run_lease_is_active,
            )
            rotator.add_target("agent", lambda value: holder.update(token=value))
            rotator.add_target("agent", guard.apply_capability_token)

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=lambda: _HolderGatewayAgent(
                    gateway, holder, handle.run_id, clock=clock, step_seconds=160
                ),
                model_budget_guard=guard,
                capability_rotator=rotator,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "SUCCEEDED", repo.attempt_errors(handle.task_id))
            self.assertEqual(len(provider.tokens), 3)
            # The third step ran past the original expiry on a rotated token.
            self.assertNotEqual(provider.tokens[2], provider.tokens[0])

    async def test_rotation_without_an_active_lease_fails_the_attempt(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter, repo, handle, store = self._build(
                root, ("smoke-001", "smoke-002")
            )
            clock = [0]
            signer = CapabilitySigner(
                b"l" * 32,
                now=lambda: clock[0],
                budget_store=InMemoryCapabilityBudgetStore(),
            )
            token = signer.issue(
                _claims(handle.run_id, issued_at=0, expires_at=300, nonce="initial")
            )
            provider = _RecordingProvider()
            gateway = ModelGateway(
                provider=provider,
                pricing=Pricing(input_per_million=0, output_per_million=0),
                capability_signer=signer,
            )
            guard = CapabilityBudgetGuard(
                signer=signer,
                token=token,
                run_id=handle.run_id,
                agent_revision_id=_REVISION_ID,
            )
            holder = {"token": token}
            rotator = CapabilityTokenRotator(now=lambda: clock[0], threshold_seconds=60)
            rotator.register(
                "agent",
                signer=signer,
                token=token,
                lease_is_active=None,  # misconfigured runner: no lease probe wired
            )
            rotator.add_target("agent", lambda value: holder.update(token=value))
            rotator.add_target("agent", guard.apply_capability_token)

            def drift_past_threshold(_case_id, _artifact):
                clock[0] = 260

            status = await RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=lambda: _HolderGatewayAgent(
                    gateway, holder, handle.run_id, clock=clock, step_seconds=30
                ),
                model_budget_guard=guard,
                capability_rotator=rotator,
                after_artifact_write=drift_past_threshold,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "FAILED")
            self.assertEqual(len(provider.tokens), 3)
            self.assertEqual(
                repo.attempt_errors(handle.task_id),
                ({"code": "AGENT_RUNTIME_ERROR"},),
            )


if __name__ == "__main__":
    unittest.main()
