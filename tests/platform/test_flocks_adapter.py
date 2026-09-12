"""Offline tests for the Flocks Agent Service v1 adapter.

A fake Flocks ASGI app is injected through httpx's ASGITransport, so no real
Flocks or LLM traffic ever happens.  Several tests drive the adapter through
the platform's own AgentServiceRuntime to prove wire compatibility.
"""

import asyncio
import json
import unittest
from typing import Any

import httpx
from pydantic import SecretStr

from examples.flocks_adapter.app import (
    AGENT_REVISION_ID,
    FlocksSettings,
    build_manifest,
    build_manifest_sha256,
    create_app,
    observation_to_text,
    parse_action,
    _turn_prefix,
)
from secrl_platform.agents.protocol import EpisodeContext
from secrl_platform.agents.service import (
    AgentServiceError,
    AgentServiceProtocolError,
    AgentServiceRuntime,
    AgentServiceEndpointPolicy,
    HttpxAgentServiceTransport,
    ServiceConfig,
    inspect_agent_service,
    manifest_sha256,
)
from secrl_platform.benchmarks.protocol import (
    Observation,
    SubmitAction,
    ToolCallAction,
    ToolDefinition,
)


FLOCKS_TOKEN = "flocks-secret-token-0123456789abcdef"
PLATFORM_CAPABILITY = "platform-capability-token-must-not-leak"


def adapter_settings(**overrides: Any) -> FlocksSettings:
    base = dict(
        base_url="http://flocks.test",
        api_token=FLOCKS_TOKEN,
        eval_agent="secl-eval",
        provider_id="openai",
        model_id="gpt-test-x",
        poll_interval_seconds=0.01,
        poll_timeout_seconds=5.0,
    )
    base.update(overrides)
    return FlocksSettings(**base)


class FakeFlocks:
    """Scripted stand-in for the Flocks native session API.

    Defaults to the wire format observed on a live deployment: ``/message``
    returns a bare array of MessageWithParts entries whose ``info`` dict holds
    role and token stats, and ``/status`` reports ``isProcessing``.  The
    legacy shapes are still selectable via ``message_shape``/``status_shape``.
    """

    def __init__(self) -> None:
        self.replies: list[dict[str, Any]] = []
        self.prompts: list[dict[str, Any]] = []
        self.headers: list[dict[str, str]] = []
        self.sessions: dict[str, list[dict[str, Any]]] = {}
        self.status_mode = "idle"
        self.fail_create = False
        self.message_shape = "array"  # "array" | "page"
        self.status_shape = "processing"  # "processing" | "plain"
        self._counter = 0

    def messages_payload(self, session_messages: list[dict[str, Any]]) -> Any:
        if self.message_shape == "page":
            return {"messages": session_messages}
        return [
            {
                "info": {
                    "role": message["role"],
                    **({"tokens": message["tokens"]} if "tokens" in message else {}),
                },
                "parts": message["parts"],
            }
            for message in session_messages
        ]

    def script(self, *texts: str, tokens: dict[str, Any] | None = None) -> None:
        for text in texts:
            message: dict[str, Any] = {
                "role": "assistant",
                "parts": [{"type": "text", "text": text}],
            }
            if tokens is not None:
                message["tokens"] = tokens
            self.replies.append(message)

    @property
    def prompt_texts(self) -> list[str]:
        return [p["parts"][0]["text"] for p in self.prompts]

    def app(self) -> httpx.AsyncClient:
        fake = self

        async def handle(scope, receive, send):  # pragma: no cover - ASGI plumbing
            request = scope
            path: str = request["path"]
            method: str = request["method"]
            raw_body = b""
            while True:
                event = await receive()
                raw_body += event.get("body", b"")
                if not event.get("more_body", False):
                    break
            body = json.loads(raw_body) if raw_body else {}
            headers = {
                key.decode().lower(): value.decode()
                for key, value in request.get("headers", [])
            }
            fake.headers.append(headers)
            status, payload = 200, {}
            if method == "POST" and path == "/api/session":
                if fake.fail_create:
                    status, payload = 500, {"error": "boom"}
                else:
                    fake._counter += 1
                    session_id = f"fl-{fake._counter}"
                    fake.sessions[session_id] = []
                    payload = {"id": session_id}
            elif path.endswith("/prompt_async"):
                session_id = path.removeprefix("/api/session/").removesuffix("/prompt_async")
                if fake.fail_create or session_id not in fake.sessions:
                    status, payload = 404, {"error": "unknown session"}
                elif not fake.replies:
                    status, payload = 500, {"error": "no scripted reply"}
                else:
                    fake.prompts.append(body)
                    fake.sessions[session_id].append(
                        {"role": "user", "parts": body.get("parts", [])}
                    )
                    fake.sessions[session_id].append(fake.replies.pop(0))
                    payload = {"accepted": True}
            elif path.endswith("/status"):
                session_id = path.removeprefix("/api/session/").removesuffix("/status")
                if session_id not in fake.sessions:
                    status, payload = 404, {"error": "unknown session"}
                elif fake.status_shape == "plain":
                    # Legacy bare shape; "isProcessing" sentinel emits
                    # {"isProcessing": false}, any other mode emits
                    # {"status": <mode>}.
                    if fake.status_mode == "isProcessing":
                        payload = {"isProcessing": False}
                    else:
                        payload = {"status": fake.status_mode}
                else:
                    # Live shape: SessionRuntimeStatusResponse with isProcessing.
                    processing = fake.status_mode not in {"idle", "error"}
                    payload = {
                        "sessionID": session_id,
                        "lifecycleStatus": "active",
                        "status": {"type": fake.status_mode},
                        "isProcessing": processing,
                        "pendingPromptCount": 0,
                        "observedAt": 0,
                    }
            elif path.endswith("/message"):
                session_id = path.removeprefix("/api/session/").removesuffix("/message")
                payload = fake.messages_payload(fake.sessions.get(session_id, []))
            else:
                status, payload = 404, {"error": "no route"}
            raw = json.dumps(payload).encode()
            await send(
                {
                    "type": "http.response.start",
                    "status": status,
                    "headers": [[b"content-type", b"application/json"]],
                }
            )
            await send({"type": "http.response.body", "body": raw})

        return handle


