import hashlib
import json
import unittest
import uuid

from pydantic import ValidationError

from secrl_platform.benchmarks.protocol import (
    BenchmarkManifest,
    Observation,
    Scope,
    Submission,
    SubmitAction,
    ToolCallAction,
    parse_agent_action,
)
from secrl_platform.benchmarks.registry import (
    BenchmarkRegistry,
    DuplicateBenchmarkError,
    UnknownBenchmarkError,
)
from secrl_platform.benchmarks.smoke import (
    PROTOCOL_SMOKE_V1_SHA256,
    ProtocolSmokeAdapter,
)


class BenchmarkProtocolTest(unittest.TestCase):
    def test_tool_call_requires_object_arguments(self):
        action = parse_agent_action(
            {
                "type": "tool_call",
                "tool": "search",
                "arguments": {"query": "alpha"},
            }
        )
        self.assertEqual(action.tool, "search")
        with self.assertRaises(ValidationError):
            parse_agent_action(
                {"type": "tool_call", "tool": "search", "arguments": ["alpha"]}
            )

    def test_unknown_action_is_rejected(self):
        with self.assertRaises(ValidationError):
            parse_agent_action({"type": "shell", "command": "id"})

    def test_observation_records_truncation(self):
        observation = Observation(type="tool_result", content={}, truncated=True)
        self.assertTrue(observation.truncated)


class BenchmarkRegistryTest(unittest.TestCase):
    class StubAdapter:
        def manifest(self):
            return BenchmarkManifest(
                benchmark_id="stub",
                name="Stub",
                version="1",
            )

    def test_duplicate_and_unknown_benchmarks_are_rejected(self):
        registry = BenchmarkRegistry()
        registry.register(self.StubAdapter())
        with self.assertRaises(DuplicateBenchmarkError):
            registry.register(self.StubAdapter())
        with self.assertRaises(UnknownBenchmarkError):
            registry.get("missing")


