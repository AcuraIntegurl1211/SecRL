from __future__ import annotations

import asyncio
import hashlib
import unittest

from secrl_platform.agents.builtin import (
    BUILTIN_AGENT_IDS,
    BuiltinAgentAdapter,
    InvalidLegacyAction,
    normalize_legacy_action,
    LegacyGatewayClient,
)
from secrl_platform.agents.protocol import AgentManifest, EpisodeContext
from secrl_platform.benchmarks.protocol import Observation, SubmitAction, ToolCallAction


class _FakeLegacyAgent:
    def __init__(self, action):
        self.action = action
        self.reset_question = None
        self.closed = False
        self.totoal_usage = {"prompt_tokens": 2, "completion_tokens": 3}

    @property
    def name(self):
        return "fake"

    def reset(self, change_seed=True, question_dict=None):
        self.reset_question = question_dict

    def act(self, _observation):
        return self.action

    def close(self):
        self.closed = True


def _episode() -> EpisodeContext:
    return EpisodeContext(
        run_id="run-1",
        case_id="incident_1:0:hash",
        attempt_id="attempt-1",
        public_input={"context": "ctx", "question": "question"},
        tools=(),
        max_steps=4,
    )


class BuiltinAgentAdapterTest(unittest.TestCase):
    def test_explicit_allowlist_contains_all_approved_secgym_agents(self):
        self.assertEqual(
            set(BUILTIN_AGENT_IDS),
            {
                "secrl-baseline-v1",
                "secrl-expel-v1",
                "secrl-mas-v1",
                "secrl-prompt-sauce-v1",
                "secrl-prompt-sauce-reflexion-v1",
                "secrl-react-v1",
                "secrl-react-reflexion-v1",
            },
        )

    def test_legacy_execute_and_submit_are_structured_actions(self):
        self.assertEqual(
            normalize_legacy_action("execute[SELECT 1]"),
            ToolCallAction(type="tool_call", tool="sql_query", arguments={"query": "SELECT 1"}),
        )
        self.assertEqual(
            normalize_legacy_action("submit[nathans]"),
            SubmitAction(type="submit", answer="nathans"),
        )
        self.assertEqual(
            normalize_legacy_action(("SELECT 1", False)),
            ToolCallAction(type="tool_call", tool="sql_query", arguments={"query": "SELECT 1"}),
        )

    def test_parser_rejects_unstructured_or_multiple_actions(self):
        for raw in ("SELECT 1", "execute[SELECT 1] submit[x]", "execute[]", "submit[]"):
            with self.assertRaises(InvalidLegacyAction):
                normalize_legacy_action(raw)

    def test_factory_rejects_nested_credentials(self):
        from secrl_platform.agents.builtin import _safe_config

        with self.assertRaises(ValueError):
            _safe_config({"config_list": [{"model": "x", "api_key": "hidden"}]})
        self.assertEqual(_safe_config({"max_tokens": 10}), {"max_tokens": 10})

    def test_reset_passes_question_dict_required_by_expel(self):
        legacy = _FakeLegacyAgent("submit[ok]")
        runtime = BuiltinAgentAdapter(legacy)
        asyncio.run(runtime.reset(_episode()))
        self.assertEqual(legacy.reset_question, {"context": "ctx", "question": "question"})

    def test_act_usage_and_close_are_runtime_compatible(self):
        legacy = _FakeLegacyAgent(("execute[SELECT 1]", False))
        runtime = BuiltinAgentAdapter(legacy)
        asyncio.run(runtime.reset(_episode()))
        action = asyncio.run(runtime.act(Observation(type="episode_start", content={})))
        self.assertIsInstance(action, ToolCallAction)
        self.assertEqual(runtime.usage().prompt_tokens, 2)
        self.assertEqual(runtime.usage().completion_tokens, 3)
        asyncio.run(runtime.close())
        self.assertTrue(legacy.closed)

    def test_manifest_is_not_derived_from_model_name(self):
        manifest = AgentManifest(
            agent_id="secrl-baseline",
            name="SecRL Baseline",
            version="1.0.0",
            runtime="built_in",
        )
        runtime = BuiltinAgentAdapter(_FakeLegacyAgent("yield[wait]"), manifest=manifest)
        self.assertEqual(runtime.model_access, "platform_gateway")
        self.assertIsNone(runtime.model_gateway_binding)

    def test_platform_gateway_runtime_exposes_capability_binding(self):
        token = "signed-capability-token"
        client = LegacyGatewayClient(
            gateway=object(),
            model="fixture",
            capability_token=token,
            agent_revision_id="secrl-baseline-v1",
            max_output_tokens=32,
        )
        runtime = BuiltinAgentAdapter(_FakeLegacyAgent("submit[ok]"), model_client=client)
        self.assertEqual(
            runtime.model_gateway_binding,
            hashlib.sha256(token.encode("utf-8")).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
