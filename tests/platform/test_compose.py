from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[2]


class ComposePackagingTest(unittest.TestCase):
    def test_compose_keeps_web_local_and_incident_mysql_profiled(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn('127.0.0.1:${SECRL_PORT:-8080}:8080', compose)
        self.assertIn('profiles: ["incident_34", "secrl-all"]', compose)
        self.assertIn('profiles: ["smoke"]', compose)
        self.assertNotIn("/var/run/docker.sock", compose)

    def test_entrypoint_requires_secrets_and_forwards_shutdown(self):
        entrypoint = (ROOT / "docker/lite/entrypoint.sh").read_text()
        self.assertIn("SECRL_MASTER_KEY", entrypoint)
        self.assertIn("alembic upgrade head", entrypoint)
        self.assertIn("trap", entrypoint)

    def test_environment_example_contains_names_only(self):
        env = (ROOT / ".env.example").read_text()
        self.assertIn("SECRL_INITIAL_ADMIN_PASSWORD=", env)
        self.assertNotRegex(env, r"(?:sk-|AKIA)[A-Za-z0-9_-]{12,}")


if __name__ == "__main__":
    unittest.main()
