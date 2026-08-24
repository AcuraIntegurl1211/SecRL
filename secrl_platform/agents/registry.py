from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Literal

from secrl_platform.agents.protocol import AgentRevisionRef, AgentRuntime


DETERMINISTIC_SMOKE_REVISION_ID = "builtin-deterministic-smoke-v1"


class DuplicateAgentError(ValueError):
    pass


class UnknownAgentError(LookupError):
    pass


class UnapprovedAgentError(PermissionError):
    pass


@dataclass(frozen=True)
class ApprovedAgentRevision:
    revision_id: str
    manifest_sha256: str
    runtime: Literal["built_in", "service"]
    factory: Callable[[], AgentRuntime]

    @classmethod
    def from_trusted_factory(
        cls,
        revision: AgentRevisionRef,
        factory: Callable[[], AgentRuntime],
    ) -> "ApprovedAgentRevision":
        return cls(
            revision_id=revision.id,
            manifest_sha256=revision.manifest_sha256,
            runtime=revision.manifest.runtime,
            factory=factory,
        )


class AgentRegistry:
    def __init__(
        self,
        approved_revisions: Iterable[ApprovedAgentRevision] = (),
    ) -> None:
        approvals = tuple(approved_revisions)
        self._approved_revisions = {approval.revision_id: approval for approval in approvals}
        if len(self._approved_revisions) != len(approvals):
            raise ValueError("approved agent revision IDs must be unique")
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
            revision, factory = self._registrations[revision_id]
        except KeyError as exc:
            raise UnknownAgentError(revision_id) from exc
        approval = self._approved_revisions.get(revision_id)
        if (
            approval is None
            or revision.manifest_sha256 != approval.manifest_sha256
            or revision.manifest.runtime != approval.runtime
            or factory is not approval.factory
        ):
            raise UnapprovedAgentError(revision_id)
        return factory()
