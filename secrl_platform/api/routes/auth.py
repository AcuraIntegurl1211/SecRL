from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from secrl_platform.api.dependencies import (
    ApiContext,
    get_context,
    require_csrf_session_user,
)
from secrl_platform.api.errors import ApiError
from secrl_platform.auth.passwords import hash_password, verify_password
from secrl_platform.auth.sessions import SESSION_COOKIE, password_change_key
from secrl_platform.storage.orm import AppSettingORM, LocalUserORM


router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=None)
async def login(
    request: Request,
    response: Response,
    context: ApiContext = Depends(get_context),
) -> dict:
    try:
        payload = await request.json()
        username = payload["username"]
        password = payload["password"]
        if not isinstance(username, str) or not isinstance(password, str):
            raise TypeError
        if not username or not password or len(username) > 128 or len(password) > 1024:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApiError(422, "INVALID_REQUEST", "Request validation failed") from None
    with context.session_factory() as session:
        user = session.scalar(
            select(LocalUserORM).where(LocalUserORM.username == username)
        )
        password_hash = user.password_hash if user is not None else "invalid"
        if (
            user is None
            or user.status != "ACTIVE"
            or not verify_password(password_hash, password)
        ):
            raise ApiError(401, "INVALID_CREDENTIALS", "Invalid username or password")
        grant = context.sessions.create(user.id)
        username = user.username
        password_change_required = session.scalar(
            select(AppSettingORM.id).where(
                AppSettingORM.key == password_change_key(user.id)
            )
        ) is not None
    response.set_cookie(
        SESSION_COOKIE,
        grant.session_id,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="strict",
        max_age=12 * 60 * 60,
        path="/",
    )
    return {
        "csrf_token": grant.csrf_token,
        "expires_at": grant.expires_at.isoformat(),
        "user": {"id": grant.user_id, "username": username},
        "password_change_required": password_change_required,
    }


@router.post("/password", status_code=204)
async def change_password(
    request: Request,
    user: LocalUserORM = Depends(require_csrf_session_user),
    context: ApiContext = Depends(get_context),
) -> None:
    try:
        payload = await request.json()
        current_password = payload["current_password"]
        new_password = payload["new_password"]
        if not isinstance(current_password, str) or not isinstance(new_password, str):
            raise TypeError
        if len(new_password) < 12 or len(new_password) > 1024:
            raise ValueError
        if current_password == new_password:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ApiError(422, "INVALID_PASSWORD_CHANGE", "Password change is invalid") from None
    with context.session_factory.begin() as session:
        stored = session.get(LocalUserORM, user.id)
        if stored is None or not verify_password(stored.password_hash, current_password):
            raise ApiError(401, "INVALID_CREDENTIALS", "Invalid current password")
        stored.password_hash = hash_password(new_password)
        marker = session.scalar(
            select(AppSettingORM).where(
                AppSettingORM.key == password_change_key(user.id)
            )
        )
        if marker is not None:
            session.delete(marker)


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    _user: LocalUserORM = Depends(require_csrf_session_user),
    context: ApiContext = Depends(get_context),
) -> None:
    context.sessions.delete(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
