from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable, Mapping

from fastapi import Depends, Request
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.api.errors import ApiError
from secrl_platform.agents.service import AgentServiceTransport
from secrl_platform.auth.sessions import (
    CSRF_HEADER,
    SESSION_COOKIE,
    SessionStore,
    password_change_key,
)
from secrl_platform.models.secrets import SecretStore
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.orm import AppSettingORM, LocalUserORM


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
    secrl_runtime_enabled: bool
    secrl_environment_probe: Callable[[tuple[str, ...]], Mapping[str, bool] | bool] | None = None
    runner_configured: bool = False
    dev_autoauth: bool = False


def get_context(request: Request) -> ApiContext:
    return request.app.state.api_context


def _dev_autoauth_user(context: ApiContext) -> LocalUserORM:
    with context.session_factory() as session:
        user = session.scalar(
            select(LocalUserORM).where(
                LocalUserORM.username == "admin",
                LocalUserORM.status == "ACTIVE",
            )
        )
    if user is None:
        raise ApiError(
            500,
            "AUTOAUTH_UNAVAILABLE",
            "Dev auto-authentication is enabled but no active local admin account exists",
        )
    return user


def require_user(
    request: Request,
    context: ApiContext = Depends(get_context),
) -> LocalUserORM:
    if context.dev_autoauth:
        user = _dev_autoauth_user(context)
    else:
        user = require_session_user(request, context)
    _require_rotated_password(context, user)
    return user


def require_session_user(
    request: Request,
    context: ApiContext = Depends(get_context),
) -> LocalUserORM:
    if context.dev_autoauth:
        return _dev_autoauth_user(context)
    user = context.sessions.authenticate(request.cookies.get(SESSION_COOKIE))
    if user is None:
        raise ApiError(401, "AUTHENTICATION_REQUIRED", "Authentication is required")
    return user


def require_csrf_user(
    request: Request,
    context: ApiContext = Depends(get_context),
) -> LocalUserORM:
    if context.dev_autoauth:
        user = _dev_autoauth_user(context)
    else:
        user = require_csrf_session_user(request, context)
    _require_rotated_password(context, user)
    return user


def require_csrf_session_user(
    request: Request,
    context: ApiContext = Depends(get_context),
) -> LocalUserORM:
    if context.dev_autoauth:
        return _dev_autoauth_user(context)
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


def _require_rotated_password(context: ApiContext, user: LocalUserORM) -> None:
    with context.session_factory() as session:
        required = session.scalar(
            select(AppSettingORM.id).where(
                AppSettingORM.key == password_change_key(user.id)
            )
        )
    if required is not None:
        raise ApiError(
            403,
            "PASSWORD_CHANGE_REQUIRED",
            "The initial administrator password must be changed",
        )
