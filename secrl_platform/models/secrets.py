from __future__ import annotations

import base64
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class SecretDecryptionError(ValueError):
    pass


@dataclass(frozen=True)
class EncryptedSecret:
    secret_ref_id: str
    owner_id: str
    provider: str
    key_version: int
    nonce: bytes
    ciphertext: bytes
    tag: bytes
    created_at: datetime
    status: str = "ACTIVE"


class SecretStore:
    def __init__(self, master_key: bytes) -> None:
        if len(master_key) != 32:
            raise ValueError("master key must contain exactly 32 bytes")
        self._cipher = AESGCM(master_key)

    def encrypt(
        self,
        value: str,
        *,
        secret_ref_id: str | None = None,
        owner_id: str = "local-admin",
        provider: str = "generic",
        key_version: int = 1,
    ) -> EncryptedSecret:
        if not value:
            raise ValueError("secret value must not be empty")
        if key_version < 1:
            raise ValueError("key version must be positive")
        reference = secret_ref_id or str(uuid.uuid4())
        nonce = os.urandom(12)
        associated_data = _associated_data(reference, owner_id, provider, key_version)
        encrypted = self._cipher.encrypt(nonce, value.encode("utf-8"), associated_data)
        return EncryptedSecret(
            secret_ref_id=reference,
            owner_id=owner_id,
            provider=provider,
            key_version=key_version,
            nonce=nonce,
            ciphertext=encrypted[:-16],
            tag=encrypted[-16:],
            created_at=datetime.now(timezone.utc),
        )

    def decrypt(self, secret: EncryptedSecret) -> str:
        associated_data = _associated_data(
            secret.secret_ref_id,
            secret.owner_id,
            secret.provider,
            secret.key_version,
        )
        try:
            plaintext = self._cipher.decrypt(
                secret.nonce,
                secret.ciphertext + secret.tag,
                associated_data,
            )
            return plaintext.decode("utf-8")
        except (InvalidTag, UnicodeDecodeError) as exc:
            raise SecretDecryptionError("secret envelope authentication failed") from exc


def mask_secret(_value: object) -> str:
    return "configured"


def encrypted_secret_to_json(secret: EncryptedSecret) -> str:
    """Serialize ciphertext metadata only; plaintext is never accepted here."""
    return json.dumps(
        {
            "ciphertext": _b64encode(secret.ciphertext),
            "created_at": secret.created_at.isoformat(),
            "key_version": secret.key_version,
            "nonce": _b64encode(secret.nonce),
            "owner_id": secret.owner_id,
            "provider": secret.provider,
            "secret_ref_id": secret.secret_ref_id,
            "status": secret.status,
            "tag": _b64encode(secret.tag),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def encrypted_secret_from_json(payload: str) -> EncryptedSecret:
    try:
        value = json.loads(payload)
        created_at = datetime.fromisoformat(value["created_at"])
        if created_at.tzinfo is None:
            raise ValueError("secret timestamp must include a timezone")
        return EncryptedSecret(
            secret_ref_id=value["secret_ref_id"],
            owner_id=value["owner_id"],
            provider=value["provider"],
            key_version=int(value["key_version"]),
            nonce=_b64decode(value["nonce"]),
            ciphertext=_b64decode(value["ciphertext"]),
            tag=_b64decode(value["tag"]),
            created_at=created_at,
            status=value["status"],
        )
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise SecretDecryptionError("secret envelope is invalid") from exc


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _b64decode(value: str) -> bytes:
    if not isinstance(value, str) or not value or any(c.isspace() for c in value):
        raise ValueError("invalid base64url")
    padding = "=" * (-len(value) % 4)
    decoded = base64.b64decode(value + padding, altchars=b"-_", validate=True)
    if _b64encode(decoded) != value:
        raise ValueError("non-canonical base64url")
    return decoded


def _associated_data(
    secret_ref_id: str,
    owner_id: str,
    provider: str,
    key_version: int,
) -> bytes:
    return json.dumps(
        {
            "key_version": key_version,
            "owner_id": owner_id,
            "provider": provider,
            "secret_ref_id": secret_ref_id,
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
