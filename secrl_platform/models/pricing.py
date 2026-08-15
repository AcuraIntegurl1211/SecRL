from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal

from secrl_platform.models.providers import Usage


@dataclass(frozen=True)
class Pricing:
    input_per_million: Decimal | int | str | None = None
    output_per_million: Decimal | int | str | None = None
    revision: str = "pricing-v1"

    def __post_init__(self) -> None:
        for field in ("input_per_million", "output_per_million"):
            value = getattr(self, field)
            if value is not None:
                converted = Decimal(str(value))
                if converted < 0:
                    raise ValueError("pricing must not be negative")
                object.__setattr__(self, field, converted)

    def sha256(self) -> str:
        payload = json.dumps(
            {
                "input_per_million": _decimal_text(self.input_per_million),
                "output_per_million": _decimal_text(self.output_per_million),
                "revision": self.revision,
            },
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return hashlib.sha256(payload).hexdigest()

    def estimate(self, usage: Usage | None) -> Decimal | None:
        if (
            usage is None
            or self.input_per_million is None
            or self.output_per_million is None
        ):
            return None
        return (
            Decimal(usage.prompt) * self.input_per_million
            + Decimal(usage.completion) * self.output_per_million
        ) / Decimal(1_000_000)


def _decimal_text(value: Decimal | int | str | None) -> str | None:
    return None if value is None else str(value)
