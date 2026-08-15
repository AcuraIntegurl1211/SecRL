import json
import asyncio
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

import httpx

from examples.agent_service.app import create_app
from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.capabilities import (
    CapabilityBudgetError,
    CapabilityClaims,
    CapabilityRequestCompleted,
    CapabilityRequestInProgress,
    CapabilityScopeError,
    CapabilitySigner,
    ExpiredCapability,
    FileCapabilityBudgetStore,
    InMemoryCapabilityBudgetStore,
    InvalidCapability,
)
from secrl_platform.agents.service import (
    AgentServiceRuntime,
    AgentServiceError,
    AgentServiceProtocolError,
    AgentServiceTimeout,
    HttpxAgentServiceTransport,
    InvalidAgentAction,
    ServiceConfig,
    manifest_sha256,
)
from secrl_platform.benchmarks.protocol import Observation
from secrl_platform.config import Settings
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
    def __init__(
        self,
        action,
        *,
        timeout_once=False,
        session_timeout_once=False,
        corrupt_correlation=False,
    ):
        self.action = action
        self.timeout_once = timeout_once
        self.session_timeout_once = session_timeout_once
        self.corrupt_correlation = corrupt_correlation
        self.act_requests = []
        self.session_requests = []
        self.urls = []

    async def request(self, method, url, *, json_body=None, headers=None):
        self.urls.append(url)
        if method == "GET" and url.endswith("/v1/manifest"):
            return manifest_payload()
        if method == "POST" and url.endswith("/v1/sessions"):
            self.session_requests.append(json_body)
            if self.session_timeout_once and len(self.session_requests) == 1:
                raise AgentServiceTimeout("timeout")
            return {
                "request_id": json_body["request_id"],
                "sequence": json_body["sequence"],
                "session_id": "session-1",
            }
        if method == "POST" and ":act" in url:
            self.act_requests.append(json_body)
            if self.timeout_once and len(self.act_requests) == 1:
                raise AgentServiceTimeout("timeout")
            return {
                "request_id": (
                    "wrong-request" if self.corrupt_correlation else json_body["request_id"]
                ),
                "sequence": json_body["sequence"],
                "action": self.action,
                "usage": {"prompt_tokens": 0, "completion_tokens": 0},
            }
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
    )


def fake_resolver(_host, _port):
    return ("127.0.0.1",)


def platform_settings(*, allowlist=("agent-service-reference",)):
    return Settings(
        data_dir=Path("/tmp/secrl-agent-service-test"),
        master_key="11" * 32,
        session_secret="s" * 32,
        agent_service_allowlist=allowlist,
    )


