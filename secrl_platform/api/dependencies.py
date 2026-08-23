from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable

from fastapi import Depends, Request
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.api.errors import ApiError
from secrl_platform.agents.service import AgentServiceTransport
from secrl_platform.auth.sessions import CSRF_HEADER, SESSION_COOKIE, SessionStore
from secrl_platform.models.secrets import SecretStore
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.orm import LocalUserORM


@dataclass(frozen=True)
class ApiContext:
    session_factory: sessionmaker[Session]
    artifact_store: LocalArtifactStore
    sessions: SessionStore
    model_provider_allowlist: tuple[str, ...]
    model_provider_resolver: Callable[[str, int], object] | None
    agent_service_allowlist: tuple[str, ...]
    agent_service_transport: AgentServiceTransport | None
    agent_service_resolver: Callable[[str, int], object] | None
    secret_store: SecretStore | None


def get_context(request: Request) -> ApiContext:
    return request.app.state.api_context


def require_user(
    request: Request,
    context: ApiContext = Depends(get_context),
) -> LocalUserORM:
    user = context.sessions.authenticate(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise ApiError(401, "AUTHENTICATION_REQUIRED", "Authentication is required")
    return user


def require_csrf_user(
    request: Request,
    context: ApiContext = Depends(get_context),
) -> LocalUserORM:
    session_id = request.cookies.get(SESSION_COOKIE)
    if context.sessions.authenticate(session_id) is None:
        raise ApiError(401, "AUTHENTICATION_REQUIRED", "Authentication is required")
    csrf_token = request.headers.get(CSRF_HEADER)
    if not csrf_token:
        raise ApiError(403, "CSRF_VALIDATION_FAILED", "CSRF validation failed")
    user = context.sessions.authenticate(session_id, csrf_token=csrf_token)
    if user is None:
        raise ApiError(403, "CSRF_VALIDATION_FAILED", "CSRF validation failed")
    return user
