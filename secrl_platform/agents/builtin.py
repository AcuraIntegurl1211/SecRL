from __future__ import annotations

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from secrl_platform.agents.protocol import (
    AgentAction,
    AgentManifest,
    AgentRevisionRef,
    EpisodeContext,
    AgentRuntime,
    UsageSnapshot,
)
from secrl_platform.agents.registry import DETERMINISTIC_SMOKE_REVISION_ID
from secrl_platform.benchmarks.protocol import (
    AgentAction,
    Observation,
    SubmitAction,
    ToolCallAction,
    YieldAction,
)


_QUERY_BY_CASE = {
    "smoke-001": "alpha",
    "smoke-002": "cafe",
    "smoke-003": "gamma",
    "smoke-004": "absent",
    "smoke-005": "long",
    "smoke-006": "epsilon",
    "smoke-007": "zeta",
    "smoke-008": "eta",
    "smoke-009": "theta",
    "smoke-010": "kappa",
    "smoke-011": "lambda",
    "smoke-012": "mu",
}


BUILTIN_AGENT_IDS = (
    "secrl-baseline-v1",
    "secrl-expel-v1",
    "secrl-mas-v1",
    "secrl-prompt-sauce-v1",
    "secrl-prompt-sauce-reflexion-v1",
    "secrl-react-v1",
    "secrl-react-reflexion-v1",
)


class InvalidLegacyAction(ValueError):
    """Raised when a legacy agent does not produce exactly one action."""


def normalize_legacy_action(payload: Any) -> AgentAction:
    """Convert legacy ``execute[...]``/``submit[...]`` output to AgentAction."""
    submit_hint = False
    tuple_result = False
    if isinstance(payload, tuple) and len(payload) == 2:
        payload, submit_hint = payload
        tuple_result = True
    if isinstance(payload, (ToolCallAction, SubmitAction, YieldAction)):
        return payload
    if not isinstance(payload, str):
        raise InvalidLegacyAction("legacy action must be a string or (string, submit) tuple")
    raw = payload.strip()
    if submit_hint:
        if not raw:
            raise InvalidLegacyAction("submitted answer cannot be empty")
        return SubmitAction(type="submit", answer=raw)
    match = re.fullmatch(r"(execute|submit|yield)\[(.*)\]", raw, flags=re.IGNORECASE | re.DOTALL)
    if match is None:
        if tuple_result and raw:
            return ToolCallAction(type="tool_call", tool="sql_query", arguments={"query": raw})
        raise InvalidLegacyAction("legacy action must contain exactly one execute[], submit[], or yield[]")
    kind, body = match.group(1).lower(), match.group(2).strip()
    if not body or re.search(r"(?:execute|submit|yield)\s*\[", body, flags=re.IGNORECASE):
        raise InvalidLegacyAction("legacy action contains multiple or empty actions")
    if kind == "execute":
        return ToolCallAction(type="tool_call", tool="sql_query", arguments={"query": body})
    if kind == "submit":
        return SubmitAction(type="submit", answer=body)
    return YieldAction(type="yield", reason=body)


@dataclass(frozen=True)
class BuiltinAgentSpec:
    agent_id: str
    name: str
    factory: Callable[[Mapping[str, Any]], Any]


def _safe_config(parameters: Mapping[str, Any]) -> dict[str, Any]:
    forbidden = re.compile(r"(?:api[_-]?key|secret|password|credential|access[_-]?token|auth[_-]?token|bearer)", re.IGNORECASE)
    def contains_forbidden(value: Any) -> bool:
        if isinstance(value, Mapping):
            return any(forbidden.search(str(key)) or contains_forbidden(item) for key, item in value.items())
        if isinstance(value, (list, tuple)):
            return any(contains_forbidden(item) for item in value)
        return False

    if contains_forbidden(parameters):
        raise ValueError("agent parameters may not contain credentials")
    return dict(parameters)


def _construct_baseline(parameters: Mapping[str, Any]) -> Any:
    from secgym.agents.baseline_agent import BaselineAgent

    return BaselineAgent(**_safe_config(parameters))


def _construct_expel(parameters: Mapping[str, Any]) -> Any:
    from secgym.agents.expel_agent import ExpelAgent

    return ExpelAgent(**_safe_config(parameters))