def make_clients(
    settings: FlocksSettings, fake: FakeFlocks
) -> tuple[httpx.AsyncClient, httpx.AsyncClient]:
    flocks_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=fake.app()), base_url="http://flocks.test"
    )
    adapter = create_app(settings, flocks_client=flocks_client)
    adapter_client = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=adapter), base_url="http://adapter.test"
    )
    return adapter_client, flocks_client


def secrl_like_episode() -> EpisodeContext:
    return EpisodeContext(
        run_id="run-1",
        case_id="incident_5:0:abc",
        attempt_id="attempt-1",
        public_input={"question": "Which host exfiltrated data?", "context": "c2 beacon"},
        tools=(
            ToolDefinition(
                name="sql_query",
                description="Run SQL.",
                parameters={
                    "type": "object",
                    "required": ["query"],
                    "properties": {"query": {"type": "string"}},
                },
            ),
            ToolDefinition(
                name="submit",
                description="Submit.",
                parameters={
                    "type": "object",
                    "required": ["answer"],
                    "properties": {"answer": {"type": "string"}},
                },
            ),
        ),
        max_steps=16,
    )


def smoke_episode() -> EpisodeContext:
    from tests.platform.test_agent_protocol import smoke_episode_context

    return smoke_episode_context()


def session_payload(episode: EpisodeContext, request_id: str = "req-1") -> dict:
    return {
        "protocol_version": "1",
        "request_id": request_id,
        "sequence": 0,
        "episode": episode.model_dump(mode="json"),
    }


def act_payload(observation: Observation, request_id: str, sequence: int) -> dict:
    return {
        "protocol_version": "1",
        "request_id": request_id,
        "sequence": sequence,
        "observation": observation.model_dump(mode="json"),
    }


AUTH = {"Authorization": f"Bearer {PLATFORM_CAPABILITY}"}


