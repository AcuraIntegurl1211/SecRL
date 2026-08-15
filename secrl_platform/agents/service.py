from __future__ import annotations

import hashlib
import ipaddress
import json
import socket
import uuid
from collections.abc import Callable
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, SecretStr

from secrl_platform.agents.protocol import EpisodeContext, UsageSnapshot
from secrl_platform.benchmarks.protocol import (
    AgentAction,
    Observation,
    ToolCallAction,
)


class AgentServiceError(RuntimeError):
    code = "AGENT_SERVICE_ERROR"

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class AgentServiceTimeout(AgentServiceError):
    code = "AGENT_SERVICE_TIMEOUT"


class AgentServiceProtocolError(AgentServiceError):
    code = "AGENT_SERVICE_PROTOCOL_ERROR"


class InvalidAgentAction(AgentServiceError):
    code = "INVALID_AGENT_ACTION"


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_revision_id: str
    capability_token: SecretStr = Field(repr=False)
    max_attempts: int = Field(default=2, ge=1, le=3)


class ServiceProtocolModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ServiceManifest(ServiceProtocolModel):
    protocol_version: str
    agent_revision_id: str
    name: str
    runtime: str
    version: str


class CreateSessionRequest(ServiceProtocolModel):
    protocol_version: str = "1"
    request_id: str
    sequence: int
    episode: EpisodeContext


class CreateSessionResponse(ServiceProtocolModel):
    request_id: str
    sequence: int
    session_id: str


class ActRequest(ServiceProtocolModel):
    protocol_version: str = "1"
    request_id: str
    sequence: int
    observation: Observation


class ActResponse(ServiceProtocolModel):
    request_id: str
    sequence: int
    action: AgentAction
    usage: UsageSnapshot