def _construct_mas(parameters: Mapping[str, Any]) -> Any:
    from secgym.agents.maset_slave_agent import MultiModelBaselineAgent

    return MultiModelBaselineAgent(**_safe_config(parameters))


def _construct_prompt_sauce(parameters: Mapping[str, Any]) -> Any:
    from secgym.agents.prompt_sauce_agent import PromptSauceAgent

    return PromptSauceAgent(**_safe_config(parameters))


def _construct_prompt_sauce_reflexion(parameters: Mapping[str, Any]) -> Any:
    from secgym.agents.prompt_sauce_reflexion_agent import PromptSauceReflexionAgent

    return PromptSauceReflexionAgent(**_safe_config(parameters))


def _construct_react(parameters: Mapping[str, Any]) -> Any:
    from secgym.agents.react_agent import ReActAgent

    return ReActAgent(**_safe_config(parameters))


def _construct_react_reflexion(parameters: Mapping[str, Any]) -> Any:
    from secgym.agents.react_reflexion_agent import ReActReflexionAgent

    return ReActReflexionAgent(**_safe_config(parameters))


BUILTIN_AGENT_SPECS = (
    BuiltinAgentSpec("secrl-baseline-v1", "BaselineAgent", _construct_baseline),
    BuiltinAgentSpec("secrl-expel-v1", "ExpelAgent", _construct_expel),
    BuiltinAgentSpec("secrl-mas-v1", "MultiModelBaselineAgent", _construct_mas),
    BuiltinAgentSpec("secrl-prompt-sauce-v1", "PromptSauceAgent", _construct_prompt_sauce),
    BuiltinAgentSpec("secrl-prompt-sauce-reflexion-v1", "PromptSauceReflexionAgent", _construct_prompt_sauce_reflexion),
    BuiltinAgentSpec("secrl-react-v1", "ReActAgent", _construct_react),
    BuiltinAgentSpec("secrl-react-reflexion-v1", "ReActReflexionAgent", _construct_react_reflexion),
)
_BUILTIN_SPEC_BY_ID = {spec.agent_id: spec for spec in BUILTIN_AGENT_SPECS}


def approved_builtin_spec(agent_id: str) -> BuiltinAgentSpec:
    try:
        return _BUILTIN_SPEC_BY_ID[agent_id]
    except KeyError as exc:
        raise KeyError(f"unapproved built-in agent: {agent_id}") from exc


def create_approved_builtin(
    agent_id: str,
    parameters: Mapping[str, Any],
    *,
    model_client: Any | None = None,
) -> "BuiltinAgentAdapter":
    spec = approved_builtin_spec(agent_id)
    legacy = spec.factory(parameters)
    return BuiltinAgentAdapter(legacy, model_client=model_client, manifest=builtin_manifest(agent_id))


def builtin_manifest(agent_id: str) -> AgentManifest:
    spec = approved_builtin_spec(agent_id)
    return AgentManifest(
        agent_id=agent_id,
        name=spec.name,
        version="1.0.0",
        runtime="built_in",
        parameter_schema={"type": "object", "additionalProperties": False},
    )


