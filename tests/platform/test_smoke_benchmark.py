import unittest

from pydantic import ValidationError

from secrl_platform.benchmarks.protocol import (
    BenchmarkManifest,
    Observation,
    parse_agent_action,
)
from secrl_platform.benchmarks.registry import (
    BenchmarkRegistry,
    DuplicateBenchmarkError,
    UnknownBenchmarkError,
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


if __name__ == "__main__":
    unittest.main()
