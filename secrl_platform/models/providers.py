from __future__ import annotations

import math
import ipaddress
import socket
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from collections.abc import Callable
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit

import httpx
import httpcore
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from uuid import uuid4

from secrl_platform.models.secrets import EncryptedSecret, SecretStore


class ProviderModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ModelMessage(ProviderModel):
    role: Literal["system", "user", "assistant", "tool"]
    content: str


class Usage(ProviderModel):
    prompt: int = Field(ge=0)
    completion: int = Field(ge=0)
    cached: int | None = Field(default=None, ge=0)
    reasoning: int | None = Field(default=None, ge=0)

    @property
    def total(self) -> int:
        return self.prompt + self.completion


class ModelRequest(ProviderModel):
    provider_adapter_version: str
    model_role: str
    model: str
    messages: tuple[ModelMessage, ...]
    requested_parameters: dict[str, Any] = Field(default_factory=dict)
    effective_parameters: dict[str, Any] = Field(default_factory=dict)
    timeout_seconds: float = Field(default=30.0, gt=0, le=600)
    run_id: str
    case_id: str
    attempt_id: str
    cache_metadata: dict[str, Any] = Field(default_factory=dict)
    max_attempts: int = Field(default=3, ge=1, le=10)
    agent_revision_id: str | None = None
    capability_token: SecretStr | None = Field(default=None, repr=False)
    request_id: str = Field(default_factory=lambda: str(uuid4()))
    max_output_tokens: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def reject_reserved_parameters(self) -> "ModelRequest":
        reserved = {"model", "messages", "max_tokens"}
        if reserved.intersection(self.requested_parameters):
            raise ValueError("requested parameters contain reserved provider fields")
        if reserved.intersection(self.effective_parameters):
            raise ValueError("effective parameters contain reserved provider fields")
        return self


class ModelResponse(ProviderModel):
    text: str
    usage: Usage | None
    provider_request_id: str | None = None
    raw_usage: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    TRANSIENT_CODES = frozenset({"TIMEOUT", "RATE_LIMITED", "PROVIDER_UNAVAILABLE"})

    def __init__(
        self,
        code: str,
        *,
        retry_after: float | None = None,
        usage_may_have_occurred: bool = False,
    ) -> None:
        super().__init__(f"model provider request failed: {code}")
        self.code = code
        self.retry_after = retry_after
        self.usage_may_have_occurred = usage_may_have_occurred

    @property
    def transient(self) -> bool:
        return self.code in self.TRANSIENT_CODES


class ModelProvider(Protocol):
    async def complete(self, request: ModelRequest) -> ModelResponse: ...


class DeferredSecretProvider:
    """Decrypt a stored API key only for the duration of one provider call."""

    def __init__(
        self,
        *,
        secret_store: SecretStore,
        encrypted_secret: EncryptedSecret,
        provider_factory: Callable[[str], ModelProvider],
    ) -> None:
        self._secret_store = secret_store
        self._encrypted_secret = encrypted_secret
        self._provider_factory = provider_factory

    async def complete(self, request: ModelRequest) -> ModelResponse:
        api_key = self._secret_store.decrypt(self._encrypted_secret)
        try:
            provider = self._provider_factory(api_key)
            return await provider.complete(request)
        finally:
            api_key = ""


class OpenAICompatibleProvider:
    def __init__(
        self,
        *,
        base_url: str,
        api_key: str,
        client: httpx.AsyncClient | None = None,
        allowed_hosts: tuple[str, ...],
        resolver: Callable[[str, int], object] | None = None,
    ) -> None:
        self._resolver = resolver or _resolve_host
        self._allowed_hosts = allowed_hosts
        self._base_url, addresses = _validated_model_endpoint(
            base_url,
            allowed_hosts=allowed_hosts,
            resolver=self._resolver,
        )
        self._base_url = self._base_url.rstrip("/")
        self._pinned_address = addresses[0]
        self._api_key = api_key
        self._client = client

    async def complete(self, request: ModelRequest) -> ModelResponse:
        validate_model_endpoint(
            self._base_url,
            allowed_hosts=self._allowed_hosts,
            resolver=self._resolver,
        )
        payload = provider_payload(request)
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is None:
                parsed = urlsplit(self._base_url)
                backend = _PinnedNetworkBackend(
                    hostname=parsed.hostname or "",
                    address=self._pinned_address,
                )
                transport = _PinnedAsyncHTTPTransport(backend)
                async with httpx.AsyncClient(
                    follow_redirects=False,
                    transport=transport,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=request.timeout_seconds,
                        follow_redirects=False,
                    )
            else:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=request.timeout_seconds,
                    follow_redirects=False,
                )
        except httpx.TimeoutException as exc:
            usage_may_have_occurred = not isinstance(
                exc,
                (httpx.ConnectTimeout, httpx.PoolTimeout),
            )
            raise ProviderError(
                "TIMEOUT",
                usage_may_have_occurred=usage_may_have_occurred,
            ) from exc
        except httpx.RequestError as exc:
            raise ProviderError(
                "PROVIDER_UNAVAILABLE",
                usage_may_have_occurred=not isinstance(exc, httpx.ConnectError),
            ) from exc

        if 300 <= response.status_code < 400:
            raise ProviderError("PROVIDER_REDIRECT")
        if response.status_code >= 400:
            raise _status_error(response)
        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            raw_usage = body.get("usage")
            usage = _normalize_usage(raw_usage) if raw_usage is not None else None
            request_id = body.get("id")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError(
                "INVALID_PROVIDER_RESPONSE",
                usage_may_have_occurred=True,
            ) from exc
        return ModelResponse(
            text=text,
            usage=usage,
            provider_request_id=request_id,
            raw_usage=raw_usage,
        )


