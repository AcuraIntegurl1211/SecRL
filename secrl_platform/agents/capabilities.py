from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal, Protocol, TypeVar

import fcntl

from pydantic import BaseModel, ConfigDict, Field


class InvalidCapability(PermissionError):
    pass


class ExpiredCapability(InvalidCapability):
    pass


class CapabilityScopeError(InvalidCapability):
    pass


class CapabilityBudgetError(InvalidCapability):
    pass


class CapabilityRequestCompleted(CapabilityBudgetError):
    def __init__(self, *, actual_tokens: int, actual_cost: Decimal) -> None:
        super().__init__("capability request has already completed")
        self.actual_tokens = actual_tokens
        self.actual_cost = actual_cost


class CapabilityRequestInProgress(CapabilityBudgetError):
    pass


@dataclass(frozen=True)
class CapabilityRequestAdmission:
    status: Literal["NEW", "IN_PROGRESS", "COMPLETED"]
    claims: "CapabilityClaims"
    actual: tuple[int, Decimal] | None = None


@dataclass(frozen=True)
class CapabilityBudgetSnapshot:
    consumed_tokens: int
    consumed_cost: Decimal
    reserved_tokens: int
    reserved_cost: Decimal


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


_T = TypeVar("_T")


class CapabilityBudgetStore(Protocol):
    def transact(
        self,
        key: tuple[str, str],
        operation: Callable[["_BudgetState"], _T],
    ) -> _T: ...


class InMemoryCapabilityBudgetStore:
    """Explicitly non-durable store for unit tests and single-process fixtures."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._states: dict[tuple[str, str], _BudgetState] = {}

    def transact(
        self,
        key: tuple[str, str],
        operation: Callable[["_BudgetState"], _T],
    ) -> _T:
        with self._lock:
            return operation(self._states.setdefault(key, _BudgetState()))


class FileCapabilityBudgetStore:
    """Durable, process-safe capability ledger stored under platform data."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._local_lock = threading.Lock()
        self._recovered_keys: set[tuple[str, str]] = set()

    def transact(
        self,
        key: tuple[str, str],
        operation: Callable[["_BudgetState"], _T],
    ) -> _T:
        digest = hashlib.sha256(_canonical_json(key)).hexdigest()
        state_path = self._root / f"{digest}.json"
        lock_path = self._root / f"{digest}.lock"
        with self._local_lock:
            with lock_path.open("a+b") as lock_file:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
                try:
                    state = _read_budget_state(state_path)
                    if key not in self._recovered_keys:
                        _reconcile_orphaned_reservations(state)
                        _write_budget_state(state_path, state)
                        self._recovered_keys.add(key)
                    result = operation(state)
                    _write_budget_state(state_path, state)
                    return result
                finally:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


