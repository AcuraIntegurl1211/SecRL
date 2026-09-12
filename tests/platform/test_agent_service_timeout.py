"""Per-request timeout knob for the Agent Service v1 transport.

A real Agent Service (e.g. the Flocks bridge) can spend tens of seconds per
:act turn because it synchronously awaits an upstream LLM.  The transport used
to hardcode timeout=10.0, which killed those runs with retryable
DEADLINE_EXCEEDED errors.  These tests pin the new configurable timeout at
every consumer: transport construction, ServiceConfig validation, Settings
wiring, and the runner runtime construction path.
"""

import asyncio
import json
import unittest
from decimal import Decimal
from pathlib import Path

import httpx
from pydantic import SecretStr, ValidationError

from secrl_platform.agents.service import (
    AgentServiceTimeout,
    HttpxAgentServiceTransport,
    ServiceConfig,
    manifest_sha256,
)
from secrl_platform.config import Settings
from secrl_platform.runner.process import _agent_service_factory_timeout


def slow_handler(delay: float, payload: dict):
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(delay)
        return httpx.Response(200, json=payload)

    return handler


class TransportTimeoutTest(unittest.IsolatedAsyncioTestCase):
    async def test_default_timeout_is_ten_seconds(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow_handler(0.05, {"ok": True}))
        ) as client:
            transport = HttpxAgentServiceTransport(client)
            self.assertEqual(transport.timeout, 10.0)
            response = await transport.request("GET", "http://127.0.0.1/v1/manifest")
            self.assertEqual(response, {"ok": True})

    async def test_custom_timeout_allows_slow_service(self):
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow_handler(0.4, {"ok": True}))
        ) as client:
            transport = HttpxAgentServiceTransport(client, timeout=5.0)
            response = await transport.request("GET", "http://127.0.0.1/v1/manifest")
            self.assertEqual(response, {"ok": True})

    async def test_short_timeout_raises_deadline_exceeded(self):
        # A real socket that accepts but never answers: httpx must enforce the
        # read timeout and the transport must surface it as DEADLINE_EXCEEDED.
        server = await asyncio.start_server(lambda r, w: None, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            async with httpx.AsyncClient() as client:
                transport = HttpxAgentServiceTransport(client, timeout=0.2)
                with self.assertRaises(AgentServiceTimeout):
                    await transport.request("GET", f"http://127.0.0.1:{port}/v1/manifest")
        finally:
            server.close()
            await server.wait_closed()

    async def test_timeout_is_per_request_not_per_client_lifetime(self):
        """Two sequential slow requests each get the full budget."""
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(slow_handler(0.2, {"ok": True}))
        ) as client:
            transport = HttpxAgentServiceTransport(client, timeout=0.5)
            for _ in range(2):
                response = await transport.request("GET", "http://127.0.0.1/v1/manifest")
                self.assertEqual(response, {"ok": True})


class ServiceConfigTimeoutTest(unittest.TestCase):
    def test_timeout_defaults_to_ten_and_validates_bounds(self):
        config = ServiceConfig(
            endpoint="http://agent.internal:8080",
            expected_manifest_sha256="0" * 64,
            agent_revision_id="svc-v1",
            capability_token=SecretStr("token"),
        )
        self.assertEqual(config.timeout_seconds, 10.0)
        with self.assertRaises(ValidationError):
            ServiceConfig(
                endpoint="http://agent.internal:8080",
                expected_manifest_sha256="0" * 64,
                agent_revision_id="svc-v1",
                capability_token=SecretStr("token"),
                timeout_seconds=0.5,
            )
        with self.assertRaises(ValidationError):
            ServiceConfig(
                endpoint="http://agent.internal:8080",
                expected_manifest_sha256="0" * 64,
                agent_revision_id="svc-v1",
                capability_token=SecretStr("token"),
                timeout_seconds=601,
            )


class SettingsTimeoutTest(unittest.TestCase):
    def test_settings_accepts_and_bounds_agent_service_timeout(self):
        settings = Settings(
            data_dir=Path("/tmp"),
            master_key="00" * 32,
            session_secret="s" * 32,
            agent_service_timeout_seconds=180.0,
        )
        self.assertEqual(settings.agent_service_timeout_seconds, 180.0)
        with self.assertRaises(ValidationError):
            Settings(
                data_dir=Path("/tmp"),
                master_key="00" * 32,
                session_secret="s" * 32,
                agent_service_timeout_seconds=0,
            )

    def test_runner_runtime_receives_settings_timeout(self):
        """The runner's per-run ServiceConfig must carry the configured
        timeout so a slow Agent Service is not killed at ten seconds."""
        manifest = {
            "protocol_version": "1",
            "agent_revision_id": "flocks-hunter-v1",
            "name": "Flocks",
            "runtime": "service",
            "version": "flocks-hunter-v1+x/y",
        }
        sha = manifest_sha256(manifest)
        settings = Settings(
            data_dir=Path("/tmp"),
            master_key="00" * 32,
            session_secret="s" * 32,
            agent_service_timeout_seconds=180.0,
        )
        resolved = _agent_service_factory_timeout(settings)
        self.assertEqual(resolved, 180.0)
        config = ServiceConfig(
            endpoint="http://192.168.165.1:8091",
            expected_manifest_sha256=sha,
            agent_revision_id="flocks-hunter-v1",
            capability_token=SecretStr("cap"),
            timeout_seconds=resolved,
        )
        self.assertEqual(config.timeout_seconds, 180.0)


if __name__ == "__main__":
    unittest.main()