class BuiltinAgentAdapter(AgentRuntime):
    """Async Agent Runtime facade around one approved synchronous SecGym agent."""

    def __init__(
        self,
        legacy_agent: Any,
        *,
        model_client: Any | None = None,
        manifest: AgentManifest | None = None,
    ) -> None:
        self.legacy_agent = legacy_agent
        self._model_client = model_client
        self._manifest = manifest or AgentManifest(
            agent_id="secrl-legacy",
            name=str(getattr(legacy_agent, "name", "SecRL legacy agent")),
            version="1.0.0",
            runtime="built_in",
        )
        self._episode: EpisodeContext | None = None
        self._closed = False
        if model_client is not None:
            for attr in ("client", "master_client", "slave_client"):
                if hasattr(legacy_agent, attr):
                    setattr(legacy_agent, attr, model_client)

    @property
    def model_access(self) -> str:
        return "platform_gateway"

    @property
    def model_gateway_binding(self) -> None:
        return None

    @property
    def name(self) -> str:
        return self._manifest.name

    async def reset(self, episode: EpisodeContext) -> None:
        self._episode = episode
        self._closed = False
        public = episode.public_input
        question_dict = {
            "context": public.get("context", ""),
            "question": public.get("question", ""),
        }
        reset = getattr(self.legacy_agent, "reset", None)
        if reset is None:
            return
        try:
            result = reset(change_seed=False, question_dict=question_dict)
        except TypeError:
            try:
                result = reset(change_seed=False)
            except TypeError:
                result = reset()
        if inspect.isawaitable(result):
            await result

    async def act(self, observation: Any) -> AgentAction:
        if self._episode is None or self._closed:
            raise RuntimeError("agent runtime has no active episode")
        payload = observation.content if hasattr(observation, "content") else observation
        text = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        result = self.legacy_agent.act(text)
        if inspect.isawaitable(result):
            result = await result
        return normalize_legacy_action(result)

    def usage(self) -> UsageSnapshot:
        value: Any = None
        method = getattr(self.legacy_agent, "usage", None)
        if callable(method):
            value = method()
        if value is None:
            value = getattr(self.legacy_agent, "totoal_usage", {})
        if isinstance(value, UsageSnapshot):
            return value
        if not isinstance(value, Mapping):
            return UsageSnapshot()
        # Legacy clients use either a flat usage dictionary or one per model.
        flat: dict[str, int] = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0}
        values = value.values() if all(isinstance(item, Mapping) for item in value.values()) else (value,)
        for item in values:
            for key in flat:
                raw = item.get(key, 0)
                if isinstance(raw, (int, float)) and raw >= 0:
                    flat[key] += int(raw)
        return UsageSnapshot(**flat)

    async def close(self) -> None:
        close = getattr(self.legacy_agent, "close", None)
        if callable(close):
            result = close()
            if inspect.isawaitable(result):
                await result
        self._closed = True
        self._episode = None


class DeterministicSmokeAgent:
    def __init__(self) -> None:
        self._episode: EpisodeContext | None = None
        self._step = 0
        self._matches: tuple[str, ...] = ()

    @property
    def model_access(self) -> str:
        return "none"

    @property
    def model_gateway_binding(self) -> None:
        return None

    @property
    def name(self) -> str:
        return "Deterministic Protocol-Smoke Agent"

    @classmethod
    def revision(cls) -> AgentRevisionRef:
        manifest = AgentManifest(
            agent_id="deterministic-smoke",
            name="Deterministic Protocol-Smoke Agent",
            version="1.0.0",
            runtime="built_in",
            parameter_schema={
                "type": "object",
                "additionalProperties": False,
                "properties": {},
            },
        )
        return AgentRevisionRef(
            id=DETERMINISTIC_SMOKE_REVISION_ID,
            manifest=manifest,
            manifest_sha256=manifest.sha256(),
        )

    async def reset(self, episode: EpisodeContext) -> None:
        self._episode = episode
        self._step = 0
        self._matches = ()

    async def act(self, observation: Observation) -> AgentAction:
        episode = self._require_episode()
        if self._step == 0:
            self._step = 1
            return ToolCallAction(
                type="tool_call",
                tool="search",
                arguments={"query": _QUERY_BY_CASE.get(episode.case_id, episode.case_id)},
            )
        if self._step == 1:
            self._step = 2
            matches = observation.content.get("matches", [])
            self._matches = tuple(value for value in matches if isinstance(value, str))
            if not self._matches:
                return SubmitAction(type="submit", answer="none")
            return ToolCallAction(
                type="tool_call",
                tool="read",
                arguments={"id": self._matches[0]},
            )
        if self._step == 2:
            self._step = 3
            if episode.case_id == "smoke-003":
                answer = ",".join(self._matches)
            else:
                text = observation.content.get("text", "")
                answer = text.partition("=")[2].strip() if "=" in text else str(text).strip()
            return SubmitAction(type="submit", answer=answer)
        return YieldAction(type="yield", reason="episode already submitted")

    def usage(self) -> UsageSnapshot:
        return UsageSnapshot()

    async def close(self) -> None:
        self._episode = None
        self._step = 0
        self._matches = ()

    def _require_episode(self) -> EpisodeContext:
        if self._episode is None:
            raise RuntimeError("agent runtime has no active episode")
        return self._episode
