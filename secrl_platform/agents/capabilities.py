from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from decimal import Decimal
from typing import Callable

from pydantic import BaseModel, ConfigDict, Field


class InvalidCapability(PermissionError):
    pass


class ExpiredCapability(InvalidCapability):
    pass


class CapabilityScopeError(InvalidCapability):
    pass


class CapabilityBudgetError(InvalidCapability):
    pass


class CapabilityClaims(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    agent_revision_id: str
    allowed_model_roles: tuple[str, ...]
    max_tokens: int = Field(ge=0)
    max_cost: Decimal = Field(ge=0)
    issued_at: int
    expires_at: int
    nonce: str


class CapabilitySigner:
    def __init__(
        self,
        secret: bytes,
        *,
        now: Callable[[], int | float] = time.time,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("capability signing secret must contain at least 32 bytes")
        self._secret = secret
        self._now = now

    def issue(self, claims: CapabilityClaims) -> str:
        payload = _canonical_json(claims.model_dump(mode="json"))
        signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        return f"{_b64encode(payload)}.{_b64encode(signature)}"

    def verify(
        self,
        token: str,
        *,
        expected_run: str | None = None,
        expected_agent: str | None = None,
        model_role: str | None = None,
    ) -> CapabilityClaims:
        try:
            payload_text, signature_text = token.split(".")
            payload = _b64decode(payload_text)
            signature = _b64decode(signature_text)
        except (ValueError, TypeError) as exc:
            raise InvalidCapability("invalid capability token") from exc
        expected_signature = hmac.new(self._secret, payload, hashlib.sha256).digest()
        if not hmac.compare_digest(signature, expected_signature):
            raise InvalidCapability("invalid capability token")
        try:
            claims = CapabilityClaims.model_validate_json(payload)
        except ValueError as exc:
            raise InvalidCapability("invalid capability token") from exc
        if claims.expires_at <= int(self._now()):
            raise ExpiredCapability("capability token expired")
        if expected_run is not None and claims.run_id != expected_run:
            raise CapabilityScopeError("capability run scope mismatch")
        if expected_agent is not None and claims.agent_revision_id != expected_agent:
            raise CapabilityScopeError("capability agent scope mismatch")
        if model_role is not None and model_role not in claims.allowed_model_roles:
            raise CapabilityScopeError("capability model role is not allowed")
        return claims

    def authorize_usage(
        self,
        token: str,
        *,
        additional_tokens: int,
        additional_cost: Decimal,
        expected_run: str | None = None,
        expected_agent: str | None = None,
        model_role: str | None = None,
    ) -> CapabilityClaims:
        if additional_tokens < 0 or additional_cost < 0:
            raise CapabilityBudgetError("capability usage must not be negative")
        claims = self.verify(
            token,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        )
        if additional_tokens > claims.max_tokens:
            raise CapabilityBudgetError("capability token budget exceeded")
        if additional_cost > claims.max_cost:
            raise CapabilityBudgetError("capability cost budget exceeded")
        return claims

    def refresh(
        self,
        token: str,
        *,
        lease_active: bool,
        lifetime_seconds: int = 300,
    ) -> str:
        if not lease_active:
            raise CapabilityScopeError("capability refresh requires an active run lease")
        if lifetime_seconds <= 0:
            raise ValueError("capability lifetime must be positive")
        claims = self.verify(token)
        issued_at = int(self._now())
        refreshed = claims.model_copy(
            update={
                "issued_at": issued_at,
                "expires_at": issued_at + lifetime_seconds,
                "nonce": secrets.token_urlsafe(18),
            }
        )
        return self.issue(refreshed)


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not value or any(character.isspace() for character in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded
