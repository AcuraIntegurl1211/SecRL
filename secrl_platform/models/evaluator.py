"""Frozen, role-separated evaluator contract for SecRL."""

from __future__ import annotations

import asyncio
import hashlib
import re
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from secrl_platform.agents.protocol import UsageSnapshot
from secrl_platform.models.gateway import ModelGateway
from secrl_platform.models.providers import ModelMessage, ModelRequest


EVALUATOR_PROMPT_TEMPLATE = (
    "Question: {question}\n"
    "Golden Answer: {gold_answer}\n"
    "Submitted Answer: {submitted_answer}\n"
    "The submitted answer may differ in format or case, but must contain the key "
    "content without an irrelevant enumeration over 15 entries.\n"
    "Return exactly: Analysis: <brief>\\nIs_Answer_Correct: <True|False>"
)
EVALUATOR_PROMPT_TEMPLATE_SHA256 = hashlib.sha256(EVALUATOR_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


class EvaluatorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = "secrl-official-v1"
    model_revision: str = "static-evaluator-v1"
    prompt_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int = 41
    retry_policy: str = "none"
    parser_version: str = "secrl-answer-v1"
    success_rule: str = "exact_normalized"
    formal: bool = True

    @field_validator("prompt_template_sha256")
    @classmethod
    def validate_template_hash(cls, value: str) -> str:
        if value != EVALUATOR_PROMPT_TEMPLATE_SHA256:
            raise ValueError("evaluator prompt template hash is not approved")
        return value


class EvaluatorParameterOverride(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    seed: int | None = None
    retry_policy: str | None = None


class EvaluatorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    question_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    prompt_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    requested_parameters: dict[str, Any] = Field(default_factory=dict)
    effective_parameters: dict[str, Any]
    parser_version: str
    role: str = "evaluator"

    @field_validator("role")
    @classmethod
    def evaluator_role_only(cls, value: str) -> str:
        if value != "evaluator":
            raise ValueError("evaluator request role is fixed")
        return value


class EvaluatorUsage(UsageSnapshot):
    role: str = "evaluator"


@dataclass(frozen=True)
class RestrictedRawResponseRef:
    sha256: str
    length: int


@dataclass(frozen=True)
class EvaluatorResponse:
    request: EvaluatorRequest
    reward: float
    correct: bool
    usage: EvaluatorUsage
    raw_response: RestrictedRawResponseRef
    _raw_text: str = field(repr=False, compare=False)
    _access: object = field(repr=False, compare=False)

    def read_raw_response(self, access: object | None = None) -> str:
        if access is not self._access:
            raise PermissionError("evaluator response artifact is restricted")
        return self._raw_text


def official_secrl_profile(
    *,
    formal: bool = True,
    model_revision: str = "static-evaluator-v1",
) -> EvaluatorProfile:
    return EvaluatorProfile(
        model_revision=model_revision,
        prompt_template_sha256=EVALUATOR_PROMPT_TEMPLATE_SHA256,
        formal=formal,
    )


class EvaluatorGatewayClient:
    """Synchronous evaluator facade over the async, capability-bound gateway."""

    def __init__(
        self,
        *,
        gateway: ModelGateway,
        model: str,
        capability_token: str,
        agent_revision_id: str,
        max_output_tokens: int,
    ) -> None:
        self._gateway = gateway
        self._model = model
        self._capability_token = SecretStr(capability_token)
        self._agent_revision_id = agent_revision_id
        self._max_output_tokens = max_output_tokens
        self._attempt: tuple[str, str, str] | None = None

    def bind_attempt(self, *, run_id: str, case_id: str, attempt_id: str) -> None:
        self._attempt = (run_id, case_id, attempt_id)

    def complete(self, *, prompt: str, parameters: Mapping[str, Any]) -> dict[str, Any]:
        if self._attempt is None:
            raise RuntimeError("evaluator model client has no active attempt")
        run_id, case_id, attempt_id = self._attempt
        request = ModelRequest(
            provider_adapter_version="openai-compatible-v1",
            model_role="evaluator",
            model=self._model,
            messages=(ModelMessage(role="user", content=prompt),),
            effective_parameters=dict(parameters),
            run_id=run_id,
            case_id=case_id,
            attempt_id=attempt_id,
            agent_revision_id=self._agent_revision_id,
            capability_token=self._capability_token,
            max_output_tokens=self._max_output_tokens,
            max_attempts=1,
        )
        response = asyncio.run(self._gateway.complete(request))
        usage = response.usage
        return {
            "text": response.text,
            "usage": {
                "prompt_tokens": usage.prompt if usage is not None else 0,
                "completion_tokens": usage.completion if usage is not None else 0,
            },
        }


class SecRLEvaluator:
    def __init__(
        self,
        profile: EvaluatorProfile,
        *,
        model_client: Any | None = None,
    ) -> None:
        if profile.prompt_template_sha256 != EVALUATOR_PROMPT_TEMPLATE_SHA256:
            raise ValueError("unapproved evaluator prompt template")
        self.profile = profile
        self._model_client = model_client
        self._access = object()

    def restricted_access(self) -> object:
        return self._access

    def evaluate(
        self,
        *,
        question: str,
        gold_answer: str,
        submitted_answer: str,
        overrides: EvaluatorParameterOverride | None = None,
    ) -> EvaluatorResponse:
        requested = overrides.model_dump(exclude_none=True) if overrides is not None else {}
        if self.profile.formal and requested:
            raise ValueError("formal evaluator profile rejects per-task parameter overrides")
        effective: dict[str, Any] = {
            "temperature": self.profile.temperature,
            "seed": self.profile.seed,
        }
        effective.update(requested)
        prompt = EVALUATOR_PROMPT_TEMPLATE.format(
            question=question,
            gold_answer=gold_answer,
            submitted_answer=submitted_answer,
        )
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()
        raw_text, usage = self._evaluate_response(prompt, question, gold_answer, submitted_answer, effective)
        decision = _parse_decision(raw_text)
        if decision is None:
            raise ValueError("official evaluator response did not match parser contract")
        correct = bool(decision)
        request = EvaluatorRequest(
            question_sha256=hashlib.sha256(question.encode("utf-8")).hexdigest(),
            prompt_sha256=prompt_sha,
            requested_parameters=requested,
            effective_parameters=effective,
            parser_version=self.profile.parser_version,
        )
        raw_bytes = raw_text.encode("utf-8")
        return EvaluatorResponse(
            request=request,
            reward=1.0 if correct else 0.0,
            correct=correct,
            usage=usage,
            raw_response=RestrictedRawResponseRef(
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                length=len(raw_bytes),
            ),
            _raw_text=raw_text,
            _access=self._access,
        )

    def _evaluate_response(
        self,
        prompt: str,
        question: str,
        gold_answer: str,
        submitted_answer: str,
        parameters: Mapping[str, Any],
    ) -> tuple[str, EvaluatorUsage]:
        if self._model_client is None:
            correct = _normalize(submitted_answer) == _normalize(gold_answer)
            return (
                "Analysis: deterministic official SecRL evaluator\n"
                f"Is_Answer_Correct: {'True' if correct else 'False'}",
                EvaluatorUsage(),
            )
        client = self._model_client
        if hasattr(client, "complete"):
            response = client.complete(prompt=prompt, parameters=dict(parameters))
        elif callable(client):
            response = client(prompt, dict(parameters))
        else:
            raise TypeError("evaluator model client must expose complete() or be callable")
        usage_value: Mapping[str, Any] = {}
        if isinstance(response, Mapping):
            raw = response.get("text", response.get("content", ""))
            usage_value = response.get("usage", {}) if isinstance(response.get("usage", {}), Mapping) else {}
        else:
            raw = getattr(response, "text", getattr(response, "content", response))
            usage_value = getattr(response, "usage", {})
        text = str(raw)
        usage = EvaluatorUsage(
            prompt_tokens=max(0, int(usage_value.get("prompt_tokens", 0))) if isinstance(usage_value, Mapping) else 0,
            completion_tokens=max(0, int(usage_value.get("completion_tokens", 0))) if isinstance(usage_value, Mapping) else 0,
        )
        return text, usage


def _parse_decision(raw: str) -> bool | None:
    match = re.search(r"Is_Answer_Correct\s*:\s*(True|False)\b", raw, flags=re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).casefold() == "true"


def _normalize(value: str) -> str:
    return " ".join(value.casefold().strip().split())
