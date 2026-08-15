from __future__ import annotations

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