class ManifestTest(unittest.TestCase):
    def test_manifest_is_static_and_hash_matches_platform_canonical_form(self):
        settings = adapter_settings()
        first = build_manifest(settings)
        second = build_manifest(settings)
        self.assertEqual(first, second)
        # The platform hashes with canonical JSON: sorted keys, no spaces.
        expected = manifest_sha256(first)
        self.assertEqual(expected, build_manifest_sha256(settings))
        raw = json.dumps(first, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        import hashlib

        self.assertEqual(hashlib.sha256(raw.encode()).hexdigest(), expected)

    def test_manifest_version_embeds_model_tag(self):
        manifest = build_manifest(adapter_settings())
        self.assertEqual(manifest["agent_revision_id"], AGENT_REVISION_ID)
        self.assertEqual(manifest["version"], "flocks-hunter-v1+openai/gpt-test-x")
        self.assertEqual(manifest["protocol_version"], "1")
        self.assertEqual(manifest["runtime"], "service")

    def test_no_usage_marker_changes_version_and_hash(self):
        with_usage = build_manifest_sha256(adapter_settings())
        without = build_manifest_sha256(adapter_settings(report_usage=False))
        self.assertNotEqual(with_usage, without)
        manifest = build_manifest(adapter_settings(report_usage=False))
        self.assertTrue(manifest["version"].endswith("+no-usage"))

    def test_model_change_produces_new_manifest_sha(self):
        original = build_manifest_sha256(adapter_settings())
        swapped = build_manifest_sha256(adapter_settings(model_id="gpt-other-y"))
        self.assertNotEqual(original, swapped)


class FlocksAdapterHttpTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.settings = adapter_settings()
        self.fake = FakeFlocks()
        self.client, self.flocks_client = make_clients(self.settings, self.fake)

    async def asyncTearDown(self):
        await _aclose(self.client, self.flocks_client)

    async def open_session(self, episode=None, request_id="req-1"):
        response = await self.client.post(
            "/v1/sessions", json=session_payload(episode or secrl_like_episode(), request_id), headers=AUTH
        )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["session_id"]

    async def test_session_create_echoes_correlation_and_opens_flocks_session(self):
        session_id = await self.open_session()
        self.assertTrue(session_id)
        self.assertEqual(self.fake._counter, 1)
        # Flocks gets its own bearer token, never the platform capability.
        self.assertEqual(self.fake.headers[0]["authorization"], f"Bearer {FLOCKS_TOKEN}")
        self.assertNotIn(PLATFORM_CAPABILITY, json.dumps(self.fake.headers))

    async def test_session_create_requires_bearer_capability(self):
        missing = await self.client.post("/v1/sessions", json=session_payload(secrl_like_episode()))
        self.assertEqual(missing.status_code, 401)
        bad_scheme = await self.client.post(
            "/v1/sessions",
            json=session_payload(secrl_like_episode()),
            headers={"Authorization": f"Basic {PLATFORM_CAPABILITY}"},
        )
        self.assertEqual(bad_scheme.status_code, 401)
        empty_token = await self.client.post(
            "/v1/sessions",
            json=session_payload(secrl_like_episode()),
            headers={"Authorization": "Bearer   "},
        )
        self.assertEqual(empty_token.status_code, 401)

    async def test_session_create_rejects_nonzero_sequence(self):
        payload = session_payload(secrl_like_episode())
        payload["sequence"] = 3
        response = await self.client.post("/v1/sessions", json=payload, headers=AUTH)
        self.assertEqual(response.status_code, 409)

    async def test_sql_line_becomes_tool_call_on_episode_query_tool(self):
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT ip FROM events LIMIT 5")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(
                Observation(type="episode_start", content={"question": "q"}),
                "req-a",
                1,
            ),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["request_id"], "req-a")
        self.assertEqual(body["sequence"], 1)
        self.assertEqual(body["action"]["type"], "tool_call")
        self.assertEqual(body["action"]["tool"], "sql_query")
        self.assertEqual(body["action"]["arguments"], {"query": "SELECT ip FROM events LIMIT 5"})
        prompt = self.fake.prompts[0]
        self.assertEqual(prompt["agent"], "secl-eval")
        self.assertEqual(prompt["model"], {"providerID": "openai", "modelID": "gpt-test-x"})
        self.assertEqual(prompt["parts"], [{"type": "text", "text": "[Turn 1/16, 15 remaining]\nInvestigate this incident.\nQuestion: q"}])

    async def test_sql_maps_to_preferred_tool_in_generic_episode(self):
        session_id = await self.open_session(smoke_episode())
        self.fake.script("SQL: SELECT 1")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-a", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"]["tool"], "search")

    async def test_submit_line_becomes_submit_action(self):
        session_id = await self.open_session()
        self.fake.script("SUBMIT: 10.0.0.8")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-b", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        action = response.json()["action"]
        self.assertEqual(action["type"], "submit")
        self.assertEqual(action["answer"], "10.0.0.8")

    async def test_multiline_or_prose_reply_triggers_correction_then_success(self):
        session_id = await self.open_session()
        self.fake.script(
            "I think we should look at the proxy logs first.",
            "SQL: SELECT * FROM proxy_logs",
        )
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-c", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"]["type"], "tool_call")
        self.assertEqual(len(self.fake.prompts), 2)
        self.assertIn("not a valid action", self.fake.prompt_texts[1])

    async def test_second_grammar_violation_fails_turn_as_invalid_action(self):
        session_id = await self.open_session()
        self.fake.script("still thinking...", "also not an action")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-d", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self.fake.prompts), 2)  # exactly one correction, no more

    async def test_sequence_mismatch_is_rejected(self):
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1")
        wrong = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-e", 5),
            headers=AUTH,
        )
        self.assertEqual(wrong.status_code, 409)
        self.assertEqual(self.fake.prompts, [])  # rejected before touching Flocks

    async def test_identical_replay_is_idempotent(self):
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1")
        payload = act_payload(Observation(type="tool_result", content={}), "req-f", 1)
        first = await self.client.post(f"/v1/sessions/{session_id}:act", json=payload, headers=AUTH)
        second = await self.client.post(f"/v1/sessions/{session_id}:act", json=payload, headers=AUTH)
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json(), second.json())
        self.assertEqual(len(self.fake.prompts), 1)

    async def test_unknown_session_is_404(self):
        self.fake.script("SQL: SELECT 1")
        response = await self.client.post(
            "/v1/sessions/nope:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-g", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 404)

    async def test_usage_passthrough_when_flocks_reports_tokens(self):
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1", tokens={"input": 120, "output": 8, "reasoning": 4})
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-h", 1),
            headers=AUTH,
        )
        usage = response.json()["usage"]
        self.assertEqual(usage["prompt_tokens"], 120)
        self.assertEqual(usage["completion_tokens"], 8)
        self.assertEqual(usage["reasoning_tokens"], 4)

    async def test_usage_zero_without_estimation_when_tokens_absent(self):
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-i", 1),
            headers=AUTH,
        )
        usage = response.json()["usage"]
        self.assertEqual(usage["prompt_tokens"], 0)
        self.assertEqual(usage["completion_tokens"], 0)
        self.assertEqual(usage["estimated_cost"], "0")

    async def test_usage_suppressed_when_report_usage_disabled(self):
        await _aclose(self.client, self.flocks_client)
        self.client, self.flocks_client = make_clients(
            adapter_settings(report_usage=False), self.fake
        )
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1", tokens={"input": 999, "output": 9})
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-j", 1),
            headers=AUTH,
        )
        usage = response.json()["usage"]
        self.assertEqual(usage["prompt_tokens"], 0)
        self.assertEqual(usage["completion_tokens"], 0)

    async def test_close_is_idempotent_and_drops_session(self):
        session_id = await self.open_session()
        closed = await self.client.post(
            f"/v1/sessions/{session_id}:close",
            json={"protocol_version": "1", "request_id": "req-k"},
            headers=AUTH,
        )
        self.assertEqual(closed.status_code, 200)
        again = await self.client.post(
            f"/v1/sessions/{session_id}:close",
            json={"protocol_version": "1", "request_id": "req-l"},
            headers=AUTH,
        )
        self.assertEqual(again.status_code, 200)
        self.fake.script("SQL: SELECT 1")
        after = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-m", 1),
            headers=AUTH,
        )
        self.assertEqual(after.status_code, 404)

    async def test_flocks_upstream_failure_maps_to_bad_gateway(self):
        self.fake.fail_create = True
        response = await self.client.post(
            "/v1/sessions", json=session_payload(secrl_like_episode()), headers=AUTH
        )
        self.assertEqual(response.status_code, 502)

    async def test_status_isProcessing_shape_is_accepted_as_idle(self):
        """Legacy bare {isProcessing: false} status shape still works."""
        self.fake.status_shape = "plain"
        self.fake.status_mode = "isProcessing"  # sentinel: not "idle", not "error"
        await _aclose(self.client, self.flocks_client)
        self.client, self.flocks_client = make_clients(adapter_settings(), self.fake)
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-ip", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["action"]["type"], "tool_call")
        self.assertEqual(body["request_id"], "req-ip")

    async def test_live_message_array_shape_with_info_tokens(self):
        """Live Flocks returns /message as a bare array with info.tokens; the
        adapter must read role/parts/tokens through the info wrapper."""
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1")
        # The fake already emits the live array shape by default; assert the
        # path end-to-end including usage extracted from info.tokens.
        self.fake.replies.clear()
        self.fake.replies.append({
            "role": "assistant",
            "tokens": {"input": 77, "output": 5, "reasoning": 1},
            "parts": [{"type": "text", "text": "SQL: SELECT 2"}],
        })
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-live", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["action"]["arguments"], {"query": "SELECT 2"})
        self.assertEqual(body["usage"]["prompt_tokens"], 77)
        self.assertEqual(body["usage"]["completion_tokens"], 5)

    async def test_message_page_shape_still_accepted(self):
        """Older Flocks versions return {messages: [...]}; must keep working."""
        session_id = await self.open_session()
        self.fake.message_shape = "page"
        self.fake.script("SUBMIT: page-shaped")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-page", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"]["answer"], "page-shaped")

    async def test_empty_text_parts_are_skipped_until_real_text(self):
        """Live rex splits a reply into several text parts (first can be '') --
        the adapter must join them, not treat the message as textless."""
        session_id = await self.open_session()
        self.fake.replies.append({
            "role": "assistant",
            "parts": [
                {"type": "text", "text": ""},
                {"type": "text", "text": "SUBMIT: joined"},
            ],
        })
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-split", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["action"]["answer"], "joined")

    async def test_flocks_never_idle_maps_to_request_timeout(self):
        self.fake.status_mode = "busy"
        await _aclose(self.client, self.flocks_client)
        self.client, self.flocks_client = make_clients(
            adapter_settings(poll_timeout_seconds=0.0), self.fake
        )
        session_id = await self.open_session()
        self.fake.script("SQL: SELECT 1")
        response = await self.client.post(
            f"/v1/sessions/{session_id}:act",
            json=act_payload(Observation(type="tool_result", content={}), "req-n", 1),
            headers=AUTH,
        )
        self.assertEqual(response.status_code, 408)


