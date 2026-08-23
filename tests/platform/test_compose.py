from pathlib import Path
import importlib.util
import unittest


ROOT = Path(__file__).resolve().parents[2]


def _load_release_gate_module():
    path = ROOT / "scripts" / "lite-protocol-smoke.py"
    spec = importlib.util.spec_from_file_location("lite_protocol_smoke", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class ComposePackagingTest(unittest.TestCase):
    def test_compose_keeps_web_local_and_incident_mysql_profiled(self):
        compose = (ROOT / "compose.yaml").read_text()
        self.assertIn('127.0.0.1:${SECRL_PORT:-8080}:8080', compose)
        self.assertIn('profiles: ["incident_34", "secrl-all"]', compose)
        self.assertIn('profiles: ["smoke"]', compose)
        self.assertNotIn("/var/run/docker.sock", compose)
        self.assertIn("SECRL_MYSQL_ROOT_PASSWORD:-", compose)
        self.assertIn("SECRL_AGENT_SERVICE_CAPABILITY_SECRET:-", compose)

    def test_platform_receives_documented_admin_and_capability_settings(self):
        compose = (ROOT / "compose.yaml").read_text()
        platform_environment = compose.split("x-mysql:", 1)[0]

        self.assertIn("SECRL_INITIAL_ADMIN_USERNAME:", platform_environment)
        self.assertIn("SECRL_AGENT_SERVICE_CAPABILITY_SECRET:", platform_environment)

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

    def test_every_platform_service_has_a_real_healthcheck(self):
        compose = (ROOT / "compose.yaml").read_text()
        for service, following in (
            ("web", "api"),
            ("api", "runner"),
            ("runner", "agent-service-reference"),
            ("agent-service-reference", "smoke"),
        ):
            start = compose.index(f"\n  {service}:\n")
            end = compose.index(f"\n  {following}:\n", start)
            section = compose[start:end]
            self.assertIn("healthcheck:", section, service)

    def test_web_final_image_drops_root(self):
        dockerfile = (ROOT / "docker/lite/Dockerfile").read_text()
        web_stage = dockerfile.split("FROM nginx:1.27.4-alpine AS web", 1)[1]
        self.assertIn("USER nginx", web_stage)

    def test_release_gate_workflow_covers_amd64_compose_and_multiarch(self):
        workflow = ROOT / ".github" / "workflows" / "secrl-lite-release-gate.yml"
        self.assertTrue(workflow.is_file())
        source = workflow.read_text()
        for required in (
            "runs-on: ubuntu-latest",
            "linux/amd64",
            "linux/arm64",
            "docker compose",
            "up -d --wait",
            "Protocol-Smoke",
            "backup",
            "npm --prefix web run build",
            "python -m unittest",
        ):
            self.assertIn(required, source)

    def test_protocol_smoke_does_not_invoke_secrl_only_failure_analysis(self):
        module = _load_release_gate_module()

        class Client:
            def __init__(self):
                self.paths = []

            def request(self, method, path, payload=None, headers=None):
                self.paths.append((method, path))
                return []

        client = Client()
        module._verify_protocol_analysis_boundary(client, "run-1")

        self.assertEqual(
            client.paths,
            [
                ("GET", "/api/v1/runs/run-1/analysis"),
                ("GET", "/api/v1/runs/run-1/attributions"),
                ("GET", "/api/v1/runs/run-1/audit"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
