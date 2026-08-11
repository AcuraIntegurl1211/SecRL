import tempfile
import unittest
from pathlib import Path

from pydantic import ValidationError
from secrl_platform.cli import build_parser
from secrl_platform.config import Settings


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


if __name__ == "__main__":
    unittest.main()