class CapabilitySigner:
    def __init__(
        self,
        secret: bytes,
        *,
        now: Callable[[], int | float] = time.time,
        lease_is_active: Callable[[str, str], bool] | None = None,
        budget_store: CapabilityBudgetStore | None = None,
        max_lifetime_seconds: int = 300,
        clock_skew_seconds: int = 5,
    ) -> None:
        if len(secret) < 32:
            raise ValueError("capability signing secret must contain at least 32 bytes")
        self._secret = secret
        self._now = now
        self._lease_is_active = lease_is_active
        self._max_lifetime_seconds = max_lifetime_seconds
        self._clock_skew_seconds = clock_skew_seconds
        self._budget_store = budget_store

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
        now = int(self._now())
        if claims.expires_at <= now:
            raise ExpiredCapability("capability token expired")
        if claims.issued_at > now + self._clock_skew_seconds:
            raise InvalidCapability("capability token issued in the future")
        if claims.expires_at <= claims.issued_at:
            raise InvalidCapability("capability token lifetime is invalid")
        if claims.expires_at - claims.issued_at > self._max_lifetime_seconds:
            raise InvalidCapability("capability token lifetime exceeds the maximum")
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
        request_id: str | None = None,
    ) -> CapabilityClaims:
        if additional_tokens < 0 or additional_cost < 0:
            raise CapabilityBudgetError("capability usage must not be negative")
        request_id = request_id or str(uuid.uuid4())
        claims = self.reserve_usage(
            token,
            request_id=request_id,
            reserved_tokens=additional_tokens,
            reserved_cost=additional_cost,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        )
        return self.reconcile_usage(
            token,
            request_id=request_id,
            actual_tokens=additional_tokens,
            actual_cost=additional_cost,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        )

    def reserve_usage(
        self,
        token: str,
        *,
        request_id: str,
        reserved_tokens: int,
        reserved_cost: Decimal,
        expected_run: str | None = None,
        expected_agent: str | None = None,
        model_role: str | None = None,
    ) -> CapabilityClaims:
        return self.begin_request(
            token,
            request_id=request_id,
            reserved_tokens=reserved_tokens,
            reserved_cost=reserved_cost,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        ).claims

    def begin_request(
        self,
        token: str,
        *,
        request_id: str,
        reserved_tokens: int,
        reserved_cost: Decimal,
        expected_run: str | None = None,
        expected_agent: str | None = None,
        model_role: str | None = None,
    ) -> CapabilityRequestAdmission:
        if reserved_tokens < 0 or reserved_cost < 0:
            raise CapabilityBudgetError("capability reservation must not be negative")
        claims = self.verify(
            token,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        )
        key = (claims.run_id, claims.agent_revision_id)
        reservation = (reserved_tokens, reserved_cost)

        def begin(state: _BudgetState) -> CapabilityRequestAdmission:
            completed = state.completed.get(request_id)
            if completed is not None:
                if completed.reservation != reservation:
                    raise CapabilityBudgetError("capability request ID was reused")
                return CapabilityRequestAdmission(
                    status="COMPLETED",
                    claims=claims,
                    actual=completed.actual,
                )
            existing = state.reservations.get(request_id)
            if existing is not None:
                if existing != reservation:
                    raise CapabilityBudgetError("capability request ID was reused")
                return CapabilityRequestAdmission(
                    status="IN_PROGRESS",
                    claims=claims,
                )
            reserved_token_total = sum(item[0] for item in state.reservations.values())
            reserved_cost_total = sum(
                (item[1] for item in state.reservations.values()), Decimal(0)
            )
            if state.consumed_tokens + reserved_token_total + reserved_tokens > claims.max_tokens:
                raise CapabilityBudgetError("capability token budget exceeded")
            if state.consumed_cost + reserved_cost_total + reserved_cost > claims.max_cost:
                raise CapabilityBudgetError("capability cost budget exceeded")
            state.reservations[request_id] = reservation
            return CapabilityRequestAdmission(status="NEW", claims=claims)

        return self._require_budget_store().transact(key, begin)

    def completed_usage(
        self,
        token: str,
        *,
        request_id: str,
        reserved_tokens: int,
        reserved_cost: Decimal,
        expected_run: str | None = None,
        expected_agent: str | None = None,
        model_role: str | None = None,
    ) -> tuple[int, Decimal] | None:
        claims = self.verify(
            token,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        )
        reservation = (reserved_tokens, reserved_cost)

        def read(state: _BudgetState) -> tuple[int, Decimal] | None:
            completed = state.completed.get(request_id)
            if completed is None:
                return None
            if completed.reservation != reservation:
                raise CapabilityBudgetError("capability request ID was reused")
            return completed.actual

        return self._require_budget_store().transact(
            (claims.run_id, claims.agent_revision_id),
            read,
        )

    def reconcile_usage(
        self,
        token: str,
        *,
        request_id: str,
        actual_tokens: int,
        actual_cost: Decimal,
        expected_run: str | None = None,
        expected_agent: str | None = None,
        model_role: str | None = None,
    ) -> CapabilityClaims:
        if actual_tokens < 0 or actual_cost < 0:
            raise CapabilityBudgetError("capability usage must not be negative")
        claims = self.verify(
            token,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        )
        key = (claims.run_id, claims.agent_revision_id)
        actual = (actual_tokens, actual_cost)
        def reconcile(state: _BudgetState) -> None:
            completed = state.completed.get(request_id)
            if completed is not None:
                if completed.actual != actual:
                    raise CapabilityBudgetError("capability request ID was reused")
                return
            reservation = state.reservations.get(request_id)
            if reservation is None:
                raise CapabilityBudgetError("capability usage was not reserved")
            if actual_tokens > reservation[0] or actual_cost > reservation[1]:
                raise CapabilityBudgetError("capability usage exceeded its reservation")
            del state.reservations[request_id]
            state.consumed_tokens += actual_tokens
            state.consumed_cost += actual_cost
            state.completed[request_id] = _CompletedUsage(
                reservation=reservation,
                actual=actual,
            )
        self._require_budget_store().transact(key, reconcile)
        return claims

    def cancel_reservation(
        self,
        token: str,
        *,
        request_id: str,
        expected_run: str | None = None,
        expected_agent: str | None = None,
        model_role: str | None = None,
    ) -> CapabilityClaims:
        claims = self.verify(
            token,
            expected_run=expected_run,
            expected_agent=expected_agent,
            model_role=model_role,
        )

        def cancel(state: _BudgetState) -> None:
            if request_id in state.completed:
                raise CapabilityBudgetError("completed capability usage cannot be canceled")
            state.reservations.pop(request_id, None)

        self._require_budget_store().transact(
            (claims.run_id, claims.agent_revision_id),
            cancel,
        )
        return claims

    def budget_snapshot(
        self,
        token: str,
        *,
        expected_run: str | None = None,
        expected_agent: str | None = None,
    ) -> CapabilityBudgetSnapshot:
        claims = self.verify(
            token,
            expected_run=expected_run,
            expected_agent=expected_agent,
        )

        def read(state: _BudgetState) -> CapabilityBudgetSnapshot:
            return CapabilityBudgetSnapshot(
                consumed_tokens=state.consumed_tokens,
                consumed_cost=state.consumed_cost,
                reserved_tokens=sum(
                    reservation[0] for reservation in state.reservations.values()
                ),
                reserved_cost=sum(
                    (reservation[1] for reservation in state.reservations.values()),
                    Decimal(0),
                ),
            )

        return self._require_budget_store().transact(
            (claims.run_id, claims.agent_revision_id),
            read,
        )

    def _require_budget_store(self) -> CapabilityBudgetStore:
        if self._budget_store is None:
            raise CapabilityBudgetError(
                "persistent capability budget store is required"
            )
        return self._budget_store

    def refresh(
        self,
        token: str,
        *,
        lifetime_seconds: int = 300,
    ) -> str:
        if lifetime_seconds <= 0 or lifetime_seconds > self._max_lifetime_seconds:
            raise ValueError("capability lifetime must be positive")
        claims = self.verify(token)
        if self._lease_is_active is None or not self._lease_is_active(
            claims.run_id,
            claims.agent_revision_id,
        ):
            raise CapabilityScopeError("capability refresh requires an active run lease")
        issued_at = int(self._now())
        refreshed = claims.model_copy(
            update={
                "issued_at": issued_at,
                "expires_at": issued_at + lifetime_seconds,
                "nonce": secrets.token_urlsafe(18),
            }
        )
        return self.issue(refreshed)