async def _aclose(*clients: httpx.AsyncClient) -> None:
    for client in clients:
        await client.aclose()


class ObservationRenderingTest(unittest.TestCase):
    def test_episode_start_renders_context_and_question(self):
        text = observation_to_text(
            Observation(
                type="episode_start",
                content={"question": "Who?", "context": "beacon"},
            )
        )
        self.assertIn("Question: Who?", text)
        self.assertIn("Context: beacon", text)

    def test_tool_result_renders_json_payload(self):
        text = observation_to_text(
            Observation(type="tool_result", content={"result": [["a"]]}, truncated=True)
        )
        self.assertIn('"result"', text)
        self.assertIn("[truncated]", text)


class TurnBudgetPrefixTest(unittest.TestCase):
    """Every prompt opens with an adapter-owned turn counter; the last two
    turns force SUBMIT (the first live run died at 15/15 tool_calls, never
    submitting)."""

    def test_prefix_counts_up_and_remaining_down(self):
        self.assertEqual(
            _turn_prefix(1, 15), "[Turn 1/15, 14 remaining]\n"
        )
        self.assertEqual(
            _turn_prefix(15, 15), "[Turn 15/15, 0 remaining] 0 turn(s) left. You MUST answer with SUBMIT now; no more SQL.\n"
        )

    def test_urgency_kicks_in_for_last_two_turns(self):
        self.assertIn("MUST answer with SUBMIT", _turn_prefix(14, 15))
        self.assertIn("MUST answer with SUBMIT", _turn_prefix(15, 15))
        self.assertIn("MUST answer with SUBMIT", _turn_prefix(13, 15))  # 2 left
        self.assertNotIn("MUST answer", _turn_prefix(12, 15))  # 3 left

    def test_halfway_nudge(self):
        self.assertIn("converging", _turn_prefix(8, 15))
        self.assertNotIn("converging", _turn_prefix(7, 15))

    def test_zero_remaining_is_clamped(self):
        banner = _turn_prefix(99, 15)
        self.assertIn("0 remaining", banner)

    async def test_act_prompts_carry_turn_prefix(self):
        import asyncio

        from tests.platform.test_api import ApiTest  # noqa: F401  (harness import side effects)

        settings = adapter_settings()
        fake = FakeFlocks()
        client, flocks_client = make_clients(settings, fake)
        try:
            response = await client.post(
                "/v1/sessions", json=session_payload(secrl_like_episode()), headers=AUTH
            )
            self.assertEqual(response.status_code, 200, response.text)
            session_id = response.json()["session_id"]
            fake.script("SQL: SELECT 1", "SUBMIT: done")
            first = await client.post(
                f"/v1/sessions/{session_id}:act",
                json=act_payload(Observation(type="episode_start", content={"question": "q"}), "t1", 1),
                headers=AUTH,
            )
            self.assertEqual(first.status_code, 200, first.text)
            second = await client.post(
                f"/v1/sessions/{session_id}:act",
                json=act_payload(Observation(type="tool_result", content={}), "t2", 2),
                headers=AUTH,
            )
            self.assertEqual(second.status_code, 200, second.text)
            self.assertTrue(fake.prompt_texts[0].startswith("[Turn 1/16, 15 remaining]"))
            self.assertTrue(fake.prompt_texts[1].startswith("[Turn 2/16, 14 remaining]"))
        finally:
            await _aclose(client, flocks_client)


