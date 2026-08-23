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
                secondary=("ANSWER",),
                confidence="high",
                evidence=("source hash",),
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
        with self.assertRaises(ValueError):
            store.append(second)

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


if __name__ == "__main__":
    unittest.main()
