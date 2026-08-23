import dataclasses
import unittest

from secrl_platform.auth.passwords import hash_password, verify_password
from secrl_platform.models.secrets import (
    SecretDecryptionError,
    SecretStore,
    encrypted_secret_from_json,
    encrypted_secret_to_json,
    mask_secret,
)


class SecretStoreTest(unittest.TestCase):
    def test_encrypted_envelope_can_be_persisted_without_plaintext(self):
        store = SecretStore(bytes.fromhex("11" * 32))
        encrypted = store.encrypt(
            "sk-persisted-private-value",
            secret_ref_id="secret-1",
            owner_id="owner-1",
            provider="openai-compatible",
        )

        payload = encrypted_secret_to_json(encrypted)
        restored = encrypted_secret_from_json(payload)

        self.assertNotIn("sk-persisted-private-value", payload)
        self.assertEqual(store.decrypt(restored), "sk-persisted-private-value")

    def setUp(self):
        self.store = SecretStore(bytes.fromhex("11" * 32))

    def test_ciphertext_does_not_contain_plaintext_and_round_trips(self):
        encrypted = self.store.encrypt(
            "test-private-value",
            secret_ref_id="secret-1",
            owner_id="admin-1",
            provider="openai-compatible",
        )

        self.assertNotIn(b"test-private-value", encrypted.ciphertext)
        self.assertNotIn(b"test-private-value", encrypted.tag)
        self.assertEqual(self.store.decrypt(encrypted), "test-private-value")

    def test_random_nonce_produces_distinct_ciphertext(self):
        first = self.store.encrypt("same-value")
        second = self.store.encrypt("same-value")

        self.assertNotEqual(first.nonce, second.nonce)
        self.assertNotEqual(first.ciphertext, second.ciphertext)

    def test_associated_data_tampering_is_rejected_without_secret_disclosure(self):
        encrypted = self.store.encrypt(
            "do-not-disclose",
            secret_ref_id="secret-1",
            owner_id="admin-1",
            provider="provider-1",
        )
        tampered = dataclasses.replace(encrypted, owner_id="admin-2")

        with self.assertRaises(SecretDecryptionError) as raised:
            self.store.decrypt(tampered)
        self.assertNotIn("do-not-disclose", str(raised.exception))

    def test_key_must_be_exactly_32_bytes(self):
        with self.assertRaises(ValueError):
            SecretStore(b"short")

    def test_mask_never_returns_secret_fragments(self):
        self.assertEqual(mask_secret("test-private-value"), "configured")


class PasswordTest(unittest.TestCase):
    def test_argon2_hash_verifies_without_containing_password(self):
        encoded = hash_password("correct horse battery staple")

        self.assertNotIn("correct horse battery staple", encoded)
        self.assertTrue(verify_password(encoded, "correct horse battery staple"))
        self.assertFalse(verify_password(encoded, "wrong"))

    def test_malformed_hash_is_rejected(self):
        self.assertFalse(verify_password("not-an-argon2-hash", "password"))


if __name__ == "__main__":
    unittest.main()
