from __future__ import annotations

import hashlib
import json
import socket
import uuid
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from secrl_platform.agents.protocol import AgentRevisionRef, EpisodeContext, UsageSnapshot
from secrl_platform.benchmarks.protocol import (
    AgentAction,
    Observation,
    ToolCallAction,
    parse_agent_action,
)


class AgentServiceError(RuntimeError):
    pass


class AgentServiceTimeout(AgentServiceError):
    pass


class InvalidAgentAction(AgentServiceError):
    pass


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_revision_id: str
    capability_token: SecretStr = Field(repr=False)
    allowlist: tuple[str, ...]
    max_attempts: int = Field(default=2, ge=1, le=3)


class AgentServiceTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]: ...


class HttpxAgentServiceTransport:
    def __init__(self, client: httpx.AsyncClient) -> None:
        self._client = client

    async def request(
        self,
        method: str,
        url: str,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        try:
            response = await self._client.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=10.0,
            )
        except httpx.TimeoutException as exc:
            raise AgentServiceTimeout("agent service request timed out") from exc
        except httpx.RequestError as exc:
            raise AgentServiceError("agent service request failed") from exc
        if response.status_code >= 400:
            raise AgentServiceError(
                f"agent service returned HTTP {response.status_code}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentServiceError("agent service returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise AgentServiceError("agent service response must be an object")
        return payload


class AgentServiceRuntime:
    def __init__(
        self,
        *,
        config: ServiceConfig,
        transport: AgentServiceTransport,
        resolver: Callable[[str, int], object] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._resolver = resolver or _resolve_host
        self._endpoint = _validated_endpoint(config, self._resolver)
        self._session_id: str | None = None
        self._episode: EpisodeContext | None = None
        self._sequence = 0
        self._usage = UsageSnapshot()
        self._manifest_checked = False
        self._last_exchange: tuple[dict[str, Any], dict[str, Any]] | None = None

    @property
    def name(self) -> str:
        return "Agent Service Protocol v1"

    async def reset(self, episode: EpisodeContext) -> None:
        if self._session_id is not None:
            raise AgentServiceError("agent service session is already active")
        if not self._manifest_checked:
            await self._check_manifest()
        request_id = str(uuid.uuid4())
        payload = {
            "request_id": request_id,
            "sequence": 0,
            "episode": episode.model_dump(mode="json"),
        }
        response = await self._transport.request(
            "POST",
            f"{self._endpoint}/v1/sessions",
            json_body=payload,
            headers=self._authorization_headers(),
        )
        session_id = response.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise AgentServiceError("agent service returned an invalid session ID")
        self._session_id = session_id
        self._episode = episode
        self._sequence = 0
        self._usage = UsageSnapshot()

    async def act(self, observation: Observation) -> AgentAction:
        if self._session_id is None or self._episode is None:
            raise AgentServiceError("agent service session is not active")
        request_id = str(uuid.uuid4())
        sequence = self._sequence + 1
        payload = {
            "request_id": request_id,
            "sequence": sequence,
            "observation": observation.model_dump(mode="json"),
        }
        response: dict[str, Any] | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                response = await self._transport.request(
                    "POST",
                    f"{self._endpoint}/v1/sessions/{self._session_id}:act",
                    json_body=payload,
                    headers=self._authorization_headers(),
                )
                break
            except AgentServiceTimeout:
                if attempt == self._config.max_attempts:
                    raise
        if response is None:
            raise AgentServiceError("agent service act request failed")
        try:
            action = parse_agent_action(response["action"])
        except (KeyError, ValueError) as exc:
            raise InvalidAgentAction("agent service returned an invalid action") from exc
        if isinstance(action, ToolCallAction):
            allowed_tools = {tool.name for tool in self._episode.tools}
            if action.tool not in allowed_tools:
                raise InvalidAgentAction("agent service returned an unapproved tool")
        usage = response.get("usage") or {}
        try:
            self._usage = UsageSnapshot(
                prompt_tokens=usage.get("prompt_tokens", 0),
                completion_tokens=usage.get("completion_tokens", 0),
                cached_tokens=usage.get("cached_tokens", 0),
                reasoning_tokens=usage.get("reasoning_tokens", 0),
            )
        except (AttributeError, ValueError) as exc:
            raise AgentServiceError("agent service returned invalid usage") from exc
        self._sequence = sequence
        self._last_exchange = (payload, response)
        return action

    def usage(self) -> UsageSnapshot:
        return self._usage

    async def close(self) -> None:
        session_id = self._session_id
        try:
            if session_id is not None:
                await self._transport.request(
                    "POST",
                    f"{self._endpoint}/v1/sessions/{session_id}:close",
                    json_body={"request_id": str(uuid.uuid4())},
                    headers=self._authorization_headers(),
                )
        finally:
            self._session_id = None
            self._episode = None
            self._sequence = 0
            self._last_exchange = None

    async def _check_manifest(self) -> None:
        manifest = await self._transport.request(
            "GET",
            f"{self._endpoint}/v1/manifest",
        )
        if manifest.get("protocol_version") != "1":
            raise AgentServiceError("agent service protocol version is not supported")
        if manifest.get("agent_revision_id") != self._config.agent_revision_id:
            raise AgentServiceError("agent service revision does not match registration")
        if manifest_sha256(manifest) != self._config.expected_manifest_sha256:
            raise ValueError("agent service manifest hash mismatch")
        self._manifest_checked = True

    def _authorization_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.capability_token.get_secret_value()}"
        }


def manifest_sha256(manifest: dict[str, Any]) -> str:
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validated_endpoint(
    config: ServiceConfig,
    resolver: Callable[[str, int], object],
) -> str:
    parsed = urlsplit(config.endpoint)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError("agent service endpoint must use HTTP or HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("agent service endpoint must not include user information")
    if parsed.fragment or parsed.query:
        raise ValueError("agent service endpoint must not include query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("agent service endpoint must not include a path")
    host = parsed.hostname
    if host is None or host not in config.allowlist:
        raise ValueError("agent service host is not allowlisted")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        resolver(host, port)
    except OSError as exc:
        raise ValueError("agent service host could not be resolved") from exc
    return config.endpoint.rstrip("/")


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({result[4][0] for result in results}))
