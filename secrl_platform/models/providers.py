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
from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator
from uuid import uuid4


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
        self._base_url = validate_model_endpoint(
            base_url,
            allowed_hosts=allowed_hosts,
            resolver=self._resolver,
        ).rstrip("/")
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
                async with httpx.AsyncClient(follow_redirects=False) as client:
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
        return base_url
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
    return base_url


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
        return ProviderError("TIMEOUT", retry_after=retry_after)
    if status == 429:
        return ProviderError("RATE_LIMITED", retry_after=retry_after)
    if status >= 500:
        return ProviderError("PROVIDER_UNAVAILABLE", retry_after=retry_after)
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