class CloseRequest(ServiceProtocolModel):
    protocol_version: str = "1"
    request_id: str


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
        if 300 <= response.status_code < 400:
            raise AgentServiceError(
                "agent service redirects are not allowed",
                code="AGENT_SERVICE_REDIRECT_REJECTED",
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
        allowed_hosts: tuple[str, ...],
        resolver: Callable[[str, int], object] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._resolver = resolver or _resolve_host
        self._endpoint = _validated_endpoint(config, allowed_hosts, self._resolver)
        self._session_id: str | None = None
        self._episode: EpisodeContext | None = None
        self._sequence = 0
        self._usage = UsageSnapshot()
        self._manifest_checked = False
        self._last_exchange: tuple[ActRequest, ActResponse] | None = None

    @property
    def name(self) -> str:
        return "Agent Service Protocol v1"

    async def reset(self, episode: EpisodeContext) -> None:
        if self._session_id is not None:
            raise AgentServiceError("agent service session is already active")
        if not self._manifest_checked:
            await self._check_manifest()
        request_id = str(uuid.uuid4())
        request = CreateSessionRequest(
            request_id=request_id,
            sequence=0,
            episode=episode,
        )
        payload = request.model_dump(mode="json")
        response_payload: dict[str, Any] | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                response_payload = await self._transport.request(
                    "POST",
                    f"{self._endpoint.connect_base}/v1/sessions",
                    json_body=payload,
                    headers=self._authorization_headers(),
                )
                break
            except AgentServiceTimeout:
                if attempt == self._config.max_attempts:
                    raise
        if response_payload is None:
            raise AgentServiceError("agent service session request failed")
        try:
            response = CreateSessionResponse.model_validate(response_payload)
        except ValueError as exc:
            raise AgentServiceProtocolError(
                "agent service returned an invalid session response"
            ) from exc
        _require_correlation(request.request_id, request.sequence, response)
        self._session_id = response.session_id
        self._episode = episode
        self._sequence = 0
        self._usage = UsageSnapshot()

    async def act(self, observation: Observation) -> AgentAction:
        if self._session_id is None or self._episode is None:
            raise AgentServiceError("agent service session is not active")
        request_id = str(uuid.uuid4())
        sequence = self._sequence + 1
        request = ActRequest(
            request_id=request_id,
            sequence=sequence,
            observation=observation,
        )
        payload = request.model_dump(mode="json")
        response_payload: dict[str, Any] | None = None
        for attempt in range(1, self._config.max_attempts + 1):
            try:
                response_payload = await self._transport.request(
                    "POST",
                    f"{self._endpoint.connect_base}/v1/sessions/{self._session_id}:act",
                    json_body=payload,
                    headers=self._authorization_headers(),
                )
                break
            except AgentServiceTimeout:
                if attempt == self._config.max_attempts:
                    raise
        if response_payload is None:
            raise AgentServiceError("agent service act request failed")
        try:
            response = ActResponse.model_validate(response_payload)
        except ValueError as exc:
            raise InvalidAgentAction("agent service returned an invalid action") from exc
        _require_correlation(request.request_id, request.sequence, response)
        action = response.action
        if isinstance(action, ToolCallAction):
            allowed_tools = {tool.name for tool in self._episode.tools}
            if action.tool not in allowed_tools:
                raise InvalidAgentAction("agent service returned an unapproved tool")
        self._usage = response.usage
        self._sequence = sequence
        self._last_exchange = (request, response)
        return action

    def usage(self) -> UsageSnapshot:
        return self._usage

    async def close(self) -> None:
        session_id = self._session_id
        try:
            if session_id is not None:
                await self._transport.request(
                    "POST",
                    f"{self._endpoint.connect_base}/v1/sessions/{session_id}:close",
                    json_body=CloseRequest(
                        request_id=str(uuid.uuid4())
                    ).model_dump(mode="json"),
                    headers=self._authorization_headers(),
                )
        finally:
            self._session_id = None
            self._episode = None
            self._sequence = 0
            self._last_exchange = None

    async def _check_manifest(self) -> None:
        manifest_payload = await self._transport.request(
            "GET",
            f"{self._endpoint.connect_base}/v1/manifest",
            headers={"Host": self._endpoint.host_header},
        )
        try:
            manifest = ServiceManifest.model_validate(manifest_payload)
        except ValueError as exc:
            raise AgentServiceProtocolError(
                "agent service returned an invalid manifest"
            ) from exc
        if manifest.protocol_version != "1":
            raise AgentServiceError("agent service protocol version is not supported")
        if manifest.agent_revision_id != self._config.agent_revision_id:
            raise AgentServiceError("agent service revision does not match registration")
        if manifest_sha256(manifest.model_dump(mode="json")) != self._config.expected_manifest_sha256:
            raise ValueError("agent service manifest hash mismatch")
        self._manifest_checked = True

    def _authorization_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.capability_token.get_secret_value()}",
            "Host": self._endpoint.host_header,
        }


def manifest_sha256(manifest: dict[str, Any]) -> str:
    payload = json.dumps(
        manifest,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class _ResolvedEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    connect_base: str
    host_header: str


def _validated_endpoint(
    config: ServiceConfig,
    allowed_hosts: tuple[str, ...],
    resolver: Callable[[str, int], object],
) -> _ResolvedEndpoint:
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
    if host is None or host not in allowed_hosts:
        raise ValueError("agent service host is not allowlisted")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        raw_addresses = resolver(host, port)
        addresses = tuple(str(address) for address in raw_addresses)
    except (OSError, TypeError) as exc:
        raise ValueError("agent service host could not be resolved") from exc
    if not addresses:
        raise ValueError("agent service host did not resolve to an address")
    try:
        parsed_addresses = tuple(ipaddress.ip_address(address) for address in addresses)
    except ValueError as exc:
        raise ValueError("agent service host returned an invalid address") from exc
    address = str(parsed_addresses[0])
    connect_host = f"[{address}]" if ":" in address else address
    default_port = 443 if parsed.scheme == "https" else 80
    connect_base = f"{parsed.scheme}://{connect_host}:{port}"
    host_header = host if port == default_port else f"{host}:{port}"
    return _ResolvedEndpoint(connect_base=connect_base, host_header=host_header)


def _require_correlation(
    request_id: str,
    sequence: int,
    response: CreateSessionResponse | ActResponse,
) -> None:
    if response.request_id != request_id or response.sequence != sequence:
        raise AgentServiceProtocolError(
            "agent service response correlation mismatch",
            code="AGENT_SERVICE_CORRELATION_ERROR",
        )


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({result[4][0] for result in results}))
