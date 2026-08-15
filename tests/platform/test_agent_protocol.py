import unittest

from pydantic import ValidationError

from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.protocol import (
    AgentManifest,
    AgentRevisionRef,
    EpisodeContext,
)
from secrl_platform.agents.registry import (
    AgentRegistry,
    UnapprovedAgentError,
)
from secrl_platform.benchmarks.protocol import (
    Observation,
    SubmitAction,
    ToolCallAction,
    ToolDefinition,
)


def smoke_episode_context() -> EpisodeContext:
    return EpisodeContext(
        run_id="run-1",
        case_id="smoke-001",
        attempt_id="attempt-1",
        public_input={"question": "What value belongs to alpha?"},
        tools=(
            ToolDefinition(
                name="search",
                description="Search documents.",
                parameters={"type": "object"},
            ),
            ToolDefinition(
                name="read",
                description="Read one document.",
                parameters={"type": "object"},
            ),
            ToolDefinition(
                name="submit",
                description="Submit an answer.",
                parameters={"type": "object"},
            ),
        ),
        max_steps=6,
    )


def initial_smoke_observation() -> Observation:
    return Observation(
        type="episode_start",
        content={"question": "What value belongs to alpha?"},
    )


class AgentRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_deterministic_agent_returns_typed_search_read_submit_actions(self):
        runtime = DeterministicSmokeAgent()
        await runtime.reset(smoke_episode_context())

        search = await runtime.act(initial_smoke_observation())
        self.assertIsInstance(search, ToolCallAction)
        self.assertEqual(search.tool, "search")
        self.assertEqual(search.arguments, {"query": "alpha"})

        read = await runtime.act(
            Observation(
                type="tool_result",
                content={"matches": ["doc-alpha"]},
            )
        )
        self.assertIsInstance(read, ToolCallAction)
        self.assertEqual(read.tool, "read")
        self.assertEqual(read.arguments, {"id": "doc-alpha"})

        submit = await runtime.act(
            Observation(
                type="tool_result",
                content={"text": "alpha = 17", "original_length": 10},
            )
        )
        self.assertIsInstance(submit, SubmitAction)
        self.assertEqual(submit.answer, "17")
        self.assertEqual(runtime.usage().total, 0)
        await runtime.close()

    async def test_act_requires_reset_and_close_discards_episode_state(self):
        runtime = DeterministicSmokeAgent()
        with self.assertRaises(RuntimeError):
            await runtime.act(initial_smoke_observation())

        await runtime.reset(smoke_episode_context())
        await runtime.close()
        with self.assertRaises(RuntimeError):
            await runtime.act(initial_smoke_observation())

    def test_runtime_models_are_immutable_and_usage_is_non_negative(self):
        context = smoke_episode_context()
        with self.assertRaises(ValidationError):
            context.max_steps = 7
        with self.assertRaises(ValidationError):
            from secrl_platform.agents.protocol import UsageSnapshot

            UsageSnapshot(prompt_tokens=-1)

    def test_registry_rejects_unapproved_revision(self):
        manifest = AgentManifest(
            agent_id="unapproved-agent",
            name="Unapproved Agent",
            version="1.0.0",
            runtime="built_in",
        )
        revision = AgentRevisionRef(
            id="agent-revision-id",
            manifest=manifest,
            manifest_sha256=manifest.sha256(),
        )
        registry = AgentRegistry(approved_revision_ids=set())
        registry.register(revision, DeterministicSmokeAgent)

        with self.assertRaises(UnapprovedAgentError):
            registry.resolve("agent-revision-id")

    def test_registry_resolves_only_matching_approved_manifest(self):
        revision = DeterministicSmokeAgent.revision()
        registry = AgentRegistry(approved_revision_ids={revision.id})
        registry.register(revision, DeterministicSmokeAgent)

        runtime = registry.resolve(revision.id)
        self.assertIsInstance(runtime, DeterministicSmokeAgent)

        with self.assertRaises(ValidationError):
            AgentRevisionRef(
                id=revision.id,
                manifest=revision.manifest,
                manifest_sha256="0" * 64,
            )


if __name__ == "__main__":
    unittest.main()
