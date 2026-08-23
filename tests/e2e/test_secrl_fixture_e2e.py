from __future__ import annotations

import json
import unittest
from pathlib import Path

from secrl_platform.benchmarks.secrl import (
    SECRL_DATASET_SHA256,
    SecRLRunSpec,
    SecRLAdapter,
    replay_fixture_through_adapter,
)
from secrl_platform.benchmarks.protocol import ToolCallAction


FIXTURE = Path("tests/fixtures/platform/secrl_run_sample.json")


class SecRLFixtureParityTest(unittest.TestCase):
    def test_fixture_replays_without_docker_or_llm(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(fixture["dataset_sha256"], SECRL_DATASET_SHA256)
        result = replay_fixture_through_adapter(FIXTURE)
        self.assertEqual(result.submitted_answer, fixture["submitted_answer"])
        self.assertEqual(result.steps, fixture["steps"])
        self.assertEqual(result.reward, fixture["reward"])
        self.assertEqual(result.observation_hashes, tuple(fixture["observation_hashes"]))
        self.assertEqual(result.raw_lengths, tuple(fixture["raw_lengths"]))
        self.assertEqual(result.truncated, tuple(fixture["truncated"]))

    def test_limits_are_taken_from_frozen_run_spec(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        adapter = SecRLAdapter(run_spec=None)
        self.assertNotIn("model", json.dumps(fixture).lower())
        self.assertGreater(adapter.run_spec.max_steps, 0)
        self.assertGreater(adapter.run_spec.max_str_len, 0)
        self.assertGreater(adapter.run_spec.max_entry_return, 0)

    def test_observation_records_raw_length_and_truncation(self):
        fixture = json.loads(FIXTURE.read_text(encoding="utf-8"))
        self.assertEqual(len(fixture["raw_lengths"]), len(fixture["observation_hashes"]))
        self.assertEqual(len(fixture["truncated"]), len(fixture["observation_hashes"]))
        self.assertTrue(all(isinstance(value, int) and value >= 0 for value in fixture["raw_lengths"]))

    def test_adapter_does_not_import_docker_runtime_control(self):
        source = Path("secrl_platform/benchmarks/secrl.py").read_text(encoding="utf-8")
        self.assertNotIn("docker.from_env", source)
        self.assertNotIn("containers.get", source)

    def test_run_spec_limits_drive_entry_and_string_truncation(self):
        adapter = SecRLAdapter(
            query_executor=lambda _scenario, _query: [["abcdefghij"]] * 5,
            run_spec=SecRLRunSpec(max_steps=2, max_str_len=18, max_entry_return=2),
        )
        case = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())[0]
        lease = adapter.prepare_scenario(case.scenario)
        episode = adapter.start_episode(case, lease)
        observation = adapter.execute_action(
            episode.ref,
            ToolCallAction(type="tool_call", tool="sql_query", arguments={"query": "SELECT 1"}),
        )
        self.assertTrue(observation.truncated)
        self.assertTrue(observation.content["entry_truncated"])
        self.assertEqual(
            observation.content["original_length"],
            len(json.dumps([["abcdefghij"]] * 5, ensure_ascii=False)),
        )
        self.assertLessEqual(len(observation.content["result"]), 18)


if __name__ == "__main__":
    unittest.main()
