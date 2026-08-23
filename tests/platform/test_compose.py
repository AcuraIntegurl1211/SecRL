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
        self.assertIn("SECRL_MYSQL_ROOT_PASSWORD:-", compose)
        self.assertIn("SECRL_AGENT_SERVICE_CAPABILITY_SECRET:-", compose)

    def test_entrypoint_requires_secrets_and_forwards_shutdown(self):
        entrypoint = (ROOT / "docker/lite/entrypoint.sh").read_text()
        self.assertIn("SECRL_MASTER_KEY", entrypoint)
        self.assertIn("alembic upgrade head", entrypoint)
        self.assertIn("trap", entrypoint)

    def test_environment_example_contains_names_only(self):
        env = (ROOT / ".env.example").read_text()
        self.assertIn("SECRL_INITIAL_ADMIN_PASSWORD=", env)
        self.assertNotRegex(env, r"(?:sk-|AKIA)[A-Za-z0-9_-]{12,}")

    def test_dockerfiles_install_hashed_dependencies_before_local_package(self):
        for relative in (
            "docker/lite/Dockerfile",
            "docker/agent-service-reference/Dockerfile",
        ):
            dockerfile = (ROOT / relative).read_text()
            self.assertNotIn("-r requirements-platform.txt .", dockerfile, relative)
            dependency_install = dockerfile.index(
                "RUN pip install --no-cache-dir -r requirements-platform.txt"
            )
            source_copy = dockerfile.index("COPY secrl_platform/")
            package_install = dockerfile.index(
                "RUN pip install --no-cache-dir --no-deps ."
            )
            self.assertLess(dependency_install, source_copy, relative)
            self.assertLess(source_copy, package_install, relative)


if __name__ == "__main__":
    unittest.main()
