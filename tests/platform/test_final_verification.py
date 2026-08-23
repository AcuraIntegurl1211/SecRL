import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


class FinalVerificationTest(unittest.TestCase):
    def test_operator_documents_are_present(self):
        for name in (
            "installation.md",
            "agent-service-v1.md",
            "benchmark-adapter-v1.md",
            "operations.md",
            "security.md",
        ):
            self.assertTrue((ROOT / "docs/secrl-lite" / name).is_file(), name)

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
