from __future__ import annotations

from collections.abc import Callable, Iterable

from secrl_platform.agents.protocol import AgentRevisionRef, AgentRuntime


DETERMINISTIC_SMOKE_REVISION_ID = "builtin-deterministic-smoke-v1"


class DuplicateAgentError(ValueError):
    pass


class UnknownAgentError(LookupError):
    pass


class UnapprovedAgentError(PermissionError):
    pass


class AgentRegistry:
    def __init__(
        self,
        approved_revision_ids: Iterable[str] | None = None,
    ) -> None:
        self._approved_revision_ids = frozenset(
            approved_revision_ids
            if approved_revision_ids is not None
            else {DETERMINISTIC_SMOKE_REVISION_ID}
        )
        self._registrations: dict[
            str, tuple[AgentRevisionRef, Callable[[], AgentRuntime]]
        ] = {}

    def register(
        self,
        revision: AgentRevisionRef,
        factory: Callable[[], AgentRuntime],
    ) -> None:
        if revision.id in self._registrations:
            raise DuplicateAgentError(revision.id)
        self._registrations[revision.id] = (revision, factory)

    def resolve(self, revision_id: str) -> AgentRuntime:
        try:
            _revision, factory = self._registrations[revision_id]
        except KeyError as exc:
            raise UnknownAgentError(revision_id) from exc
        if revision_id not in self._approved_revision_ids:
            raise UnapprovedAgentError(revision_id)
        return factory()
