import tempfile
import unittest
from decimal import Decimal
from pathlib import Path

from pydantic import ValidationError
from secrl_platform.agents.capabilities import CapabilityClaims
from secrl_platform.cli import build_parser
from secrl_platform.config import Settings
from secrl_platform.runner.process import capability_signer


class PlatformCliTest(unittest.TestCase):
    def test_serve_command_is_registered(self):
        args = build_parser().parse_args(["serve", "--host", "0.0.0.0"])
        self.assertEqual(args.command, "serve")
        self.assertEqual(args.host, "0.0.0.0")


class SettingsTest(unittest.TestCase):
    def test_data_paths_are_derived_from_data_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                master_key="a" * 64,
                session_secret="s" * 32,
            )
            self.assertEqual(
                settings.database_path,
                Path(tmp) / "secrl-lite.sqlite3",
            )
            self.assertEqual(settings.artifact_dir, Path(tmp) / "artifacts")

    def test_master_key_must_be_32_byte_hex(self):
        for invalid_key in ("short", "a" * 62 + "  ", "g" * 64):
            with self.subTest(invalid_key=invalid_key):
                with self.assertRaises(ValidationError):
                    Settings(master_key=invalid_key, session_secret="s" * 32)

    def test_explicit_agent_service_secret_signs_runner_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            settings = Settings(
                data_dir=Path(tmp),
                master_key="a" * 64,
                session_secret="s" * 32,
                agent_service_capability_secret="42" * 32,
            )
            claims = CapabilityClaims(
                run_id="run-1",
                agent_revision_id="agent-1",
                allowed_model_roles=("agent",),
                max_tokens=1,
                max_cost=Decimal("0"),
                issued_at=1,
                expires_at=2,
                nonce="n",
            )

            token = capability_signer(settings).issue(claims)

            from secrl_platform.agents.capabilities import CapabilitySigner

            verified = CapabilitySigner(
                bytes.fromhex("42" * 32), now=lambda: 1
            ).verify(token)
            self.assertEqual(verified.run_id, "run-1")

    def test_agent_service_secret_must_be_32_byte_hex(self):
        for invalid_secret in ("short", "g" * 64):
            with self.subTest(invalid_secret=invalid_secret):
                with self.assertRaises(ValidationError):
                    Settings(
                        master_key="a" * 64,
                        session_secret="s" * 32,
                        agent_service_capability_secret=invalid_secret,
                    )


if __name__ == "__main__":
    unittest.main()