class ParseActionTest(unittest.TestCase):
    def test_grammar_rejects_fenced_and_multi_line_replies(self):
        tools = secrl_like_episode().tools
        for bad in (
            "```sql\nSELECT 1\n```",
            "SQL: SELECT 1\nSQL: SELECT 2",
            "Sure! SQL: SELECT 1",
            "SUBMIT:",
            "",
        ):
            with self.assertRaises(ValueError, msg=bad):
                parse_action(bad, tools)

    def test_sql_accepts_single_line_with_inline_semicolon(self):
        action = parse_action("SQL: SELECT 1; ", secrl_like_episode().tools)
        self.assertIsInstance(action, ToolCallAction)
        self.assertEqual(action.arguments, {"query": "SELECT 1;"})


class PlatformRuntimeEndToEndTest(unittest.IsolatedAsyncioTestCase):
    """Drive the adapter through the platform's own AgentServiceRuntime to
    prove wire compatibility with the authoritative client."""

    async def asyncSetUp(self):
        self.settings = adapter_settings()
        self.fake = FakeFlocks()
        self.adapter_client, self.flocks_client = make_clients(self.settings, self.fake)
        transport = HttpxAgentServiceTransport(self.adapter_client)
        self.policy = AgentServiceEndpointPolicy(allowed_hosts=("adapter.test",))
        self.resolver = lambda _host, _port: ("127.0.0.1",)
        self.transport = transport

    async def asyncTearDown(self):
        await _aclose(self.adapter_client, self.flocks_client)

    def runtime(self, *, expected_sha: str | None = None) -> AgentServiceRuntime:
        config = ServiceConfig(
            endpoint="http://adapter.test:8091",
            expected_manifest_sha256=expected_sha or build_manifest_sha256(self.settings),
            agent_revision_id=AGENT_REVISION_ID,
            capability_token=SecretStr(PLATFORM_CAPABILITY),
        )
        return AgentServiceRuntime(
            config=config,
            transport=self.transport,
            _policy=self.policy,
            resolver=self.resolver,
        )

    async def test_inspect_agent_service_returns_registerable_manifest(self):
        manifest = await inspect_agent_service(
            endpoint="http://adapter.test:8091",
            transport=self.transport,
            policy=self.policy,
            resolver=self.resolver,
        )
        self.assertEqual(manifest.agent_revision_id, AGENT_REVISION_ID)
        self.assertEqual(
            manifest_sha256(manifest.model_dump(mode="json")),
            build_manifest_sha256(self.settings),
        )

    async def test_full_episode_flow_through_platform_runtime(self):
        runtime = self.runtime()
        episode = secrl_like_episode()
        await runtime.reset(episode)
        self.fake.script(
            "SQL: SELECT host FROM events",
            "SUBMIT: host-7",
        )
        action = await runtime.act(
            Observation(type="episode_start", content={"question": "q"})
        )
        self.assertIsInstance(action, ToolCallAction)
        self.assertEqual(action.tool, "sql_query")
        action = await runtime.act(
            Observation(type="tool_result", content={"result": [["host-7"]]})
        )
        self.assertIsInstance(action, SubmitAction)
        self.assertEqual(action.answer, "host-7")
        await runtime.close()

    async def test_manifest_hash_mismatch_is_rejected_before_session(self):
        runtime = self.runtime(expected_sha="00" * 32)
        with self.assertRaises(AgentServiceProtocolError):
            await runtime.reset(secrl_like_episode())
        self.assertEqual(self.fake._counter, 0)

    async def test_grammar_violation_surfaces_as_invalid_action_error(self):
        runtime = self.runtime()
        await runtime.reset(secrl_like_episode())
        self.fake.script("prose again", "still prose")
        with self.assertRaises(AgentServiceError) as ctx:
            await runtime.act(Observation(type="episode_start", content={"question": "q"}))
        self.assertEqual(ctx.exception.code, "INVALID_ACTION")


if __name__ == "__main__":
    unittest.main()
