from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import (
    ModelProvider,
    ModelRequest,
    ProviderError,
    Usage,
)


class GatewayResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    text: str
    usage: Usage | None
    estimated_cost: Decimal | None
    pricing_profile_sha256: str
    provider_request_id: str | None = None
    raw_usage: dict[str, Any] | None = None


class ModelGateway:
    def __init__(
        self,
        *,
        provider: ModelProvider,
        pricing: Pricing,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self._provider = provider
        self._pricing = pricing
        self._sleep = sleep

    async def complete(self, request: ModelRequest) -> GatewayResponse:
        for attempt in range(1, request.max_attempts + 1):
            try:
                response = await self._provider.complete(request)
            except ProviderError as exc:
                if not exc.transient or attempt == request.max_attempts:
                    raise
                delay = exc.retry_after
                if delay is None:
                    delay = min(0.5 * (2 ** (attempt - 1)), 5.0)
                await self._sleep(delay)
                continue
            return GatewayResponse(
                text=response.text,
                usage=response.usage,
                estimated_cost=self._pricing.estimate(response.usage),
                pricing_profile_sha256=self._pricing.sha256(),
                provider_request_id=response.provider_request_id,
                raw_usage=response.raw_usage,
            )
        raise RuntimeError("model gateway retry loop exited unexpectedly")
