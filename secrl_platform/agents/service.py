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
from secrl_platform.config import Settings


class AgentServiceError(RuntimeError):
    code = "INTERNAL"
    transient_codes = frozenset({"DEADLINE_EXCEEDED", "RATE_LIMITED", "UNAVAILABLE"})

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code

    @property
    def transient(self) -> bool:
        return self.code in self.transient_codes


class AgentServiceTimeout(AgentServiceError):
    code = "DEADLINE_EXCEEDED"


class AgentServiceProtocolError(AgentServiceError):
    code = "PROTOCOL_MISMATCH"


class InvalidAgentAction(AgentServiceError):
    code = "INVALID_ACTION"


class ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    endpoint: str
    expected_manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    agent_revision_id: str
    capability_token: SecretStr = Field(repr=False)
    max_attempts: int = Field(default=2, ge=1, le=3)


class AgentServiceEndpointPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    allowed_hosts: tuple[str, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> "AgentServiceEndpointPolicy":
        return cls(allowed_hosts=settings.agent_service_allowlist)


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
                follow_redirects=False,
            )
        except httpx.TimeoutException as exc:
            raise AgentServiceTimeout("agent service request timed out") from exc
        except httpx.RequestError as exc:
            raise AgentServiceError(
                "agent service request failed",
                code="UNAVAILABLE",
            ) from exc
        if response.status_code >= 400:
            raise _http_service_error(response)
        if 300 <= response.status_code < 400:
            raise AgentServiceError(
                "agent service redirects are not allowed",
                code="PROTOCOL_MISMATCH",
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentServiceProtocolError(
                "agent service returned invalid JSON"
            ) from exc
        if not isinstance(payload, dict):
            raise AgentServiceProtocolError(
                "agent service response must be an object"
            )
        return payload


class AgentServiceRuntime:
    def __init__(
        self,
        *,
        config: ServiceConfig,
        transport: AgentServiceTransport,
        _policy: AgentServiceEndpointPolicy,
        resolver: Callable[[str, int], object] | None = None,
    ) -> None:
        self._config = config
        self._transport = transport
        self._resolver = resolver or _resolve_host
        self._endpoint = _validated_endpoint(config, _policy, self._resolver)
        self._session_id: str | None = None
        self._episode: EpisodeContext | None = None
        self._sequence = 0
        self._usage = UsageSnapshot()
        self._manifest_checked = False
        self._last_exchange: tuple[ActRequest, ActResponse] | None = None
        self._pending_session: CreateSessionRequest | None = None
        self._pending_act: ActRequest | None = None
        self._pending_close: CloseRequest | None = None

    @classmethod
    def from_settings(
        cls,
        *,
        config: ServiceConfig,
        transport: AgentServiceTransport,
        settings: Settings,
        resolver: Callable[[str, int], object] | None = None,
    ) -> "AgentServiceRuntime":
        return cls(
            config=config,
            transport=transport,
            _policy=AgentServiceEndpointPolicy.from_settings(settings),
            resolver=resolver,
        )

    @property
    def name(self) -> str:
        return "Agent Service Protocol v1"

    async def reset(self, episode: EpisodeContext) -> None:
        if self._session_id is not None:
            raise AgentServiceError("agent service session is already active")
        if not self._manifest_checked:
            await self._check_manifest()
        request = self._pending_session
        if request is not None and request.episode != episode:
            raise AgentServiceProtocolError(
                "a different session creation request is still pending"
            )
        if request is None:
            request = CreateSessionRequest(
                request_id=str(uuid.uuid4()),
                sequence=0,
                episode=episode,
            )
            self._pending_session = request
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
            except AgentServiceError as exc:
                if not exc.transient or attempt == self._config.max_attempts:
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
        self._pending_session = None
        self._session_id = response.session_id
        self._episode = episode
        self._sequence = 0
        self._usage = UsageSnapshot()

    async def act(self, observation: Observation) -> AgentAction:
        if self._session_id is None or self._episode is None:
            raise AgentServiceError("agent service session is not active")
        request = self._pending_act
        if request is not None and request.observation != observation:
            raise AgentServiceProtocolError(
                "a different agent action request is still pending"
            )
        if request is None:
            request = ActRequest(
                request_id=str(uuid.uuid4()),
                sequence=self._sequence + 1,
                observation=observation,
            )
            self._pending_act = request
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
            except AgentServiceError as exc:
                if not exc.transient or attempt == self._config.max_attempts:
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
        self._sequence = request.sequence
        self._last_exchange = (request, response)
        self._pending_act = None
        return action

    def usage(self) -> UsageSnapshot:
        return self._usage

    async def close(self) -> None:
        session_id = self._session_id
        if session_id is not None:
            request = self._pending_close or CloseRequest(request_id=str(uuid.uuid4()))
            self._pending_close = request
            for attempt in range(1, self._config.max_attempts + 1):
                try:
                    await self._transport.request(
                        "POST",
                        f"{self._endpoint.connect_base}/v1/sessions/{session_id}:close",
                        json_body=request.model_dump(mode="json"),
                        headers=self._authorization_headers(),
                    )
                    break
                except AgentServiceError as exc:
                    if not exc.transient or attempt == self._config.max_attempts:
                        raise
        self._pending_close = None
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
            raise AgentServiceProtocolError(
                "agent service protocol version is not supported"
            )
        if manifest.agent_revision_id != self._config.agent_revision_id:
            raise AgentServiceProtocolError(
                "agent service revision does not match registration"
            )
        if manifest_sha256(manifest.model_dump(mode="json")) != self._config.expected_manifest_sha256:
            raise AgentServiceProtocolError("agent service manifest hash mismatch")
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


def _http_service_error(response: httpx.Response) -> AgentServiceError:
    status = response.status_code
    code = {
        404: "SESSION_NOT_FOUND",
        408: "DEADLINE_EXCEEDED",
        409: "PROTOCOL_MISMATCH",
        422: "INVALID_ACTION",
        429: "RATE_LIMITED",
    }.get(status)
    if code is None:
        code = "UNAVAILABLE" if status >= 500 else "INTERNAL"
    return AgentServiceError(
        f"agent service returned HTTP {status}",
        code=code,
    )


class _ResolvedEndpoint(BaseModel):
    model_config = ConfigDict(frozen=True)

    connect_base: str
    host_header: str


def _validated_endpoint(
    config: ServiceConfig,
    policy: AgentServiceEndpointPolicy,
    resolver: Callable[[str, int], object],
) -> _ResolvedEndpoint:
    parsed = urlsplit(config.endpoint)
    if parsed.scheme != "http":
        raise ValueError(
            "Agent Service Protocol v1 endpoints must use pinned internal HTTP"
        )
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("agent service endpoint must not include user information")
    if parsed.fragment or parsed.query:
        raise ValueError("agent service endpoint must not include query or fragment")
    if parsed.path not in {"", "/"}:
        raise ValueError("agent service endpoint must not include a path")
    host = parsed.hostname
    if host is None or host not in policy.allowed_hosts:
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
            code="PROTOCOL_MISMATCH",
        )


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({result[4][0] for result in results}))