class CapabilityTokenTest(unittest.TestCase):
    def setUp(self):
        self.active_leases = {
            ("run-1", DeterministicSmokeAgent.revision().id)
        }
        self.signer = CapabilitySigner(
            b"c" * 32,
            now=lambda: 1_000,
            budget_store=InMemoryCapabilityBudgetStore(),
            lease_is_active=lambda run_id, agent_id: (
                run_id,
                agent_id,
            )
            in self.active_leases,
        )

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

        self.active_leases.clear()
        with self.assertRaises(CapabilityScopeError):
            self.signer.refresh(token)

        self.active_leases.add(("run-1", DeterministicSmokeAgent.revision().id))
        refreshed = self.signer.refresh(token)
        claims = self.signer.verify(refreshed)
        self.assertEqual(claims.expires_at - claims.issued_at, 300)
        self.assertNotEqual(claims.nonce, "nonce-1")

    def test_token_lifetime_and_future_issued_at_are_rejected(self):
        long_lived = valid_claims().model_copy(update={"expires_at": 101_000})
        future = valid_claims().model_copy(
            update={"issued_at": 1_100, "expires_at": 1_200}
        )

        with self.assertRaises(InvalidCapability):
            self.signer.verify(self.signer.issue(long_lived))
        with self.assertRaises(InvalidCapability):
            self.signer.verify(self.signer.issue(future))

    def test_usage_is_cumulative_for_same_run_and_agent(self):
        token = self.signer.issue(valid_claims(max_tokens=10))

        self.signer.authorize_usage(
            token,
            additional_tokens=6,
            additional_cost=Decimal("0"),
            request_id="model-call-1",
        )
        with self.assertRaises(CapabilityBudgetError):
            self.signer.authorize_usage(
                token,
                additional_tokens=6,
                additional_cost=Decimal("0"),
                request_id="model-call-2",
            )

    def test_budget_state_survives_signer_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            store = FileCapabilityBudgetStore(Path(directory))
            signer = CapabilitySigner(
                b"r" * 32,
                now=lambda: 1_000,
                budget_store=store,
            )
            token = signer.issue(valid_claims(max_tokens=10))
            signer.authorize_usage(
                token,
                additional_tokens=10,
                additional_cost=Decimal("0"),
                request_id="first-process-call",
            )

            restarted = CapabilitySigner(
                b"r" * 32,
                now=lambda: 1_000,
                budget_store=FileCapabilityBudgetStore(Path(directory)),
            )
            with self.assertRaises(CapabilityBudgetError):
                restarted.authorize_usage(
                    token,
                    additional_tokens=1,
                    additional_cost=Decimal("0"),
                    request_id="second-process-call",
                )

    def test_usage_fails_closed_without_a_budget_store(self):
        signer = CapabilitySigner(b"z" * 32, now=lambda: 1_000)
        token = signer.issue(valid_claims())

        with self.assertRaises(CapabilityBudgetError):
            signer.authorize_usage(
                token,
                additional_tokens=1,
                additional_cost=Decimal("0"),
            )