@dataclass
class _BudgetState:
    consumed_tokens: int = 0
    consumed_cost: Decimal = Decimal(0)
    reservations: dict[str, tuple[int, Decimal]] = field(default_factory=dict)
    completed: dict[str, "_CompletedUsage"] = field(default_factory=dict)


@dataclass(frozen=True)
class _CompletedUsage:
    reservation: tuple[int, Decimal]
    actual: tuple[int, Decimal]


def _reconcile_orphaned_reservations(state: _BudgetState) -> None:
    """Charge reservations left by a prior single-runner process conservatively."""
    for request_id, reservation in tuple(state.reservations.items()):
        state.consumed_tokens += reservation[0]
        state.consumed_cost += reservation[1]
        state.completed[request_id] = _CompletedUsage(
            reservation=reservation,
            actual=reservation,
        )
    state.reservations.clear()


def _read_budget_state(path: Path) -> _BudgetState:
    if not path.exists():
        return _BudgetState()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return _BudgetState(
            consumed_tokens=int(payload["consumed_tokens"]),
            consumed_cost=Decimal(payload["consumed_cost"]),
            reservations={
                request_id: (int(value[0]), Decimal(value[1]))
                for request_id, value in payload["reservations"].items()
            },
            completed={
                request_id: _completed_usage_from_json(value)
                for request_id, value in payload["completed"].items()
            },
        )
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise CapabilityBudgetError("capability budget ledger is corrupted") from exc


def _write_budget_state(path: Path, state: _BudgetState) -> None:
    payload = _canonical_json(
        {
            "completed": {
                request_id: {
                    "actual": [completed.actual[0], str(completed.actual[1])],
                    "reservation": [
                        completed.reservation[0],
                        str(completed.reservation[1]),
                    ],
                }
                for request_id, completed in sorted(state.completed.items())
            },
            "consumed_cost": str(state.consumed_cost),
            "consumed_tokens": state.consumed_tokens,
            "reservations": {
                request_id: [tokens, str(cost)]
                for request_id, (tokens, cost) in sorted(state.reservations.items())
            },
        }
    )
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(dir=path.parent, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            temporary.write(payload)
            temporary.flush()
            os.fsync(temporary.fileno())
        os.replace(temporary_path, path)
        temporary_path = None
        directory_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _completed_usage_from_json(value: object) -> _CompletedUsage:
    if isinstance(value, list) and len(value) == 2:
        actual = (int(value[0]), Decimal(value[1]))
        return _CompletedUsage(reservation=actual, actual=actual)
    if not isinstance(value, dict):
        raise ValueError("invalid completed capability usage")
    reservation = value["reservation"]
    actual = value["actual"]
    return _CompletedUsage(
        reservation=(int(reservation[0]), Decimal(reservation[1])),
        actual=(int(actual[0]), Decimal(actual[1])),
    )


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
