from __future__ import annotations

from fastapi import APIRouter, Depends, Request, Response
from sqlalchemy import select

from secrl_platform.api.dependencies import ApiContext, get_context, require_csrf_user
from secrl_platform.api.errors import ApiError
from secrl_platform.auth.passwords import verify_password
from secrl_platform.auth.sessions import SESSION_COOKIE
from secrl_platform.storage.orm import LocalUserORM


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
    }


@router.post("/logout", status_code=204)
def logout(
    request: Request,
    response: Response,
    _user: LocalUserORM = Depends(require_csrf_user),
    context: ApiContext = Depends(get_context),
) -> None:
    context.sessions.delete(request.cookies.get(SESSION_COOKIE))
    response.delete_cookie(SESSION_COOKIE, path="/")