class AgentServiceTest(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.signer = CapabilitySigner(b"c" * 32, now=lambda: 1_000)
        self.token = self.signer.issue(valid_claims())

    async def test_retry_reuses_request_id_and_sequence(self):
        transport = RecordingTransport(
            {"type": "tool_call", "tool": "search", "arguments": {"query": "alpha"}},
            timeout_once=True,
        )
        runtime = AgentServiceRuntime.from_settings(
            config=service_config(self.token),
            transport=transport,
            resolver=fake_resolver,
            settings=platform_settings(),
        )
        await runtime.reset(smoke_episode_context())
        await runtime.act(Observation(type="episode_start", content={"question": "alpha"}))

        first, second = transport.act_requests
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["sequence"], second["sequence"])
        await runtime.close()

    async def test_unknown_tool_action_is_rejected_before_execution(self):
        runtime = AgentServiceRuntime.from_settings(
            config=service_config(self.token),
            transport=RecordingTransport(
                {"type": "tool_call", "tool": "shell", "arguments": {}}
            ),
            resolver=fake_resolver,
            settings=platform_settings(),
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
            AgentServiceRuntime.from_settings(
                config=config,
                transport=RecordingTransport({}),
                resolver=fake_resolver,
                settings=platform_settings(),
            )

        config = service_config(self.token).model_copy(
            update={"endpoint": "http://not-allowlisted"}
        )
        with self.assertRaises(ValueError):
            AgentServiceRuntime.from_settings(
                config=config,
                transport=RecordingTransport({}),
                resolver=fake_resolver,
                settings=platform_settings(),
            )

        config = service_config(self.token).model_copy(
            update={"expected_manifest_sha256": "0" * 64}
        )
        runtime = AgentServiceRuntime.from_settings(
            config=config,
            transport=RecordingTransport({}),
            resolver=fake_resolver,
            settings=platform_settings(),
        )
        with self.assertRaises(AgentServiceProtocolError):
            await runtime.reset(smoke_episode_context())

        https_config = service_config(self.token).model_copy(
            update={"endpoint": "https://agent-service-reference"}
        )
        with self.assertRaises(ValueError):
            AgentServiceRuntime.from_settings(
                config=https_config,
                transport=RecordingTransport({}),
                resolver=fake_resolver,
                settings=platform_settings(),
            )

    async def test_builtin_and_asgi_service_return_equivalent_actions(self):
        app = create_app(self.signer)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://agent-service-reference",
        ) as client:
            service = AgentServiceRuntime.from_settings(
                config=service_config(self.token),
                transport=HttpxAgentServiceTransport(client),
                resolver=fake_resolver,
                settings=platform_settings(),
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

    async def test_session_creation_retry_reuses_request_id_and_sequence(self):
        transport = RecordingTransport({}, session_timeout_once=True)
        runtime = AgentServiceRuntime.from_settings(
            config=service_config(self.token),
            transport=transport,
            resolver=fake_resolver,
            settings=platform_settings(),
        )

        await runtime.reset(smoke_episode_context())

        first, second = transport.session_requests
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["sequence"], second["sequence"])
        await runtime.close()

    async def test_response_correlation_is_required(self):
        runtime = AgentServiceRuntime.from_settings(
            config=service_config(self.token),
            transport=RecordingTransport(
                {"type": "tool_call", "tool": "search", "arguments": {"query": "alpha"}},
                corrupt_correlation=True,
            ),
            resolver=fake_resolver,
            settings=platform_settings(),
        )
        await runtime.reset(smoke_episode_context())

        with self.assertRaises(AgentServiceProtocolError) as raised:
            await runtime.act(Observation(type="episode_start", content={}))
        self.assertEqual(raised.exception.code, "PROTOCOL_MISMATCH")

    async def test_resolution_must_be_nonempty_and_connection_uses_pinned_address(self):
        with self.assertRaises(ValueError):
            AgentServiceRuntime.from_settings(
                config=service_config(self.token),
                transport=RecordingTransport({}),
                resolver=lambda _host, _port: (),
                settings=platform_settings(),
            )

        transport = RecordingTransport({})
        runtime = AgentServiceRuntime.from_settings(
            config=service_config(self.token),
            transport=transport,
            resolver=fake_resolver,
            settings=platform_settings(),
        )
        await runtime.reset(smoke_episode_context())
        self.assertTrue(all("127.0.0.1" in url for url in transport.urls))
        await runtime.close()

    async def test_http_transport_rejects_redirects(self):
        transport = httpx.MockTransport(
            lambda _request: httpx.Response(302, headers={"Location": "http://elsewhere"})
        )
        async with httpx.AsyncClient(transport=transport) as client:
            with self.assertRaises(AgentServiceError) as raised:
                await HttpxAgentServiceTransport(client).request(
                    "GET", "http://127.0.0.1/v1/manifest"
                )
        self.assertEqual(raised.exception.code, "PROTOCOL_MISMATCH")

    async def test_http_transport_disables_injected_client_redirects(self):
        def handler(request):
            if request.url.host == "127.0.0.1":
                return httpx.Response(
                    302,
                    headers={"Location": "http://169.254.169.254/latest"},
                )
            return httpx.Response(200, json={"unsafe": True})

        async with httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            follow_redirects=True,
        ) as client:
            with self.assertRaises(AgentServiceError) as raised:
                await HttpxAgentServiceTransport(client).request(
                    "GET", "http://127.0.0.1/v1/manifest"
                )
        self.assertEqual(raised.exception.code, "PROTOCOL_MISMATCH")

    async def test_http_transport_maps_standard_transient_error_codes(self):
        for status, expected in (
            (408, "DEADLINE_EXCEEDED"),
            (429, "RATE_LIMITED"),
            (503, "UNAVAILABLE"),
        ):
            with self.subTest(status=status):
                async with httpx.AsyncClient(
                    transport=httpx.MockTransport(
                        lambda _request, status=status: httpx.Response(status)
                    )
                ) as client:
                    with self.assertRaises(AgentServiceError) as raised:
                        await HttpxAgentServiceTransport(client).request(
                            "GET", "http://127.0.0.1/v1/manifest"
                        )
                self.assertEqual(raised.exception.code, expected)
                self.assertTrue(raised.exception.transient)

        async def unavailable(_request):
            raise httpx.ConnectError("refused")

        async with httpx.AsyncClient(transport=httpx.MockTransport(unavailable)) as client:
            with self.assertRaises(AgentServiceError) as raised:
                await HttpxAgentServiceTransport(client).request(
                    "GET", "http://127.0.0.1/v1/manifest"
                )
        self.assertEqual(raised.exception.code, "UNAVAILABLE")
        self.assertTrue(raised.exception.transient)

    async def test_ambiguous_act_retry_on_later_call_reuses_pending_request(self):
        transport = RecordingTransport(
            {"type": "tool_call", "tool": "search", "arguments": {"query": "alpha"}},
            timeout_once=True,
        )
        runtime = AgentServiceRuntime.from_settings(
            config=service_config(self.token).model_copy(update={"max_attempts": 1}),
            transport=transport,
            resolver=fake_resolver,
            settings=platform_settings(),
        )
        await runtime.reset(smoke_episode_context())
        with self.assertRaises(AgentServiceTimeout):
            await runtime.act(Observation(type="episode_start", content={"question": "alpha"}))

        await runtime.act(Observation(type="episode_start", content={"question": "alpha"}))
        first, second = transport.act_requests
        self.assertEqual(first["request_id"], second["request_id"])
        self.assertEqual(first["sequence"], second["sequence"])
        await runtime.close()

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

    async def test_gateway_reserves_cumulative_budget_before_provider_call(self):
        signer = CapabilitySigner(
            b"d" * 32,
            now=lambda: 1_000,
            budget_store=InMemoryCapabilityBudgetStore(),
        )
        token = signer.issue(valid_claims(max_tokens=95))

        class Provider:
            calls = 0

            async def complete(self, _request):
                self.calls += 1
                return ModelResponse(
                    text="ok",
                    usage=Usage(prompt=3, completion=3),
                )

        provider = Provider()
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=1),
            capability_signer=signer,
        )

        def request(request_id):
            return ModelRequest(
                provider_adapter_version="v1",
                model_role="agent",
                model="fixture",
                messages=({"role": "user", "content": "hello"},),
                run_id="run-1",
                case_id="case-1",
                attempt_id="attempt-1",
                agent_revision_id=DeterministicSmokeAgent.revision().id,
                capability_token=token,
                request_id=request_id,
                max_output_tokens=1,
            )

        await gateway.complete(request("model-call-1"))
        with self.assertRaises(CapabilityBudgetError):
            await gateway.complete(request("model-call-2"))
        self.assertEqual(provider.calls, 1)

    async def test_gateway_fails_closed_when_usage_or_pricing_is_missing(self):
        for response, pricing in (
            (ModelResponse(text="ok", usage=None), Pricing(1, 1)),
            (ModelResponse(text="ok", usage=Usage(prompt=1, completion=1)), Pricing()),
        ):
            with self.subTest(response=response, pricing=pricing):
                signer = CapabilitySigner(
                    b"e" * 32,
                    now=lambda: 1_000,
                    budget_store=InMemoryCapabilityBudgetStore(),
                )
                token = signer.issue(valid_claims())

                class Provider:
                    async def complete(self, _request):
                        return response

                gateway = ModelGateway(
                    provider=Provider(),
                    pricing=pricing,
                    capability_signer=signer,
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
                    capability_token=token,
                    request_id="model-call",
                    max_output_tokens=1,
                )
                with self.assertRaises(CapabilityBudgetError):
                    await gateway.complete(request)

    async def test_gateway_derives_reservation_and_rejects_tiny_budget_before_dispatch(self):
        signer = CapabilitySigner(
            b"f" * 32,
            now=lambda: 1_000,
            budget_store=InMemoryCapabilityBudgetStore(),
        )
        token = signer.issue(valid_claims(max_tokens=1, max_cost="0"))

        class Provider:
            calls = 0

            async def complete(self, _request):
                self.calls += 1
                return ModelResponse(text="unsafe", usage=Usage(prompt=100, completion=100))

        provider = Provider()
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=1),
            capability_signer=signer,
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
            capability_token=token,
            max_output_tokens=1,
        )

        with self.assertRaises(CapabilityBudgetError):
            await gateway.complete(request)
        self.assertEqual(provider.calls, 0)

    async def test_gateway_accounts_for_complete_payload_before_dispatch(self):
        signer = CapabilitySigner(
            b"g" * 32,
            now=lambda: 1_000,
            budget_store=InMemoryCapabilityBudgetStore(),
        )
        token = signer.issue(valid_claims(max_tokens=100, max_cost="1"))

        class Provider:
            calls = 0

            async def complete(self, _request):
                self.calls += 1
                return ModelResponse(text="unsafe", usage=Usage(prompt=1_000, completion=1))

        provider = Provider()
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=1),
            capability_signer=signer,
        )
        request = ModelRequest(
            provider_adapter_version="v1",
            model_role="agent",
            model="fixture",
            messages=({"role": "user", "content": "hello"},),
            effective_parameters={
                "tools": [
                    {
                        "type": "function",
                        "function": {
                            "name": "large",
                            "description": "x" * 2_000,
                            "parameters": {"type": "object"},
                        },
                    }
                ]
            },
            run_id="run-1",
            case_id="case-1",
            attempt_id="attempt-1",
            agent_revision_id=DeterministicSmokeAgent.revision().id,
            capability_token=token,
            max_output_tokens=1,
        )

        with self.assertRaises(CapabilityBudgetError):
            await gateway.complete(request)
        self.assertEqual(provider.calls, 0)

    async def test_completed_gateway_request_is_not_dispatched_twice(self):
        signer = CapabilitySigner(
            b"h" * 32,
            now=lambda: 1_000,
            budget_store=InMemoryCapabilityBudgetStore(),
        )
        token = signer.issue(valid_claims(max_tokens=100, max_cost="1"))

        class Provider:
            calls = 0

            async def complete(self, _request):
                self.calls += 1
                return ModelResponse(text="ok", usage=Usage(prompt=1, completion=1))

        provider = Provider()
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=1),
            capability_signer=signer,
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
            capability_token=token,
            request_id="replayed-call",
            max_output_tokens=1,
        )

        await gateway.complete(request)
        with self.assertRaises(CapabilityRequestCompleted):
            await gateway.complete(request)
        self.assertEqual(provider.calls, 1)

    async def test_concurrent_identical_request_is_dispatched_once(self):
        signer = CapabilitySigner(
            b"i" * 32,
            now=lambda: 1_000,
            budget_store=InMemoryCapabilityBudgetStore(),
        )
        token = signer.issue(valid_claims(max_tokens=100, max_cost="1"))
        started = asyncio.Event()
        release = asyncio.Event()

        class Provider:
            calls = 0

            async def complete(self, _request):
                self.calls += 1
                started.set()
                await release.wait()
                return ModelResponse(text="ok", usage=Usage(prompt=1, completion=1))

        provider = Provider()
        gateway = ModelGateway(
            provider=provider,
            pricing=Pricing(input_per_million=1, output_per_million=1),
            capability_signer=signer,
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
            capability_token=token,
            request_id="concurrent-call",
            max_output_tokens=1,
        )

        first = asyncio.create_task(gateway.complete(request))
        await started.wait()
        with self.assertRaises(CapabilityRequestInProgress):
            await gateway.complete(request)
        release.set()
        await first
        self.assertEqual(provider.calls, 1)


if __name__ == "__main__":
    unittest.main()