def provider_payload(request: ModelRequest) -> dict[str, Any]:
    payload = {
        **request.effective_parameters,
        "model": request.model,
        "messages": [message.model_dump(mode="json") for message in request.messages],
    }
    if request.max_output_tokens is not None:
        payload["max_tokens"] = request.max_output_tokens
    return payload


def validate_model_endpoint(
    base_url: str,
    *,
    allowed_hosts: tuple[str, ...] | None,
    resolver: Callable[[str, int], object] | None = None,
) -> str:
    endpoint, _addresses = _validated_model_endpoint(
        base_url,
        allowed_hosts=allowed_hosts,
        resolver=resolver,
    )
    return endpoint


def _validated_model_endpoint(
    base_url: str,
    *,
    allowed_hosts: tuple[str, ...] | None,
    resolver: Callable[[str, int], object] | None = None,
) -> tuple[str, tuple[str, ...]]:
    parsed = urlsplit(base_url)
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("model provider endpoint must use HTTPS")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("model provider endpoint must not include user information")
    if parsed.query or parsed.fragment:
        raise ValueError("model provider endpoint must not include query or fragment")
    host = parsed.hostname.lower()
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None
    if literal is not None and not literal.is_global:
        raise ValueError("model provider endpoint must not use a private address")
    if allowed_hosts is None:
        return base_url, (host,)
    normalized_allowlist = {value.lower() for value in allowed_hosts}
    if host not in normalized_allowlist:
        raise ValueError("model provider host is not allowlisted")
    port = parsed.port or 443
    resolve = resolver or _resolve_host
    try:
        raw_addresses = resolve(host, port)
        addresses = tuple(str(address) for address in raw_addresses)
    except (OSError, TypeError) as exc:
        raise ValueError("model provider host could not be resolved") from exc
    if not addresses:
        raise ValueError("model provider host did not resolve to an address")
    try:
        parsed_addresses = tuple(ipaddress.ip_address(value) for value in addresses)
    except ValueError as exc:
        raise ValueError("model provider host returned an invalid address") from exc
    if any(not address.is_global for address in parsed_addresses):
        raise ValueError("model provider host resolved to a non-global address")
    return base_url, tuple(str(address) for address in parsed_addresses)


class _PinnedNetworkBackend:
    """Resolve one validated provider hostname to one pre-approved address."""

    def __init__(
        self,
        *,
        hostname: str,
        address: str,
        backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        self._hostname = hostname.lower()
        self._address = address
        self._backend = backend or httpcore.AnyIOBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options=None,
    ):
        normalized = host.decode("ascii") if isinstance(host, bytes) else host
        if normalized.lower() != self._hostname:
            raise httpcore.ConnectError("connection host was not validated")
        return await self._backend.connect_tcp(
            self._address,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(self, path: str, **kwargs):
        return await self._backend.connect_unix_socket(path, **kwargs)

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, backend: _PinnedNetworkBackend) -> None:
        super().__init__(trust_env=False, retries=0)
        self._pool = httpcore.AsyncConnectionPool(
            ssl_context=httpx.create_ssl_context(trust_env=False),
            network_backend=backend,
            retries=0,
        )


def _resolve_host(host: str, port: int) -> tuple[str, ...]:
    results = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    return tuple(sorted({result[4][0] for result in results}))


def _status_error(response: httpx.Response) -> ProviderError:
    status = response.status_code
    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
    if status in {401, 403}:
        return ProviderError("AUTHENTICATION_FAILED")
    if status == 404:
        return ProviderError("MODEL_NOT_FOUND")
    if status == 408:
        return ProviderError(
            "TIMEOUT",
            retry_after=retry_after,
            usage_may_have_occurred=True,
        )
    if status == 429:
        return ProviderError("RATE_LIMITED", retry_after=retry_after)
    if status >= 500:
        return ProviderError(
            "PROVIDER_UNAVAILABLE",
            retry_after=retry_after,
            usage_may_have_occurred=True,
        )
    return ProviderError("INVALID_PROVIDER_REQUEST")


def _retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        seconds = (retry_at - datetime.now(timezone.utc)).total_seconds()
    if not math.isfinite(seconds):
        return None
    return max(0.0, seconds)


def _normalize_usage(raw: dict[str, Any]) -> Usage:
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    return Usage(
        prompt=raw["prompt_tokens"],
        completion=raw["completion_tokens"],
        cached=prompt_details.get("cached_tokens"),
        reasoning=completion_details.get("reasoning_tokens"),
    )
