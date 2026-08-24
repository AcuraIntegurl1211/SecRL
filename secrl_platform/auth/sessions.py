from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from secrl_platform.storage.orm import AppSettingORM, LocalUserORM
from secrl_platform.storage.repositories import canonical_json


SESSION_COOKIE = "secrl_session"
CSRF_HEADER = "X-CSRF-Token"
PASSWORD_CHANGE_KEY_PREFIX = "auth.password_change_required."


def password_change_key(user_id: str) -> str:
    return f"{PASSWORD_CHANGE_KEY_PREFIX}{user_id}"


@dataclass(frozen=True)
class SessionGrant:
    session_id: str
    csrf_token: str
    expires_at: datetime
    user_id: str


class SessionStore:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        *,
        now: Callable[[], datetime] | None = None,
        ttl: timedelta = timedelta(hours=12),
    ) -> None:
        self._session_factory = session_factory
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._ttl = ttl

    def create(self, user_id: str) -> SessionGrant:
        session_id = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        expires_at = self._utc_now() + self._ttl
        record = AppSettingORM(
            key=_session_key(session_id),
            value_json=canonical_json(
                {
                    "csrf_sha256": _digest(csrf_token),
                    "expires_at": expires_at.isoformat(),
                    "user_id": user_id,
                }
            ),
        )
        with self._session_factory.begin() as session:
            session.add(record)
        return SessionGrant(
            session_id=session_id,
            csrf_token=csrf_token,
            expires_at=expires_at,
            user_id=user_id,
        )

    def authenticate(
        self,
        session_id: str | None,
        *,
        csrf_token: str | None = None,
    ) -> LocalUserORM | None:
        if not session_id:
            return None
        key = _session_key(session_id)
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(AppSettingORM).where(AppSettingORM.key == key)
            )
            if record is None:
                return None
            try:
                payload = json.loads(record.value_json)
                expires_at = datetime.fromisoformat(payload["expires_at"])
                csrf_sha256 = str(payload["csrf_sha256"])
                user_id = str(payload["user_id"])
            except (KeyError, TypeError, ValueError):
                session.delete(record)
                return None
            if expires_at <= self._utc_now():
                session.delete(record)
                return None
            if csrf_token is not None and not secrets.compare_digest(
                csrf_sha256,
                _digest(csrf_token),
            ):
                return None
            user = session.get(LocalUserORM, user_id)
            if user is None or user.status != "ACTIVE":
                return None
            return user

    def delete(self, session_id: str | None) -> None:
        if not session_id:
            return
        with self._session_factory.begin() as session:
            record = session.scalar(
                select(AppSettingORM).where(
                    AppSettingORM.key == _session_key(session_id)
                )
            )
            if record is not None:
                session.delete(record)

    def _utc_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise ValueError("session clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _session_key(session_id: str) -> str:
    return f"api.session.{_digest(session_id)}"
