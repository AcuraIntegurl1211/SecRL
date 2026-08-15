from __future__ import annotations

from typing import Any, Literal, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field


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


class ModelResponse(ProviderModel):
    text: str
    usage: Usage | None
    provider_request_id: str | None = None
    raw_usage: dict[str, Any] | None = None


class ProviderError(RuntimeError):
    TRANSIENT_CODES = frozenset({"TIMEOUT", "RATE_LIMITED", "PROVIDER_UNAVAILABLE"})

    def __init__(self, code: str, *, retry_after: float | None = None) -> None:
        super().__init__(f"model provider request failed: {code}")
        self.code = code
        self.retry_after = retry_after

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
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._client = client

    async def complete(self, request: ModelRequest) -> ModelResponse:
        payload = {
            "model": request.model,
            "messages": [message.model_dump(mode="json") for message in request.messages],
            **request.effective_parameters,
        }
        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            if self._client is None:
                async with httpx.AsyncClient() as client:
                    response = await client.post(
                        f"{self._base_url}/chat/completions",
                        json=payload,
                        headers=headers,
                        timeout=request.timeout_seconds,
                    )
            else:
                response = await self._client.post(
                    f"{self._base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                    timeout=request.timeout_seconds,
                )
        except httpx.TimeoutException as exc:
            raise ProviderError("TIMEOUT") from exc
        except httpx.RequestError as exc:
            raise ProviderError("PROVIDER_UNAVAILABLE") from exc

        if response.status_code >= 400:
            raise _status_error(response)
        try:
            body = response.json()
            text = body["choices"][0]["message"]["content"]
            raw_usage = body.get("usage")
            usage = _normalize_usage(raw_usage) if raw_usage is not None else None
            request_id = body.get("id")
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise ProviderError("INVALID_PROVIDER_RESPONSE") from exc
        return ModelResponse(
            text=text,
            usage=usage,
            provider_request_id=request_id,
            raw_usage=raw_usage,
        )


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
