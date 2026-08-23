import tempfile
import unittest
from pathlib import Path

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.runner.engine import RunnerEngine
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session


class InjectedProcessCrash(BaseException):
    pass


class RunnerRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def test_interrupted_case_is_retried_and_orphan_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "platform.sqlite3"
            session_factory = create_engine_and_session(database_path, create=True)
            adapter = ProtocolSmokeAdapter.load_default()
            repo = RunnerRepository(
                session_factory,
                owner_id="crashed-worker",
                now=lambda: 1_000,
                lease_seconds=10,
            )
            handle = repo.create_protocol_smoke_run(
                name="recovery",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001", "smoke-002", "smoke-003"),
            )
            store = LocalArtifactStore(root / "artifacts")
            crashed = False

            def crash_after_artifact(case_id, _artifact):
                nonlocal crashed
                if case_id == "smoke-002" and not crashed:
                    crashed = True
                    raise InjectedProcessCrash()

            first = RunnerEngine(
                repository=repo,
                artifact_store=store,
                adapter=adapter,
                runtime_factory=DeterministicSmokeAgent,
                after_artifact_write=crash_after_artifact,
            )
            with self.assertRaises(InjectedProcessCrash):
                await first.run(handle.task_id, handle.run_id)

            restarted_repo = RunnerRepository(
                create_engine_and_session(database_path, create=False),
                owner_id="replacement-worker",
                now=lambda: 1_011,
                lease_seconds=10,
            )
            restarted = RunnerEngine(
                repository=restarted_repo,
                artifact_store=store,
                adapter=ProtocolSmokeAdapter.load_default(),
                runtime_factory=DeterministicSmokeAgent,
            )
            status = await restarted.run(handle.task_id, handle.run_id)

            self.assertEqual(status, "SUCCEEDED")
            self.assertEqual(
                restarted_repo.final_attempt_count(handle.task_id, "smoke-001"),
                1,
            )
            self.assertEqual(
                restarted_repo.attempt_count(handle.task_id, "smoke-002"),
                2,
            )
            self.assertEqual(restarted_repo.final_result_count(handle.task_id), 3)
            self.assertTrue(restarted_repo.unreferenced_artifacts(store))

    async def test_pause_request_survives_crash_before_case_commit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database_path = root / "platform.sqlite3"
            adapter = ProtocolSmokeAdapter.load_default()
            repo = RunnerRepository(
                create_engine_and_session(database_path, create=True),
                owner_id="paused-crashed-worker",
                now=lambda: 1_000,
                lease_seconds=10,
            )
            handle = repo.create_protocol_smoke_run(
                name="pause-recovery",
                adapter=adapter,
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001", "smoke-002"),
            )
            store = LocalArtifactStore(root / "artifacts")

            def pause_then_crash(_case_id, _artifact):
                repo.request_pause(handle.task_id, handle.run_id)
                raise InjectedProcessCrash()

            with self.assertRaises(InjectedProcessCrash):
                await RunnerEngine(
                    repository=repo,
                    artifact_store=store,
                    adapter=adapter,
                    runtime_factory=DeterministicSmokeAgent,
                    after_artifact_write=pause_then_crash,
                ).run(handle.task_id, handle.run_id)

            restarted_repo = RunnerRepository(
                create_engine_and_session(database_path, create=False),
                owner_id="paused-replacement-worker",
                now=lambda: 1_011,
                lease_seconds=10,
            )
            status = await RunnerEngine(
                repository=restarted_repo,
                artifact_store=store,
                adapter=ProtocolSmokeAdapter.load_default(),
                runtime_factory=DeterministicSmokeAgent,
            ).run(handle.task_id, handle.run_id)

            self.assertEqual(status, "PAUSED")
            self.assertEqual(restarted_repo.final_result_count(handle.task_id), 1)
            self.assertEqual(restarted_repo.checkpoint(handle.task_id, handle.run_id), 1)


if __name__ == "__main__":
    unittest.main()
