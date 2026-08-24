import json
import re
import unittest
from pathlib import Path

import secrl_platform
from secrl_platform.storage.backup import PLATFORM_VERSION


ROOT = Path(__file__).resolve().parents[2]


class FinalVerificationTest(unittest.TestCase):
    def test_operator_documents_are_present(self):
        for name in (
            "installation.md",
            "agent-service-v1.md",
            "benchmark-adapter-v1.md",
            "operations.md",
            "release-v0.1.0.md",
            "security.md",
        ):
            self.assertTrue((ROOT / "docs/secrl-lite" / name).is_file(), name)

    def test_release_version_is_consistent(self):
        expected = "0.1.0"
        setup = (ROOT / "setup.py").read_text()
        api = (ROOT / "secrl_platform/api/app.py").read_text()
        web = json.loads((ROOT / "web/package.json").read_text())
        web_lock = json.loads((ROOT / "web/package-lock.json").read_text())

        self.assertEqual(getattr(secrl_platform, "__version__", None), expected)
        self.assertEqual(PLATFORM_VERSION, expected)
        self.assertIn(f'version="{expected}"', setup)
        self.assertIn("version=PLATFORM_VERSION", api)
        self.assertEqual(web["version"], expected)
        self.assertEqual(web_lock["version"], expected)
        self.assertEqual(web_lock["packages"][""]["version"], expected)

    def test_frontend_release_dependencies_are_pinned_to_audited_versions(self):
        package = json.loads((ROOT / "web/package.json").read_text())

        self.assertEqual(package["dependencies"]["react-router-dom"], "7.18.2")
        self.assertEqual(package["devDependencies"]["eslint"], "9.39.5")
        self.assertEqual(package["devDependencies"]["vite"], "6.4.3")
        self.assertEqual(package["devDependencies"]["vitest"], "3.2.7")

    def test_release_notes_cover_operator_boundaries(self):
        release_notes = ROOT / "docs/secrl-lite/release-v0.1.0.md"
        self.assertTrue(release_notes.is_file())
        notes = release_notes.read_text()
        for required in (
            "docker compose up -d",
            "linux/amd64",
            "linux/arm64",
            "Known limitations",
            "Security boundaries",
            "Backup, restore, and upgrade",
            "Protocol-Smoke",
        ):
            self.assertIn(required, notes)

    def test_source_and_compose_have_no_socket_or_secret_material(self):
        source = "\n".join(
            path.read_text(errors="ignore")
            for path in (ROOT / "secrl_platform").rglob("*.py")
        )
        compose = (ROOT / "compose.yaml").read_text()
        self.assertNotIn("/var/run/docker.sock", source + compose)
        self.assertNotRegex(source + compose, r"(?:sk-|AKIA)[A-Za-z0-9_-]{12,}")
        self.assertIn('127.0.0.1:${SECRL_PORT:-8080}:8080', compose)

    def test_browser_does_not_persist_model_keys(self):
        browser = "\n".join(path.read_text(errors="ignore") for path in (ROOT / "web/src").rglob("*.tsx"))
        self.assertNotIn("localStorage", browser)
        self.assertNotIn("sessionStorage.setItem(\"model", browser)


if __name__ == "__main__":
    unittest.main()