class ProtocolSmokeAdapterTest(unittest.TestCase):
    def setUp(self):
        self.adapter = ProtocolSmokeAdapter.load_default()

    def start_case(self, case_id):
        case = self.adapter.enumerate_cases(
            self.adapter.dataset_ref(),
            Scope(case_ids=(case_id,)),
        )[0]
        episode = self.adapter.start_episode(
            case,
            self.adapter.prepare_scenario(case.scenario),
        )
        self.assertIsNotNone(episode.ref)
        self.assertEqual(uuid.UUID(episode.ref.id).version, 4)
        return episode

    def test_smoke_search_read_submit_episode(self):
        case = self.adapter.enumerate_cases(self.adapter.dataset_ref(), Scope.all())[0]
        episode = self.adapter.start_episode(
            case,
            self.adapter.prepare_scenario(case.scenario),
        )
        search = self.adapter.execute_action(
            episode.ref,
            ToolCallAction(
                type="tool_call",
                tool="search",
                arguments={"query": "alpha"},
            ),
        )
        self.assertEqual(search.content["matches"], ["doc-alpha"])
        read = self.adapter.execute_action(
            episode.ref,
            ToolCallAction(
                type="tool_call",
                tool="read",
                arguments={"id": "doc-alpha"},
            ),
        )
        self.assertIn("17", read.content["text"])
        result = self.adapter.evaluate(episode.ref, Submission(answer="17"))
        self.assertEqual(result.reward, 1.0)

    def test_canonical_dataset_hash_and_version_are_frozen(self):
        dataset = self.adapter.dataset_ref()
        payload = json.loads(dataset.source.read_text(encoding="utf-8"))
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        digest = hashlib.sha256(canonical).hexdigest()
        self.assertEqual(digest, PROTOCOL_SMOKE_V1_SHA256)
        self.assertEqual(self.adapter.manifest().dataset_sha256, digest)
        self.assertEqual(self.adapter.manifest().dataset_version, "1.0.0")
        self.assertEqual(dataset.sha256, digest)

    def test_fixture_has_twelve_cases_and_required_coverage(self):
        dataset = self.adapter.dataset_ref()
        payload = json.loads(dataset.source.read_text(encoding="utf-8"))
        self.assertEqual(len(payload["cases"]), 12)
        tags = {tag for case in payload["cases"] for tag in case["tags"]}
        self.assertTrue(
            {
                "exact-answer",
                "normalized-answer",
                "search-read-multi-step",
                "unknown-key",
                "long-observation",
                "invalid-tool-arguments",
                "max-steps",
                "wrong-answer",
            }.issubset(tags)
        )
        public_cases = self.adapter.enumerate_cases(dataset, Scope.all())
        self.assertEqual(len(public_cases), 12)
        self.assertTrue(all("gold" not in case.public_input for case in public_cases))

    def test_normalized_and_wrong_answers_are_deterministic(self):
        normalized = self.start_case("smoke-002")
        result = self.adapter.evaluate(
            normalized.ref,
            Submission(answer="  Cafe\u0301  "),
        )
        self.assertEqual(result.reward, 1.0)
        wrong = self.start_case("smoke-009")
        result = self.adapter.evaluate(wrong.ref, Submission(answer="42"))
        self.assertEqual(result.reward, 0.0)

    def test_tool_errors_truncation_and_max_steps_are_enforced(self):
        unknown = self.start_case("smoke-004")
        observation = self.adapter.execute_action(
            unknown.ref,
            ToolCallAction(
                type="tool_call",
                tool="read",
                arguments={"id": "missing"},
            ),
        )
        self.assertEqual(observation.content["error"], "unknown_key")
        observation = self.adapter.execute_action(
            unknown.ref,
            ToolCallAction(type="tool_call", tool="shell", arguments={}),
        )
        self.assertEqual(observation.content["error"], "unknown_tool")

        invalid = self.start_case("smoke-006")
        observation = self.adapter.execute_action(
            invalid.ref,
            ToolCallAction(type="tool_call", tool="search", arguments={}),
        )
        self.assertEqual(observation.content["error"], "invalid_tool_arguments")

        long_episode = self.start_case("smoke-005")
        observation = self.adapter.execute_action(
            long_episode.ref,
            ToolCallAction(
                type="tool_call",
                tool="read",
                arguments={"id": "doc-long"},
            ),
        )
        self.assertTrue(observation.truncated)
        self.assertEqual(len(observation.content["text"]), 64)

        bounded = self.start_case("smoke-008")
        for query in ("eta", "eta"):
            self.adapter.execute_action(
                bounded.ref,
                ToolCallAction(
                    type="tool_call",
                    tool="search",
                    arguments={"query": query},
                ),
            )
        observation = self.adapter.execute_action(
            bounded.ref,
            ToolCallAction(
                type="tool_call",
                tool="search",
                arguments={"query": "eta"},
            ),
        )
        self.assertEqual(observation.content["error"], "max_steps_exceeded")
        self.assertTrue(observation.terminal)

    def test_all_cases_run_and_submit_is_a_registered_terminal_action(self):
        dataset = self.adapter.dataset_ref()
        payload = json.loads(dataset.source.read_text(encoding="utf-8"))
        gold_by_id = {
            record["id"]: record["gold"]["answer"] for record in payload["cases"]
        }
        self.assertEqual(
            [tool.name for tool in self.adapter.tool_definitions()],
            ["search", "read", "submit"],
        )
        episode_ids = set()
        for case in self.adapter.enumerate_cases(dataset, Scope.all()):
            lease = self.adapter.prepare_scenario(case.scenario)
            episode = self.adapter.start_episode(case, lease)
            episode_ids.add(episode.ref.id)
            result = self.adapter.evaluate(
                episode.ref,
                Submission(answer=gold_by_id[case.id]),
            )
            self.assertEqual(result.reward, 1.0)
            self.adapter.close_episode(episode.ref)
            self.adapter.release_scenario(lease)
        self.assertEqual(len(episode_ids), 12)

        submitted = self.start_case("smoke-011")
        observation = self.adapter.execute_action(
            submitted.ref,
            SubmitAction(type="submit", answer="43"),
        )
        self.assertEqual(observation.type, "submission")
        self.assertTrue(observation.terminal)


if __name__ == "__main__":
    unittest.main()
