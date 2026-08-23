from __future__ import annotations

import asyncio
import json
import math
from collections.abc import Awaitable, Callable
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict

from secrl_platform.agents.capabilities import (
    CapabilityBudgetError,
    CapabilityRequestCompleted,
    CapabilityRequestInProgress,
    CapabilitySigner,
    InvalidCapability,
)
from secrl_platform.models.pricing import Pricing
from secrl_platform.models.providers import (
    ModelProvider,
    ModelRequest,
    ProviderError,
    Usage,
    provider_payload,
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
        capability_signer: CapabilitySigner | None = None,
    ) -> None:
        self._provider = provider
        self._pricing = pricing
        self._sleep = sleep
        self._capability_signer = capability_signer

    async def complete(self, request: ModelRequest) -> GatewayResponse:
        token: str | None = None
        reservation_usage: Usage | None = None
        reservation_cost: Decimal | None = None
        if self._capability_signer is not None:
            if request.capability_token is None or request.agent_revision_id is None:
                raise InvalidCapability("model request requires a capability token")
            token = request.capability_token.get_secret_value()
            self._capability_signer.verify(
                token,
                expected_run=request.run_id,
                expected_agent=request.agent_revision_id,
                model_role=request.model_role,
            )
            if request.max_output_tokens is None:
                raise CapabilityBudgetError(
                    "budgeted model request requires an enforced output limit"
                )
            input_token_bound = _conservative_input_token_bound(request)
            reservation_usage = Usage(
                prompt=input_token_bound,
                completion=request.max_output_tokens,
            )
            reservation_cost = self._pricing.estimate(reservation_usage)
            if reservation_cost is None:
                raise CapabilityBudgetError(
                    "budgeted model request requires frozen input and output pricing"
                )
            admission = self._capability_signer.begin_request(
                token,
                request_id=request.request_id,
                reserved_tokens=reservation_usage.total,
                reserved_cost=reservation_cost,
                expected_run=request.run_id,
                expected_agent=request.agent_revision_id,
                model_role=request.model_role,
            )
            if admission.status == "COMPLETED":
                if admission.actual is None:
                    raise CapabilityBudgetError(
                        "completed capability request is missing usage"
                    )
                raise CapabilityRequestCompleted(
                    actual_tokens=admission.actual[0],
                    actual_cost=admission.actual[1],
                )
            if admission.status == "IN_PROGRESS":
                raise CapabilityRequestInProgress(
                    "capability request is already in progress"
                )
        for attempt in range(1, request.max_attempts + 1):
            try:
                response = await self._provider.complete(request)
            except ProviderError as exc:
                if (
                    exc.usage_may_have_occurred
                    or not exc.transient
                    or attempt == request.max_attempts
                ):
                    if token is not None:
                        if reservation_usage is None or reservation_cost is None:
                            raise RuntimeError(
                                "capability reservation state is missing"
                            ) from exc
                        if exc.usage_may_have_occurred:
                            self._capability_signer.reconcile_usage(
                                token,
                                request_id=request.request_id,
                                actual_tokens=reservation_usage.total,
                                actual_cost=reservation_cost,
                                expected_run=request.run_id,
                                expected_agent=request.agent_revision_id,
                                model_role=request.model_role,
                            )
                        else:
                            self._capability_signer.cancel_reservation(
                                token,
                                request_id=request.request_id,
                                expected_run=request.run_id,
                                expected_agent=request.agent_revision_id,
                                model_role=request.model_role,
                            )
                    raise
                delay = exc.retry_after
                if delay is None or not math.isfinite(delay):
                    delay = min(0.5 * (2 ** (attempt - 1)), 5.0)
                delay = min(max(delay, 0.0), 30.0)
                await self._sleep(delay)
                continue
            estimated_cost = self._pricing.estimate(response.usage)
            if self._capability_signer is not None and token is not None:
                if response.usage is None or estimated_cost is None:
                    if reservation_usage is None or reservation_cost is None:
                        raise RuntimeError("capability reservation state is missing")
                    self._capability_signer.reconcile_usage(
                        token,
                        request_id=request.request_id,
                        actual_tokens=reservation_usage.total,
                        actual_cost=reservation_cost,
                        expected_run=request.run_id,
                        expected_agent=request.agent_revision_id,
                        model_role=request.model_role,
                    )
                    raise CapabilityBudgetError(
                        "budgeted model response requires usage and pricing"
                    )
                try:
                    self._capability_signer.reconcile_usage(
                        token,
                        request_id=request.request_id,
                        actual_tokens=response.usage.total,
                        actual_cost=estimated_cost,
                        expected_run=request.run_id,
                        expected_agent=request.agent_revision_id,
                        model_role=request.model_role,
                    )
                except CapabilityBudgetError:
                    if reservation_usage is None or reservation_cost is None:
                        raise RuntimeError("capability reservation state is missing")
                    self._capability_signer.reconcile_usage(
                        token,
                        request_id=request.request_id,
                        actual_tokens=reservation_usage.total,
                        actual_cost=reservation_cost,
                        expected_run=request.run_id,
                        expected_agent=request.agent_revision_id,
                        model_role=request.model_role,
                    )
                    raise
            return GatewayResponse(
                text=response.text,
                usage=response.usage,
                estimated_cost=estimated_cost,
                pricing_profile_sha256=self._pricing.sha256(),
                provider_request_id=response.provider_request_id,
                raw_usage=response.raw_usage,
            )
        raise RuntimeError("model gateway retry loop exited unexpectedly")


def _conservative_input_token_bound(request: ModelRequest) -> int:
    allowed = {
        "frequency_penalty",
        "n",
        "presence_penalty",
        "response_format",
        "seed",
        "stop",
        "temperature",
        "tool_choice",
        "tools",
        "top_p",
    }
    unknown = set(request.effective_parameters).difference(allowed)
    if unknown:
        raise CapabilityBudgetError(
            "budgeted model request contains parameters with unknown budget impact"
        )
    output_count = request.effective_parameters.get("n", 1)
    if not isinstance(output_count, int) or isinstance(output_count, bool) or output_count != 1:
        raise CapabilityBudgetError(
            "budgeted model request must produce exactly one completion"
        )
    try:
        canonical_payload = json.dumps(
            provider_payload(request),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise CapabilityBudgetError(
            "budgeted model request payload is not canonical JSON"
        ) from exc
    # UTF-8 bytes plus fixed provider framing is a conservative upper bound for
    # supported tokenizers and includes tools/response schemas/stop sequences.
    return len(canonical_payload) + 8
