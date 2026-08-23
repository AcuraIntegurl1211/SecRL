from __future__ import annotations

import hashlib
import json
import tempfile
import unittest
from pathlib import Path

from tests.failure_analysis.test_cli import make_fixture
from secrl_platform.analysis.service import (
    AnalysisInputs,
    FailureAnalysisService,
    HumanReviewStore,
    ReviewRecord,
    HumanReviewRepository,
    AnalysisRunRepository,
)
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import (
    AnalysisRunORM,
    ArtifactORM,
    AttributionORM,
    AuditEventORM,
    CaseAttemptORM,
    CaseRecordORM,
    HumanReviewORM,
    LocalUserORM,
)


class AnalysisServiceTest(unittest.TestCase):
    def _inputs(self, root: Path) -> AnalysisInputs:
        values = {
            "agent": [{"question_dict": {"question": "q", "answer": "a"}}],
            "env": [{"question": {"question": "q", "answer": "a"}, "trajectory": []}],
            "question": [{"question": "q", "answer": "a"}],
            "taxonomy": {"taxonomy_version": "taxonomy_v1", "categories": [], "always_human_review": [], "review_sampling": {}},
        }
        paths = {}
        for name, value in values.items():
            path = root / f"{name}.json"
            path.write_text(json.dumps(value, sort_keys=True), encoding="utf-8")
            paths[name] = path
        return AnalysisInputs(
            agent_json=paths["agent"],
            env_json=paths["env"],
            question_json=paths["question"],
            taxonomy=paths["taxonomy"],
        )

    def test_inputs_are_hash_verified_before_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            inputs = self._inputs(root)
            frozen = inputs.freeze()
            with self.assertRaises(TypeError):
                frozen.hashes["agent"] = "tampered"
            inputs.agent_json.write_text("tampered", encoding="utf-8")
            with self.assertRaises(ValueError):
                frozen.verify()

    def test_materialized_inputs_are_read_only_and_manifested(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = self._inputs(root)
            target = root / "materialized"
            inputs = FailureAnalysisService().materialize_inputs(source, target)
            manifest = target / "input_manifest.json"
            self.assertTrue(manifest.is_file())
            self.assertEqual(inputs.freeze().verify(), inputs.freeze().hashes)
            self.assertEqual(manifest.stat().st_mode & 0o222, 0)

    def test_human_review_is_append_only_and_revisioned(self):
        store = HumanReviewStore()
        first = store.append(
            ReviewRecord(
                attribution_id="attr-1",
                revision=1,
                prior_revision=None,
                reviewer_user_id="reviewer-1",
                primary="GOLD",
                secondary=["ANSWER"],
                confidence="high",
                evidence=["source hash"],
                notes="confirmed",
            )
        )
        self.assertEqual(first.revision, 1)
        second = store.append(
            ReviewRecord(
                attribution_id="attr-1",
                revision=2,
                prior_revision=1,
                reviewer_user_id="reviewer-2",
                primary="ANSWER",
                secondary=(),
                confidence="medium",
                evidence=("new evidence",),
                notes="updated",
            )
        )
        self.assertEqual(store.history("attr-1"), (first, second))
        with self.assertRaises(AttributeError):
            first.secondary.append("LOOP")
        with self.assertRaises(ValueError):
            store.append(second)

    def test_human_review_repository_persists_revision_and_audit_without_mutating_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            sessions = create_engine_and_session(Path(directory) / "reviews.sqlite3", create=True)
            # Reuse the schema's foreign-key chain from a tiny Protocol-Smoke run.
            from secrl_platform.agents.builtin import DeterministicSmokeAgent
            from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
            from secrl_platform.runner.recovery import RunnerRepository

            runner = RunnerRepository(sessions)
            handle = runner.create_protocol_smoke_run(
                name="review fixture",
                adapter=ProtocolSmokeAdapter.load_default(),
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001",),
            )
            with sessions.begin() as session:
                reviewer = LocalUserORM(username="reviewer", password_hash="not-used", status="ACTIVE")
                session.add(reviewer)
                session.flush()
                reviewer_id = reviewer.id
                case = session.query(CaseRecordORM).one()
                attempt = CaseAttemptORM(
                    run_id=handle.run_id,
                    case_id=case.id,
                    attempt_no=1,
                    status="SUCCEEDED",
                    is_final=True,
                )
                session.add(attempt)
                session.flush()
                attribution = AttributionORM(
                    case_attempt_id=attempt.id,
                    taxonomy="taxonomy_v1",
                    label="ANSWER",
                    confidence=0.7,
                    evidence_json='["automatic"]',
                )
                session.add(attribution)
                session.flush()
                attribution_id = attribution.id

            repository = HumanReviewRepository(sessions)
            first = repository.submit(
                attribution_id=attribution_id,
                reviewer_user_id=reviewer_id,
                primary="GOLD",
                secondary=("ANSWER",),
                confidence="high",
                evidence=("artifact:abc",),
                notes="confirmed",
            )
            second = repository.submit(
                attribution_id=attribution_id,
                reviewer_user_id=reviewer_id,
                primary="ANSWER",
                secondary=(),
                confidence="medium",
                evidence=("artifact:def",),
                notes="revised",
            )
            self.assertEqual((first.revision, second.revision), (1, 2))
            self.assertEqual(second.prior_review_id, first.id)
            self.assertEqual(repository.history(attribution_id), (first, second))
            with sessions() as session:
                automatic = session.get(AttributionORM, attribution_id)
                self.assertEqual(automatic.label, "ANSWER")
                self.assertEqual(session.query(HumanReviewORM).count(), 2)
                self.assertEqual(session.query(AuditEventORM).filter_by(action="human_review.append").count(), 2)

    def test_service_never_constructs_docker_or_shell_commands(self):
        source = Path("secrl_platform/analysis/service.py").read_text(encoding="utf-8")
        self.assertNotIn("docker.from_env", source)
        self.assertNotIn("shell=True", source)

    def test_service_runs_existing_failure_analysis_and_verifies_artifacts(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_fixture(root)
            run = FailureAnalysisService().run(
                AnalysisInputs(
                    agent_json=paths["agent"],
                    env_json=paths["env"],
                    question_json=paths["questions"],
                    taxonomy=paths["taxonomy"],
                ),
                incident="incident_5",
                output_dir=root / "analysis",
                max_steps=15,
            )
            self.assertTrue(run.manifest.path.is_file())
            for artifact in run.outputs:
                self.assertEqual(artifact.sha256, hashlib.sha256(artifact.path.read_bytes()).hexdigest())

    def test_verified_analysis_is_registered_as_restricted_artifacts_and_sanitized_attribution(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths = make_fixture(root)
            analysis = FailureAnalysisService().run(
                AnalysisInputs(
                    agent_json=paths["agent"],
                    env_json=paths["env"],
                    question_json=paths["questions"],
                    taxonomy=paths["taxonomy"],
                ),
                incident="incident_5",
                output_dir=root / "analysis",
                max_steps=15,
            )
            sessions = create_engine_and_session(root / "platform.sqlite3", create=True)
            from secrl_platform.agents.builtin import DeterministicSmokeAgent
            from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
            from secrl_platform.runner.recovery import RunnerRepository

            runner = RunnerRepository(sessions)
            handle = runner.create_protocol_smoke_run(
                name="analysis fixture",
                adapter=ProtocolSmokeAdapter.load_default(),
                agent_revision=DeterministicSmokeAgent.revision(),
                case_ids=("smoke-001",),
            )
            with sessions.begin() as session:
                case = session.query(CaseRecordORM).one()
                attempt = CaseAttemptORM(
                    run_id=handle.run_id,
                    case_id=case.id,
                    attempt_no=1,
                    status="SUCCEEDED",
                    is_final=True,
                )
                session.add(attempt)
                session.flush()
                attempt_id = attempt.id
            store = LocalArtifactStore(root / "artifacts")
            registered = AnalysisRunRepository(sessions, store).register(
                run_id=handle.run_id,
                analysis=analysis,
                case_attempt_ids=(attempt_id,),
            )
            self.assertRegex(registered.input_manifest_sha256, r"^[0-9a-f]{64}$")
            self.assertEqual(registered.output_manifest_sha256, analysis.manifest.sha256)
            with sessions() as session:
                self.assertEqual(session.query(AnalysisRunORM).count(), 1)
                artifacts = session.query(ArtifactORM).all()
                automatic = session.query(AttributionORM).one()
            self.assertTrue(artifacts)
            self.assertTrue(all(item.visibility == "RESTRICTED" for item in artifacts))
            self.assertNotIn("server01", automatic.evidence_json)


if __name__ == "__main__":
    unittest.main()
