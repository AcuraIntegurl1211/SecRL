"""Flocks -> SecRL Lite Agent Service Protocol v1 adapter.

Exposes the four Agent Service v1 endpoints the platform consumes
(GET /v1/manifest, POST /v1/sessions, .../{id}:act, .../{id}:close) and
translates each turn into Flocks' native HTTP API:

    POST /api/session
    POST /api/session/{id}/prompt_async
    GET  /api/session/{id}/status      (polled until "idle")
    GET  /api/session/{id}/message     (last assistant message)

Design decisions baked in (see README):
  * one dedicated, tool-less Flocks agent per evaluation session;
  * strict two-form action grammar (``SQL: <one line>`` / ``SUBMIT: <one line>``)
    with exactly one in-session correction retry;
  * the platform capability token is verified for presence only and is never
    forwarded to Flocks -- Flocks gets its own bearer token from the env;
  * request_id/sequence are echoed verbatim so the platform can pin
    correlation, and the manifest is static so its SHA-256 can be registered.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

import httpx
from fastapi import FastAPI, Header, HTTPException

from secrl_platform.agents.protocol import EpisodeContext, UsageSnapshot
from secrl_platform.agents.service import (
    ActRequest,
    ActResponse,
    CloseRequest,
    CreateSessionRequest,
    CreateSessionResponse,
    ServiceManifest,
    manifest_sha256,
)
from secrl_platform.benchmarks.protocol import (
    Observation,
    SubmitAction,
    ToolCallAction,
)


AGENT_REVISION_ID = "flocks-hunter-v1"
AGENT_NAME = "Flocks Hunter (Agent Service v1 bridge)"

# Strict grammar: the whole assistant message must be exactly one line in one
# of the two accepted forms.  Anything else (prose, fences, multiple actions)
# is rejected and triggers the single correction retry.
_SQL_LINE = re.compile(r"SQL:[ \t]*(?P<body>[^\r\n]+)\Z")
_SUBMIT_LINE = re.compile(r"SUBMIT:[ \t]*(?P<body>[^\r\n]+)\Z")

# Preferred tool names in order; the first one present in the episode's frozen
# tool list wins, so the action survives the platform's tool allowlist check.
_SQL_TOOL_PREFERENCE = ("sql_query", "execute", "search", "query")


class FlocksSettingsError(RuntimeError):
    pass


@dataclass(frozen=True)
class FlocksSettings:
    base_url: str
    api_token: str = field(repr=False)
    eval_agent: str = "secl-eval"
    provider_id: str = "anthropic"
    model_id: str = "claude-sonnet-4-5"
    port: int = 8091
    report_usage: bool = True
    poll_interval_seconds: float = 1.0
    poll_timeout_seconds: float = 300.0

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "FlocksSettings":
        values = os.environ if env is None else env

        def name(key: str, default: str = "") -> str:
            return (values.get(key) or default).strip()

        base_url = name("FLOCKS_BASE_URL").rstrip("/")
        api_token = name("FLOCKS_API_TOKEN")
        if not base_url:
            raise FlocksSettingsError("FLOCKS_BASE_URL is required")
        if not api_token:
            raise FlocksSettingsError("FLOCKS_API_TOKEN is required")
        if not base_url.startswith("http://"):
            raise FlocksSettingsError(
                "FLOCKS_BASE_URL must be plain http:// on a private network"
            )
        return cls(
            base_url=base_url,
            api_token=api_token,
            eval_agent=name("FLOCKS_EVAL_AGENT", "secl-eval"),
            provider_id=name("MODEL_PROVIDER_ID", "anthropic"),
            model_id=name("MODEL_ID", "claude-sonnet-4-5"),
            port=int(name("ADAPTER_PORT", "8091") or "8091"),
            report_usage=name("FLOCKS_USAGE_REPORTED", "true").lower()
            not in {"0", "false", "no"},
            poll_interval_seconds=float(
                name("FLOCKS_POLL_INTERVAL_SECONDS", "1.0") or "1.0"
            ),
            poll_timeout_seconds=float(
                name("FLOCKS_POLL_TIMEOUT_SECONDS", "300") or "300"
            ),
        )

    def model_tag(self) -> str:
        return f"{self.provider_id}/{self.model_id}"

    def manifest_version(self) -> str:
        version = f"{AGENT_REVISION_ID}+{self.model_tag()}"
        if not self.report_usage:
            version += "+no-usage"
        return version


def build_manifest(settings: FlocksSettings) -> dict[str, Any]:
    return ServiceManifest(
        protocol_version="1",
        agent_revision_id=AGENT_REVISION_ID,
        name=AGENT_NAME,
        runtime="service",
        version=settings.manifest_version(),
    ).model_dump(mode="json")


def build_manifest_sha256(settings: FlocksSettings) -> str:
    return manifest_sha256(build_manifest(settings))


class FlocksUpstreamError(RuntimeError):
    """Any non-successful or malformed Flocks response."""


class FlocksTimeout(RuntimeError):
    """Flocks session did not reach idle within the poll budget."""


class FlocksClient:
    """Thin async client for the Flocks native session API."""

    def __init__(self, settings: FlocksSettings, client: httpx.AsyncClient) -> None:
        self._settings = settings
        self._client = client

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._settings.api_token}"}

    async def create_session(self, title: str) -> str:
        payload = await self._request(
            "POST", "/api/session", json_body={"title": title}
        )
        session_id = payload.get("id")
        if not isinstance(session_id, str) or not session_id:
            raise FlocksUpstreamError("Flocks session response has no id")
        return session_id

    async def prompt(self, session_id: str, text: str) -> None:
        body = {
            "parts": [{"type": "text", "text": text}],
            "agent": self._settings.eval_agent,
            "model": {
                "providerID": self._settings.provider_id,
                "modelID": self._settings.model_id,
            },
        }
        await self._request(
            "POST", f"/api/session/{session_id}/prompt_async", json_body=body
        )

    async def await_idle(self, session_id: str) -> None:
        deadline = self._settings.poll_timeout_seconds
        elapsed = 0.0
        while True:
            payload = await self._request(
                "GET", f"/api/session/{session_id}/status"
            )
            status = payload.get("status")
            if status == "idle":
                return
            if status == "error":
                raise FlocksUpstreamError("Flocks session reported error")
            if elapsed >= deadline:
                raise FlocksTimeout(
                    f"Flocks session did not become idle within {deadline}s"
                )
            step = self._settings.poll_interval_seconds
            await asyncio.sleep(step)
            elapsed += step

    async def latest_turn(self, session_id: str) -> tuple[str, UsageSnapshot]:
        payload = await self._request(
            "GET", f"/api/session/{session_id}/message"
        )
        messages = payload.get("messages") or payload.get("info") or []
        if not isinstance(messages, list):
            raise FlocksUpstreamError("Flocks message response is malformed")
        for message in reversed(messages):
            if not isinstance(message, dict) or message.get("role") != "assistant":
                continue
            text = _assistant_text(message)
            if text is None:
                continue
            return text, _usage_from(message, report_usage=self._settings.report_usage)
        raise FlocksUpstreamError("Flocks session has no assistant message yet")

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                path,
                json=json_body,
                headers=self._headers(),
                timeout=30.0,
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise FlocksUpstreamError(f"Flocks request failed: {type(exc).__name__}") from exc
        if 300 <= response.status_code:
            raise FlocksUpstreamError(
                f"Flocks returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise FlocksUpstreamError("Flocks returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise FlocksUpstreamError("Flocks response must be an object")
        return payload


def _assistant_text(message: dict[str, Any]) -> str | None:
    parts = message.get("parts")
    if isinstance(parts, list):
        chunks = [
            str(part["text"])
            for part in parts
            if isinstance(part, dict)
            and part.get("type") == "text"
            and isinstance(part.get("text"), str)
        ]
        if chunks:
            return "\n".join(chunks)
    content = message.get("content")
    if isinstance(content, str) and content:
        return content
    return None


def _usage_from(message: dict[str, Any], *, report_usage: bool) -> UsageSnapshot:
    tokens = message.get("tokens")
    if not isinstance(tokens, dict) or not report_usage:
        return UsageSnapshot()
    cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}

    def number(*keys: str) -> int:
        for key in keys:
            value = tokens.get(key)
            if isinstance(value, int) and value >= 0:
                return value
        return 0

    return UsageSnapshot(
        prompt_tokens=number("input", "prompt", "prompt_tokens"),
        completion_tokens=number("output", "completion", "completion_tokens"),
        cached_tokens=number("cached", "cache_read") or int(cache.get("read") or 0),
        reasoning_tokens=number("reasoning"),
    )


def observation_to_text(observation: Observation) -> str:
    """Render one platform observation as the Flocks turn text."""
    if observation.type == "episode_start":
        question = observation.content.get("question") or ""
        context = observation.content.get("context") or ""
        if context:
            return f"Investigate this incident.\nContext: {context}\nQuestion: {question}"
        return f"Investigate this incident.\nQuestion: {question}"
    if observation.type == "submission":
        return f"Submitted: {observation.content.get('answer', '')}"
    body = json.dumps(observation.content, ensure_ascii=False, default=str)
    if observation.truncated:
        body += " [truncated]"
    return f"{observation.type}: {body}"


def parse_action(
    text: str, tools: tuple[Any, ...]
) -> ToolCallAction | SubmitAction:
    """Strict two-form grammar -> platform AgentAction."""
    stripped = text.strip()
    sql = _SQL_LINE.fullmatch(stripped)
    if sql is not None:
        return ToolCallAction(
            type="tool_call",
            tool=_sql_tool_name(tools),
            arguments=_sql_arguments(tools, sql.group("body").strip()),
        )
    submit = _SUBMIT_LINE.fullmatch(stripped)
    if submit is not None:
        return SubmitAction(type="submit", answer=submit.group("body").strip())
    raise ValueError("assistant reply does not match the SQL:/SUBMIT: grammar")


def _sql_tool_name(tools: tuple[Any, ...]) -> str:
    names = [tool.name for tool in tools]
    for preferred in _SQL_TOOL_PREFERENCE:
        if preferred in names:
            return preferred
    candidates = [name for name in names if name != "submit"]
    if candidates:
        return candidates[0]
    raise ValueError("episode exposes no query tool to run SQL against")


def _sql_arguments(tools: tuple[Any, ...], query: str) -> dict[str, Any]:
    for tool in tools:
        if tool.name != _sql_tool_name(tools):
            continue
        properties = tool.parameters.get("properties", {})
        required = tool.parameters.get("required", [])
        for key in ("query", "sql", "statement", "code"):
            if key in properties:
                return {key: query}
        if required:
            first = required[0]
            if isinstance(first, str):
                return {first: query}
    return {"query": query}


_CORRECTION_PROMPT = (
    "Your reply was not a valid action. Reply with exactly one line and nothing "
    "else, using one of these two forms:\n"
    "SQL: <one read-only SQL statement>\n"
    "SUBMIT: <final answer>\n"
    "Do not call tools, do not add explanation or code fences."
)


@dataclass
class _Session:
    flocks_session_id: str
    episode: EpisodeContext
    sequence: int = 0
    responses: dict[tuple[str, int], dict[str, Any]] = field(default_factory=dict)


def create_app(
    settings: FlocksSettings | None = None,
    *,
    flocks_client: httpx.AsyncClient | None = None,
) -> FastAPI:
    settings = settings or FlocksSettings.from_env()
    owns_client = flocks_client is None
    client = flocks_client or httpx.AsyncClient(base_url=settings.base_url)
    flocks = FlocksClient(settings, client)

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        if owns_client:
            await client.aclose()

    app = FastAPI(
        title="SecRL Lite Flocks Agent Service Adapter", version="1.0.0", lifespan=lifespan
    )
    app.state.settings = settings
    sessions: dict[str, _Session] = {}
    created_sessions: dict[str, CreateSessionResponse] = {}
    manifest = build_manifest(settings)
    manifest_sha256_value = manifest_sha256(manifest)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/manifest")
    async def get_manifest() -> dict[str, Any]:
        return manifest

    @app.get("/v1/manifest/sha256")
    async def get_manifest_sha256() -> dict[str, str]:
        return {"sha256": manifest_sha256_value}

    @app.post("/v1/sessions")
    async def create_session(
        request: CreateSessionRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        _require_capability(authorization)
        if request.sequence != 0:
            raise HTTPException(status_code=409, detail="invalid sequence")
        cached = created_sessions.get(request.request_id)
        if cached is not None:
            return cached.model_dump(mode="json")
        episode = request.episode
        flocks_session_id = await _flocks_call(
            flocks.create_session, _session_title(episode)
        )
        session_id = str(uuid.uuid4())
        sessions[session_id] = _Session(
            flocks_session_id=flocks_session_id,
            episode=episode,
        )
        response = CreateSessionResponse(
            request_id=request.request_id,
            sequence=request.sequence,
            session_id=session_id,
        )
        created_sessions[request.request_id] = response
        return response.model_dump(mode="json")

    @app.post("/v1/sessions/{session_id}:act")
    async def act(
        session_id: str,
        request: ActRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        session = sessions.get(session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="unknown session")
        _require_capability(authorization)
        key = (request.request_id, request.sequence)
        cached = session.responses.get(key)
        if cached is not None:
            return cached
        if request.sequence != session.sequence + 1:
            raise HTTPException(status_code=409, detail="invalid sequence")

        await _flocks_call(flocks.prompt, session.flocks_session_id, observation_to_text(request.observation))
        await _flocks_call(flocks.await_idle, session.flocks_session_id)
        text, usage = await _flocks_call(flocks.latest_turn, session.flocks_session_id)
        try:
            action = parse_action(text, session.episode.tools)
        except ValueError:
            # One in-session correction, then fail the turn as a protocol-level
            # invalid action so the platform records AGENT_RUNTIME_ERROR.
            await _flocks_call(flocks.prompt, session.flocks_session_id, _CORRECTION_PROMPT)
            await _flocks_call(flocks.await_idle, session.flocks_session_id)
            text, usage = await _flocks_call(flocks.latest_turn, session.flocks_session_id)
            try:
                action = parse_action(text, session.episode.tools)
            except ValueError:
                raise HTTPException(
                    status_code=422, detail="assistant reply violates action grammar"
                ) from None

        response = ActResponse(
            request_id=request.request_id,
            sequence=request.sequence,
            action=action,
            usage=usage,
        )
        payload = response.model_dump(mode="json")
        session.responses[key] = payload
        session.sequence = request.sequence
        return payload

    @app.post("/v1/sessions/{session_id}:close")
    async def close(
        session_id: str,
        _request: CloseRequest,
        authorization: str = Header(default=""),
    ) -> dict[str, Any]:
        session = sessions.get(session_id)
        if session is None:
            return {"closed": True}
        _require_capability(authorization)
        sessions.pop(session_id, None)
        return {"closed": True}

    return app


async def _flocks_call(function, *args: Any) -> Any:
    try:
        return await function(*args)
    except FlocksTimeout as exc:
        raise HTTPException(status_code=408, detail=str(exc)) from exc
    except FlocksUpstreamError as exc:
        raise HTTPException(status_code=502, detail=str(exc)) from exc


def _require_capability(authorization: str) -> None:
    """Presence-only check: the platform token is never forwarded or verified."""
    scheme, separator, token = authorization.partition(" ")
    if separator != " " or scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="capability required")


def _session_title(episode: EpisodeContext) -> str:
    return f"secl-{episode.case_id}-{episode.attempt_id}"


def main() -> int:
    import uvicorn

    settings = FlocksSettings.from_env()
    print(f"flocks adapter manifest sha256 = {build_manifest_sha256(settings)}")
    uvicorn.run(create_app(settings), host="127.0.0.1", port=settings.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
