"""Frozen, role-separated evaluator contract for SecRL."""

from __future__ import annotations

import asyncio
import ast
import hashlib
import inspect
import json
import re
import textwrap
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from typing import Any, Mapping

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator

from secrl_platform.agents.protocol import UsageSnapshot
from secrl_platform.models.gateway import ModelGateway
from secrl_platform.models.providers import ModelMessage, ModelRequest


OFFICIAL_EVALUATOR_SOURCE_SHA256 = "b146af231c0b63d7252c5b7852c62f0ba59ab40980b65be5d003cbc2f08d05e2"
_OFFICIAL_SOURCE = Path(__file__).resolve().parents[2] / "secgym" / "evaluator.py"


def _frozen_official_constants() -> dict[str, str]:
    source = _OFFICIAL_SOURCE.read_bytes()
    if hashlib.sha256(source).hexdigest() != OFFICIAL_EVALUATOR_SOURCE_SHA256:
        raise RuntimeError("SecRL evaluator source does not match the frozen baseline")
    wanted = {
        "EVAL_ANSWER_TEMPLATE",
        "FUZZY_ANSWER_CHECK_PROMPT",
        "FUZZY_ANSWER_CHECK_REFLECTION_PROMPT",
        "EVAL_SOLUTION_TEMPLATE",
        "STEP_CHECK_PROMPT",
        "STEP_CHECK_REFLECTION_PROMPT",
    }
    values: dict[str, str] = {}
    for statement in ast.parse(source, filename=str(_OFFICIAL_SOURCE)).body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name) or target.id not in wanted:
            continue
        value = statement.value
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            values[target.id] = value.value
        elif (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "dedent"
            and len(value.args) == 1
            and isinstance(value.args[0], ast.Constant)
            and isinstance(value.args[0].value, str)
        ):
            values[target.id] = textwrap.dedent(value.args[0].value)
    if set(values) != wanted:
        raise RuntimeError("frozen SecRL evaluator prompt bundle is incomplete")
    return values


_OFFICIAL = _frozen_official_constants()
EVALUATOR_PROMPT_TEMPLATE = (
    "System:\n"
    + _OFFICIAL["FUZZY_ANSWER_CHECK_PROMPT"]
    + "\nUser:\n"
    + _OFFICIAL["EVAL_ANSWER_TEMPLATE"]
)
EVALUATOR_PROMPT_TEMPLATE_SHA256 = hashlib.sha256(EVALUATOR_PROMPT_TEMPLATE.encode("utf-8")).hexdigest()


class EvaluatorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    profile_id: str = "secrl-official-v1"
    model_revision: str = "static-evaluator-v1"
    source_sha256: str = OFFICIAL_EVALUATOR_SOURCE_SHA256
    prompt_template_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    temperature: float = Field(default=0.0, ge=0.0, le=2.0)
    seed: int = 41
    retry_policy: str = "bounded-10"
    parser_version: str = "secrl-answer-v1"
    success_rule: str = "official-fuzzy-and-solution-v1"
    answer_reflection: bool = True
    solution_reflection: bool = True
    step_checking: bool = True
    formal: bool = True

    @field_validator("prompt_template_sha256")
    @classmethod
    def validate_template_hash(cls, value: str) -> str:
        if value != EVALUATOR_PROMPT_TEMPLATE_SHA256:
            raise ValueError("evaluator prompt template hash is not approved")
        return value

    @field_validator("source_sha256")
    @classmethod
    def validate_source_hash(cls, value: str) -> str:
        if value != OFFICIAL_EVALUATOR_SOURCE_SHA256:
            raise ValueError("evaluator source hash is not approved")
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

    def apply_capability_token(self, capability_token: str) -> None:
        """Swap in a refreshed capability token issued for the same claims."""
        self._capability_token = SecretStr(capability_token)

    def complete(
        self,
        *,
        parameters: Mapping[str, Any],
        messages: tuple[Mapping[str, str], ...] | None = None,
        prompt: str | None = None,
    ) -> dict[str, Any]:
        if self._attempt is None:
            raise RuntimeError("evaluator model client has no active attempt")
        if messages is None:
            if prompt is None:
                raise ValueError("evaluator request requires messages")
            messages = ({"role": "user", "content": prompt},)
        run_id, case_id, attempt_id = self._attempt
        request = ModelRequest(
            provider_adapter_version="openai-compatible-v1",
            model_role="evaluator",
            model=self._model,
            messages=tuple(
                ModelMessage(role=str(item["role"]), content=str(item["content"]))
                for item in messages
            ),
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
                "cached_tokens": (usage.cached or 0) if usage is not None else 0,
                "reasoning_tokens": (usage.reasoning or 0) if usage is not None else 0,
                "estimated_cost": str(response.estimated_cost or Decimal(0)),
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
        context: str = "",
        question: str,
        gold_answer: str,
        solution: tuple[str, ...] | list[str] | str | None = None,
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
        full_question = f"{context} {question}".strip()
        answer_input = _OFFICIAL["EVAL_ANSWER_TEMPLATE"].format(
            question=full_question,
            golden_answer=gold_answer,
            submitted_answer=submitted_answer,
        )
        primary_prompt = _prompt_bundle(
            _OFFICIAL["FUZZY_ANSWER_CHECK_PROMPT"],
            answer_input,
        )
        prompt_sha = hashlib.sha256(primary_prompt.encode("utf-8")).hexdigest()
        reward, raw_text, usage = self._evaluate_official(
            full_question=full_question,
            gold_answer=gold_answer,
            solution=solution,
            submitted_answer=submitted_answer,
            answer_input=answer_input,
            parameters=effective,
        )
        correct = reward == 1.0
        request = EvaluatorRequest(
            question_sha256=hashlib.sha256(full_question.encode("utf-8")).hexdigest(),
            prompt_sha256=prompt_sha,
            requested_parameters=requested,
            effective_parameters=effective,
            parser_version=self.profile.parser_version,
        )
        raw_bytes = raw_text.encode("utf-8")
        return EvaluatorResponse(
            request=request,
            reward=reward,
            correct=correct,
            usage=usage,
            raw_response=RestrictedRawResponseRef(
                sha256=hashlib.sha256(raw_bytes).hexdigest(),
                length=len(raw_bytes),
            ),
            _raw_text=raw_text,
            _access=self._access,
        )

    def _evaluate_official(
        self,
        *,
        full_question: str,
        gold_answer: str,
        solution: tuple[str, ...] | list[str] | str | None,
        submitted_answer: str,
        answer_input: str,
        parameters: Mapping[str, Any],
    ) -> tuple[float, str, EvaluatorUsage]:
        if self._model_client is None:
            correct = _normalize(submitted_answer) == _normalize(gold_answer)
            raw = (
                "Analysis: deterministic official SecRL evaluator\n"
                f"Is_Answer_Correct: {'True' if correct else 'False'}"
            )
            return (1.0 if correct else 0.0, raw, EvaluatorUsage())

        raw_responses: list[dict[str, Any]] = []
        usage = EvaluatorUsage()
        answer_text, answer_usage = self._decision_call(
            system_prompt=_OFFICIAL["FUZZY_ANSWER_CHECK_PROMPT"],
            user_prompt=answer_input,
            parameters=parameters,
            seed_offset=0,
        )
        usage = _add_usage(usage, answer_usage)
        raw_responses.append({"stage": "answer", "response": answer_text})
        decision = _parse_decision(answer_text)
        if decision is None:
            raise ValueError("official evaluator response did not match parser contract")

        if self.profile.answer_reflection:
            reflection_text, reflection_usage = self._decision_call(
                system_prompt=_OFFICIAL["FUZZY_ANSWER_CHECK_REFLECTION_PROMPT"],
                user_prompt=f"{answer_input}\n{answer_text}",
                parameters=parameters,
                seed_offset=0,
            )
            usage = _add_usage(usage, reflection_usage)
            raw_responses.append(
                {"stage": "answer_reflection", "response": reflection_text}
            )
            decision = _parse_decision(reflection_text)
            if decision is None:
                raise ValueError("official evaluator reflection did not match parser contract")

        reward = 1.0 if decision else 0.0
        if reward == 0.0 and solution is not None and self.profile.step_checking:
            if isinstance(solution, (list, tuple)):
                golden_solution = "".join(
                    f"Step {index}: {step}\n" for index, step in enumerate(solution)
                )
            else:
                golden_solution = str(solution)
            solution_input = _OFFICIAL["EVAL_SOLUTION_TEMPLATE"].format(
                question=full_question,
                golden_solution=golden_solution,
                submitted_answer=submitted_answer,
            )
            steps, step_text, step_usage = self._json_call(
                system_prompt=_OFFICIAL["STEP_CHECK_PROMPT"],
                user_prompt=solution_input,
                parameters=parameters,
            )
            usage = _add_usage(usage, step_usage)
            raw_responses.append({"stage": "solution", "response": step_text})
            if self.profile.solution_reflection:
                steps, reflection_text, reflection_usage = self._json_call(
                    system_prompt=_OFFICIAL["STEP_CHECK_REFLECTION_PROMPT"],
                    user_prompt=f"{solution_input}\n{json.dumps(steps, ensure_ascii=False)}",
                    parameters=parameters,
                )
                usage = _add_usage(usage, reflection_usage)
                raw_responses.append(
                    {"stage": "solution_reflection", "response": reflection_text}
                )
            reward = _legacy_solution_reward(steps)

        return reward, json.dumps(raw_responses, ensure_ascii=False, separators=(",", ":")), usage

    def _decision_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        parameters: Mapping[str, Any],
        seed_offset: int,
    ) -> tuple[str, EvaluatorUsage]:
        last_text = ""
        total = EvaluatorUsage()
        for retry in range(10):
            effective = dict(parameters)
            effective["seed"] = int(parameters["seed"]) + seed_offset + retry
            last_text, call_usage = self._model_call(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                parameters=effective,
            )
            total = _add_usage(total, call_usage)
            if _parse_decision(last_text) is not None:
                return last_text, total
        raise ValueError("official evaluator response did not match parser contract")

    def _json_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        parameters: Mapping[str, Any],
    ) -> tuple[dict[str, Any], str, EvaluatorUsage]:
        total = EvaluatorUsage()
        for retry in range(10):
            effective = dict(parameters)
            effective["seed"] = int(parameters["seed"]) + 10 + retry
            effective["response_format"] = {"type": "json_object"}
            text, call_usage = self._model_call(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                parameters=effective,
            )
            total = _add_usage(total, call_usage)
            candidate = text
            if "```json" in candidate:
                candidate = candidate.split("```json", 1)[1].split("```", 1)[0]
            try:
                parsed = json.loads(candidate)
                if not isinstance(parsed, dict) or not parsed:
                    continue
                if not all(
                    isinstance(item, Mapping)
                    and item.get("is_step_correct") in {"True", "False"}
                    for item in parsed.values()
                ):
                    continue
                return parsed, text, total
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
        raise ValueError("official evaluator solution response did not match parser contract")

    def _model_call(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        parameters: Mapping[str, Any],
    ) -> tuple[str, EvaluatorUsage]:
        client = self._model_client
        if hasattr(client, "complete"):
            complete = client.complete
            signature = inspect.signature(complete)
            if "messages" in signature.parameters:
                response = complete(
                    messages=(
                        {"role": "system", "content": system_prompt},
                        {"role": "user", "content": user_prompt},
                    ),
                    parameters=dict(parameters),
                )
            else:
                response = complete(
                    prompt=_prompt_bundle(system_prompt, user_prompt),
                    parameters=dict(parameters),
                )
        elif callable(client):
            response = client(
                _prompt_bundle(system_prompt, user_prompt),
                dict(parameters),
            )
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
            cached_tokens=max(0, int(usage_value.get("cached_tokens", 0))) if isinstance(usage_value, Mapping) else 0,
            reasoning_tokens=max(0, int(usage_value.get("reasoning_tokens", 0))) if isinstance(usage_value, Mapping) else 0,
            estimated_cost=Decimal(str(usage_value.get("estimated_cost", "0"))) if isinstance(usage_value, Mapping) else Decimal(0),
        )
        return text, usage


