import json
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import httpx

from examples.agent_service.app import create_app
from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.capabilities import (
    CapabilityClaims,
    CapabilitySigner,
    InMemoryCapabilityBudgetStore,
)
from secrl_platform.agents.protocol import AgentRevisionRef
from secrl_platform.agents.service import (
    AgentServiceRuntime,
    HttpxAgentServiceTransport,
    ServiceConfig,
    manifest_sha256,
)
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.config import Settings
from secrl_platform.runner.engine import CapabilityBudgetGuard, RunnerEngine
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session


_MANIFEST = json.loads(
    Path("examples/agent_service/manifest.json").read_text(encoding="utf-8")
)


def _settings(root):
    return Settings(
        data_dir=root,
        master_key="11" * 32,
        session_secret="s" * 32,
        agent_service_allowlist=("agent-service-reference",),
    )


def _resolver(_host, _port):
    return ("127.0.0.1",)


def _semantic_trajectories(repo, task_id, store):
    payloads = repo.trajectory_payloads(task_id, store)
    for payload in payloads:
        payload.pop("run_id")
        payload.pop("attempt_id")
    return sorted(payloads, key=lambda payload: payload["case_id"])


class ProtocolSmokeE2ETest(unittest.IsolatedAsyncioTestCase):
    async def test_all_cases_complete_with_verified_equivalent_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            adapter = ProtocolSmokeAdapter.load_default()

            builtin_repo = RunnerRepository(
                create_engine_and_session(root / "builtin.sqlite3", create=True)
            )
            builtin_handle = builtin_repo.create_protocol_smoke_run(
                name="builtin-e2e",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
            )
            builtin_store = LocalArtifactStore(root / "builtin-artifacts")
            builtin_status = await RunnerEngine(
                repository=builtin_repo,
                artifact_store=builtin_store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
            ).run(builtin_handle.task_id, builtin_handle.run_id)

            signer = CapabilitySigner(
                b"e" * 32,
                now=lambda: 1_000,
                budget_store=InMemoryCapabilityBudgetStore(),
            )
            service_repo = RunnerRepository(
                create_engine_and_session(root / "service.sqlite3", create=True)
            )
            service_adapter = ProtocolSmokeAdapter.load_default()
            service_manifest = DeterministicSmokeAgent.revision().manifest.model_copy(
                update={"runtime": "service"}
            )
            service_handle = service_repo.create_protocol_smoke_run(
                name="service-e2e",
                adapter=service_adapter,
                agent_revision=AgentRevisionRef(
                    id=DeterministicSmokeAgent.revision().id,
                    manifest=service_manifest,
                    manifest_sha256=service_manifest.sha256(),
                ),
            )
            claims = CapabilityClaims(
                run_id=service_handle.run_id,
                agent_revision_id=DeterministicSmokeAgent.revision().id,
                allowed_model_roles=("agent",),
                max_tokens=0,
                max_cost=Decimal("0"),
                issued_at=1_000,
                expires_at=1_300,
                nonce="e2e",
            )
            token = signer.issue(claims)
            service_store = LocalArtifactStore(root / "service-artifacts")
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(signer)),
                base_url="http://agent-service-reference",
            ) as client:
                transport = HttpxAgentServiceTransport(client)

                def service_factory():
                    return AgentServiceRuntime.from_settings(
                        config=ServiceConfig(
                            endpoint="http://agent-service-reference",
                            expected_manifest_sha256=manifest_sha256(_MANIFEST),
                            agent_revision_id=DeterministicSmokeAgent.revision().id,
                            capability_token=token,
                        ),
                        transport=transport,
                        settings=_settings(root),
                        resolver=_resolver,
                    )

                service_status = await RunnerEngine(
                    repository=service_repo,
                    artifact_store=service_store,
                    adapter=service_adapter,
                    runtime_factory=service_factory,
                    model_budget_guard=CapabilityBudgetGuard(
                        signer=signer,
                        token=token,
                        run_id=service_handle.run_id,
                        agent_revision_id=DeterministicSmokeAgent.revision().id,
                    ),
                ).run(service_handle.task_id, service_handle.run_id)

            self.assertEqual(builtin_status, "SUCCEEDED")
            self.assertEqual(service_status, "SUCCEEDED")
            self.assertEqual(builtin_repo.final_result_count(builtin_handle.task_id), 12)
            self.assertEqual(service_repo.final_result_count(service_handle.task_id), 12)

            builtin_refs = builtin_repo.artifact_refs(
                builtin_handle.task_id, builtin_store
            )
            service_refs = service_repo.artifact_refs(
                service_handle.task_id, service_store
            )
            self.assertEqual(len(builtin_refs), 12)
            self.assertEqual(len(service_refs), 12)
            self.assertTrue(all(builtin_store.verify(ref) for ref in builtin_refs))
            self.assertTrue(all(service_store.verify(ref) for ref in service_refs))
            self.assertEqual(
                _semantic_trajectories(
                    builtin_repo, builtin_handle.task_id, builtin_store
                ),
                _semantic_trajectories(
                    service_repo, service_handle.task_id, service_store
                ),
            )


if __name__ == "__main__":
    unittest.main()
