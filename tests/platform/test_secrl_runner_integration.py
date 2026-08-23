from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from secrl_platform.agents.builtin import builtin_manifest
from secrl_platform.agents.protocol import AgentRevisionRef, UsageSnapshot
from secrl_platform.benchmarks.protocol import SubmitAction
from secrl_platform.benchmarks.secrl import SecRLAdapter, SecRLRunSpec
from secrl_platform.runner.engine import RunnerEngine
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import CaseRecordORM, RunORM
from sqlalchemy import select


CASE_ID = "incident_134:0:f85431d5ee76a2f65908ea5dc308418ff5328582d4ee45c0b73b80eaa0dd5ec7"


class _SubmitRuntime:
    model_access = "none"
    model_gateway_binding = None
    name = "fixture"

    async def reset(self, episode):
        self.max_steps = episode.max_steps

    async def act(self, _observation):
        return SubmitAction(type="submit", answer="170.54.121.63")

    def usage(self):
        return UsageSnapshot()

    async def close(self):
        return None


class SecRLRunnerIntegrationTest(unittest.IsolatedAsyncioTestCase):
    async def test_repository_and_runner_execute_secrl_from_frozen_run_spec(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sessions = create_engine_and_session(root / "platform.sqlite3", create=True)
            repository = RunnerRepository(sessions)
            adapter = SecRLAdapter(
                query_executor=lambda _scenario, _query: ([], True),
                run_spec=SecRLRunSpec(max_steps=7, max_str_len=4096, max_entry_return=9),
            )
            manifest = builtin_manifest("secrl-baseline-v1")
            handle = repository.create_benchmark_run(
                name="SecRL runner integration",
                adapter=adapter,
                agent_revision=AgentRevisionRef(
                    id=manifest.agent_id,
                    manifest=manifest,
                    manifest_sha256=manifest.sha256(),
                ),
                case_ids=(CASE_ID,),
                run_limits={"max_steps": 7, "max_str_len": 4096, "max_entry_return": 9},
                agent_parameters={"retry_num": 2},
            )
            runtime = _SubmitRuntime()
            status = await RunnerEngine(
                repository=repository,
                artifact_store=LocalArtifactStore(root / "artifacts"),
                adapter=adapter,
                runtime_factory=lambda: runtime,
            ).run(handle.task_id, handle.run_id)
            self.assertEqual(status, "SUCCEEDED")
            self.assertEqual(runtime.max_steps, 7)
            with sessions() as session:
                run = session.get(RunORM, handle.run_id)
                record = session.scalar(select(CaseRecordORM))
            self.assertEqual(json.loads(run.run_spec_json)["limits"]["max_str_len"], 4096)
            self.assertEqual(record.external_id, CASE_ID)
            self.assertEqual(json.loads(record.payload_json)["scenario"]["id"], "incident_134")


if __name__ == "__main__":
    unittest.main()
