from __future__ import annotations

import math
import ipaddress
import logging
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


_LOGGER = logging.getLogger(__name__)


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
    finish_reason: str | None = None
    reasoning_content: str | None = None
    raw_usage: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    TRANSIENT_CODES = frozenset({"TIMEOUT", "RATE_LIMITED", "PROVIDER_UNAVAILABLE"})

    def __init__(
        self,
        code: str,
        *,
        retry_after: float | None = None,
        usage_may_have_occurred: bool = False,
        http_status: int | None = None,
        content_type: str | None = None,
        provider_request_id: str | None = None,
        request_id: str | None = None,
        response_shape: str | None = None,
    ) -> None:
        super().__init__(f"model provider request failed: {code}")
        self.code = code
        self.retry_after = retry_after
        self.usage_may_have_occurred = usage_may_have_occurred
        self.http_status = http_status
        self.content_type = content_type
        self.provider_request_id = provider_request_id
        self.request_id = request_id
        self.response_shape = response_shape

    @property
    def transient(self) -> bool:
        return self.code in self.TRANSIENT_CODES

    @property
    def safe_to_retry(self) -> bool:
        """True only when the request outcome cannot have incurred usage."""
        return self.transient and not self.usage_may_have_occurred


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

        response_context = _safe_response_context(response, request_id=request.request_id)
        if 300 <= response.status_code < 400:
            raise ProviderError("PROVIDER_REDIRECT", **response_context)
        if response.status_code >= 400:
            raise _status_error(response, request_id=request.request_id)
        try:
            body = response.json()
        except ValueError as exc:
            raise _invalid_response(
                request,
                response,
                response_shape="malformed_json",
            ) from exc
        try:
            if not isinstance(body, dict):
                raise TypeError("response body is not an object")
            choices = body.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("response choices are empty")
            choice = choices[0]
            if not isinstance(choice, dict):
                raise TypeError("response choice is not an object")
            message = choice.get("message")
            if not isinstance(message, dict):
                raise TypeError("response message is not an object")
            content = message.get("content")
            reasoning_content = message.get("reasoning_content")
            if not isinstance(reasoning_content, (str, type(None))):
                raise TypeError("reasoning_content is not a string")
            if not isinstance(content, str) or not content.strip():
                if not isinstance(reasoning_content, str) or not reasoning_content.strip():
                    raise ValueError("response content is empty")
                content = reasoning_content
            finish_reason = choice.get("finish_reason")
            if not isinstance(finish_reason, (str, type(None))):
                raise TypeError("finish_reason is not a string")
            raw_usage = body.get("usage")
            usage = _normalize_usage(raw_usage) if raw_usage is not None else None
            provider_request_id = _safe_identifier(body.get("id"))
        except (TypeError, ValueError, KeyError) as exc:
            raise _invalid_response(
                request,
                response,
                response_shape=_response_shape(body, exc),
            ) from exc
        return ModelResponse(
            text=content,
            usage=usage,
            provider_request_id=provider_request_id,
            finish_reason=finish_reason,
            reasoning_content=(
                reasoning_content
                if isinstance(reasoning_content, str) and reasoning_content.strip()
                else None
            ),
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
    if not allowed_hosts:
        raise ValueError("model provider host allowlist is required")
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


def _status_error(response: httpx.Response, *, request_id: str | None = None) -> ProviderError:
    status = response.status_code
    retry_after = _retry_after_seconds(response.headers.get("Retry-After"))
    context = _safe_response_context(response, request_id=request_id)
    if status in {401, 403}:
        return ProviderError("AUTHENTICATION_FAILED", **context)
    if status == 404:
        return ProviderError("MODEL_NOT_FOUND", **context)
    if status == 408:
        return ProviderError(
            "TIMEOUT",
            retry_after=retry_after,
            usage_may_have_occurred=True,
            **context,
        )
    if status == 429:
        return ProviderError("RATE_LIMITED", retry_after=retry_after, **context)
    if status >= 500:
        return ProviderError(
            "PROVIDER_UNAVAILABLE",
            retry_after=retry_after,
            usage_may_have_occurred=True,
            **context,
        )
    return ProviderError("INVALID_PROVIDER_REQUEST", **context)


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


def _normalize_usage(raw: Any) -> Usage:
    if not isinstance(raw, dict):
        raise TypeError("usage is not an object")
    prompt_details = raw.get("prompt_tokens_details") or {}
    completion_details = raw.get("completion_tokens_details") or {}
    if not isinstance(prompt_details, dict) or not isinstance(completion_details, dict):
        raise TypeError("usage details are not objects")
    return Usage(
        prompt=_usage_count(raw, "prompt_tokens"),
        completion=_usage_count(raw, "completion_tokens"),
        cached=_optional_usage_count(prompt_details, "cached_tokens"),
        reasoning=_optional_usage_count(completion_details, "reasoning_tokens"),
    )


def _usage_count(raw: dict[str, Any], key: str) -> int:
    value = raw[key]
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"usage field {key} is invalid")
    return value


def _optional_usage_count(raw: dict[str, Any], key: str) -> int | None:
    if key not in raw or raw[key] is None:
        return None
    return _usage_count(raw, key)


def _safe_identifier(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    if not normalized or len(normalized) > 256:
        return None
    return normalized


def _safe_response_context(
    response: httpx.Response,
    *,
    request_id: str | None,
) -> dict[str, Any]:
    return {
        "http_status": response.status_code,
        "content_type": _safe_identifier(response.headers.get("content-type")),
        "provider_request_id": _safe_identifier(
            response.headers.get("x-request-id")
            or response.headers.get("x-correlation-id")
            or response.headers.get("request-id")
        ),
        "request_id": _safe_identifier(request_id),
    }


def _invalid_response(
    request: ModelRequest,
    response: httpx.Response,
    *,
    response_shape: str,
) -> ProviderError:
    context = _safe_response_context(response, request_id=request.request_id)
    _LOGGER.warning(
        "provider response rejected request_id=%s http_status=%s content_type=%s provider_request_id=%s response_shape=%s",
        context["request_id"],
        context["http_status"],
        context["content_type"],
        context["provider_request_id"],
        response_shape,
    )
    return ProviderError(
        "INVALID_PROVIDER_RESPONSE",
        usage_may_have_occurred=True,
        response_shape=response_shape,
        **context,
    )


def _response_shape(body: Any, error: Exception) -> str:
    if isinstance(body, dict):
        choices = body.get("choices")
        if choices == []:
            return "empty_choices"
        if isinstance(choices, list) and choices:
            return "invalid_choice"
        if "usage" in body:
            return "invalid_usage"
    return type(error).__name__.lower()
