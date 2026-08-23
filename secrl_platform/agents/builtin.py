from __future__ import annotations

from secrl_platform.agents.protocol import (
    AgentManifest,
    AgentRevisionRef,
    EpisodeContext,
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