def _parse_decision(raw: str) -> bool | None:
    match = re.search(r"Is_Answer_Correct\s*:\s*(True|False)\b", raw, flags=re.IGNORECASE)
    if match is None:
        return None
    return match.group(1).casefold() == "true"


def _prompt_bundle(system_prompt: str, user_prompt: str) -> str:
    return f"System:\n{system_prompt}\nUser:\n{user_prompt}"


def _add_usage(left: EvaluatorUsage, right: EvaluatorUsage) -> EvaluatorUsage:
    return EvaluatorUsage(
        prompt_tokens=left.prompt_tokens + right.prompt_tokens,
        completion_tokens=left.completion_tokens + right.completion_tokens,
        cached_tokens=left.cached_tokens + right.cached_tokens,
        reasoning_tokens=left.reasoning_tokens + right.reasoning_tokens,
        estimated_cost=left.estimated_cost + right.estimated_cost,
    )


def _legacy_solution_reward(steps: Mapping[str, Any]) -> float:
    decisions = [item["is_step_correct"] for item in steps.values()]
    decisions.reverse()
    reward = 0.0
    current = 0.4
    for decision in decisions[1:]:
        if decision == "True":
            reward += current
        if reward >= 1.0:
            return 1.0
        current *= 0.4
    return reward


def _normalize(value: str) -> str:
    return " ".join(value.casefold().strip().split())
