import json
import unittest
from decimal import Decimal
from pathlib import Path

import httpx

from examples.agent_service.app import create_app
from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.capabilities import (
    CapabilityBudgetError,
    CapabilityClaims,
    CapabilityScopeError,
    CapabilitySigner,
    ExpiredCapability,
    InvalidCapability,
)
from secrl_platform.agents.service import (
    AgentServiceRuntime,
    AgentServiceTimeout,
    HttpxAgentServiceTransport,
    InvalidAgentAction,
    ServiceConfig,
    manifest_sha256,
)
from secrl_platform.benchmarks.protocol import Observation, ToolDefinition
from secrl_platform.models.gateway import ModelGateway
from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import ModelRequest, ModelResponse, Usage
from tests.platform.test_agent_protocol import smoke_episode_context


_MANIFEST_PATH = Path("examples/agent_service/manifest.json")


def manifest_payload():
    return json.loads(_MANIFEST_PATH.read_text(encoding="utf-8"))


def valid_claims(*, expires_in_seconds=300, max_tokens=10_000, max_cost="1"):
    return CapabilityClaims(
        run_id="run-1",
        agent_revision_id=DeterministicSmokeAgent.revision().id,
        allowed_model_roles=("agent",),
        max_tokens=max_tokens,
        max_cost=Decimal(max_cost),
        issued_at=1_000,
        expires_at=1_000 + expires_in_seconds,
        nonce="nonce-1",
    )


def tamper(token):
    payload, signature = token.split(".")
    replacement = "A" if signature[-1] != "A" else "B"
    return f"{payload}.{signature[:-1]}{replacement}"


class RecordingTransport:
    def __init__(self, action, *, timeout_once=False):
        self.action = action
        self.timeout_once = timeout_once
        self.act_requests = []

    async def request(self, method, url, *, json_body=None, headers=None):
        if method == "GET" and url.endswith("/v1/manifest"):
            return manifest_payload()
        if method == "POST" and url.endswith("/v1/sessions"):
            return {"session_id": "session-1"}
        if method == "POST" and ":act" in url:
            self.act_requests.append(json_body)
            if self.timeout_once and len(self.act_requests) == 1:
                raise AgentServiceTimeout("timeout")
            return {"action": self.action, "usage": {"prompt_tokens": 0, "completion_tokens": 0}}
        if method == "POST" and url.endswith(":close"):
            return {"closed": True}
        raise AssertionError((method, url))


def service_config(token):
    manifest = manifest_payload()
    return ServiceConfig(
        endpoint="http://agent-service-reference",
        expected_manifest_sha256=manifest_sha256(manifest),
        agent_revision_id=DeterministicSmokeAgent.revision().id,
        capability_token=token,
        allowlist=("agent-service-reference",),
    )


def fake_resolver(_host, _port):
    return ("127.0.0.1",)


class CapabilityTokenTest(unittest.TestCase):
    def setUp(self):
        self.signer = CapabilitySigner(b"c" * 32, now=lambda: 1_000)

    def test_capability_rejects_tamper_expiry_scope_and_budget(self):
        token = self.signer.issue(valid_claims())
        with self.assertRaises(InvalidCapability):
            self.signer.verify(
                tamper(token),
                expected_run="run-1",
                expected_agent=DeterministicSmokeAgent.revision().id,
            )
        with self.assertRaises(ExpiredCapability):
            self.signer.verify(self.signer.issue(valid_claims(expires_in_seconds=-1)))
        with self.assertRaises(CapabilityScopeError):
            self.signer.verify(
                token,
                expected_run="run-2",
                expected_agent=DeterministicSmokeAgent.revision().id,
            )
        with self.assertRaises(CapabilityScopeError):
            self.signer.verify(token, model_role="evaluator")
        with self.assertRaises(CapabilityBudgetError):
            self.signer.authorize_usage(token, additional_tokens=10_001, additional_cost=Decimal("0"))
        with self.assertRaises(CapabilityBudgetError):
            self.signer.authorize_usage(token, additional_tokens=0, additional_cost=Decimal("1.01"))

    def test_refresh_defaults_to_five_minutes_and_requires_active_run_lease(self):
        token = self.signer.issue(valid_claims())

        with self.assertRaises(CapabilityScopeError):
            self.signer.refresh(token, lease_active=False)

        refreshed = self.signer.refresh(token, lease_active=True)
        claims = self.signer.verify(refreshed)
        self.assertEqual(claims.expires_at - claims.issued_at, 300)
        self.assertNotEqual(claims.nonce, "nonce-1")


class AgentServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.signer = CapabilitySigner(b"c" * 32, now=lambda: 1_000)
        self.token = self.signer.issue(valid_claims())

    async def test_retry_reuses_request_id_and_sequence(self):
        transport = RecordingTransport(
            {"type": "tool_call", "tool": "search", "arguments": {"query": "alpha"}},
            timeout_once=True,
        )
        runtime = AgentServiceRuntime(
            config=service_config(self.token),
            transport=transport,
            resolver=fake_resolver,
        )
        await runtime.reset(smoke_episode_context())
        await runtime.act(Observation(type="episode_start", content={"question": "alpha"}))

        first, second = transport.act_requests
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["sequence"], second["sequence"])
        await runtime.close()

    async def test_unknown_tool_action_is_rejected_before_execution(self):
        runtime = AgentServiceRuntime(
            config=service_config(self.token),
            transport=RecordingTransport(
                {"type": "tool_call", "tool": "shell", "arguments": {}}
            ),
            resolver=fake_resolver,
        )
        await runtime.reset(smoke_episode_context())

        with self.assertRaises(InvalidAgentAction):
            await runtime.act(Observation(type="episode_start", content={}))
        await runtime.close()

    async def test_endpoint_and_manifest_must_be_allowlisted(self):
        config = service_config(self.token).model_copy(
            update={"endpoint": "http://user@agent-service-reference"}
        )
        with self.assertRaises(ValueError):
            AgentServiceRuntime(config=config, transport=RecordingTransport({}), resolver=fake_resolver)

        config = service_config(self.token).model_copy(
            update={"endpoint": "http://not-allowlisted"}
        )
        with self.assertRaises(ValueError):
            AgentServiceRuntime(config=config, transport=RecordingTransport({}), resolver=fake_resolver)

        config = service_config(self.token).model_copy(
            update={"expected_manifest_sha256": "0" * 64}
        )
        runtime = AgentServiceRuntime(
            config=config,
            transport=RecordingTransport({}),
            resolver=fake_resolver,
        )
        with self.assertRaises(ValueError):
            await runtime.reset(smoke_episode_context())

    async def test_builtin_and_asgi_service_return_equivalent_actions(self):
        app = create_app(self.signer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://agent-service-reference",
        ) as client:
            service = AgentServiceRuntime(
                config=service_config(self.token),
                transport=HttpxAgentServiceTransport(client),
                resolver=fake_resolver,
            )
            builtin = DeterministicSmokeAgent()
            context = smoke_episode_context()
            await builtin.reset(context)
            await service.reset(context)
            observations = [
                Observation(type="episode_start", content=context.public_input),
                Observation(type="tool_result", content={"matches": ["doc-alpha"]}),
                Observation(type="tool_result", content={"text": "alpha = 17"}),
            ]
            for observation in observations:
                expected = await builtin.act(observation)
                actual = await service.act(observation)
                self.assertEqual(actual, expected)
            await builtin.close()
            await service.close()

    async def test_model_gateway_rejects_tampered_capability_before_provider_call(self):
        class Provider:
            calls = 0

            async def complete(self, _request):
                self.calls += 1
                return ModelResponse(text="ok", usage=Usage(prompt=1, completion=1))

        provider = Provider()
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=1),
            capability_signer=self.signer,
        )
        request = ModelRequest(
            provider_adapter_version="v1",
            model_role="agent",
            model="fixture",
            messages=({"role": "user", "content": "hello"},),
            run_id="run-1",
            case_id="case-1",
            attempt_id="attempt-1",
            agent_revision_id=DeterministicSmokeAgent.revision().id,
            capability_token=tamper(self.token),
        )

        with self.assertRaises(InvalidCapability):
            await gateway.complete(request)
        self.assertEqual(provider.calls, 0)


if __name__ == "__main__":
    unittest.main()
