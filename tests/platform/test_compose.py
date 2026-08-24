from pathlib import Path
import importlib.util
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock
from zipfile import ZipFile


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

    def test_https_proxy_headers_reach_api_cookie_policy(self):
        compose = (ROOT / "compose.yaml").read_text()
        nginx = (ROOT / "docker" / "lite" / "nginx.conf").read_text()

        self.assertIn('FORWARDED_ALLOW_IPS: "*"', compose)
        self.assertIn("$http_x_forwarded_proto", nginx)
        self.assertNotIn("proxy_set_header X-Forwarded-Proto $scheme;", nginx)

    def test_incident_profiles_have_resource_limits(self):
        compose = (ROOT / "compose.yaml").read_text()
        mysql = compose.split("services:", 1)[0]
        self.assertIn("resources:", mysql)
        self.assertIn("memory:", mysql)

    def test_incident_mysql_retains_only_bootstrap_capabilities(self):
        compose = (ROOT / "compose.yaml").read_text()
        mysql = compose.split("services:", 1)[0]

        self.assertIn('cap_drop: ["ALL"]', mysql)
        self.assertIn(
            'cap_add: ["CHOWN", "DAC_OVERRIDE", "SETGID", "SETUID"]', mysql
        )

    def test_sourced_incident_init_does_not_mutate_entrypoint_shell_options(self):
        init = (ROOT / "docker" / "mysql" / "init-incident.sh").read_text()

        self.assertNotIn("set -u", init)
        self.assertNotIn("set -eu", init)

    def test_agent_service_is_network_isolated_from_incidents(self):
        compose = (ROOT / "compose.yaml").read_text()
        agent_start = compose.index("\n  agent-service-reference:\n")
        agent_end = compose.index("\n  smoke:\n", agent_start)
        agent = compose[agent_start:agent_end]
        runner_start = compose.index("\n  runner:\n")
        runner_end = compose.index("\n  agent-service-reference:\n", runner_start)
        runner = compose[runner_start:runner_end]
        incident_start = compose.index("\n  incident-34:\n")
        incident_end = compose.index("\n  incident-38:\n", incident_start)
        incident = compose[incident_start:incident_end]

        self.assertIn("networks:\n      - control", agent)
        self.assertNotIn("- incident", agent)
        self.assertIn(
            "networks:\n      - control\n      - incident\n      - egress", runner
        )
        mysql = compose.split("services:", 1)[0]
        self.assertIn("networks:\n    - incident", mysql)
        self.assertIn("<<: *mysql", incident)
        self.assertIn("networks:\n  control:\n    internal: true", compose)
        self.assertIn("\n  egress:\n", compose)
        self.assertIn("\n  ingress:\n", compose)

        api_start = compose.index("\n  api:\n")
        api_end = compose.index("\n  runner:\n", api_start)
        api = compose[api_start:api_end]
        self.assertIn("networks:\n      - control\n      - egress", api)
        self.assertNotIn("- egress", agent)

        web_start = compose.index("\n  web:\n")
        web_end = compose.index("\n  api:\n", web_start)
        web = compose[web_start:web_end]
        self.assertIn("networks:\n      - control\n      - ingress", web)
        self.assertNotIn("- ingress", agent)

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

    def test_installed_package_contains_frozen_secrl_dataset(self):
        setup = (ROOT / "setup.py").read_text()

        self.assertIn(
            '"secgym": ["questions/o1/test/*.json"]',
            setup,
        )

    def test_built_wheel_imports_builtin_agents_with_runtime_assets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            dist = root / "dist"
            install = root / "install"
            dist.mkdir()
            install.mkdir()
            subprocess.run(
                [sys.executable, "-m", "pip", "wheel", str(ROOT), "--no-deps", "--no-build-isolation", "-w", str(dist)],
                check=True,
                cwd=ROOT,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            wheel = next(dist.glob("*.whl"))
            with ZipFile(wheel) as archive:
                names = set(archive.namelist())
            self.assertTrue(
                any(name.startswith("secgym/agents/react_examples/") and name.endswith(".txt") for name in names)
            )
            self.assertIn("secgym/agents/expel_train/insights.json", names)
            self.assertIn("secgym/agents/expel_train/corrects.jsonl", names)
            subprocess.run(
                [sys.executable, "-m", "pip", "install", "--no-deps", "--target", str(install), str(wheel)],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            env = {key: value for key, value in os.environ.items() if key not in {"PYTHONPATH"}}
            env["PYTHONPATH"] = str(install)
            result = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    "from secgym.agents.react_agent import ReActAgent; from secrl_platform.agents.builtin import builtin_manifest; print(ReActAgent.__name__, builtin_manifest('secrl-react-v1').agent_id)",
                ],
                cwd=root,
                env=env,
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            self.assertIn("ReActAgent secrl-react-v1", result.stdout)

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

    def test_multiarch_frontend_build_runs_on_build_platform(self):
        dockerfile = (ROOT / "docker/lite/Dockerfile").read_text()

        self.assertIn(
            "FROM --platform=$BUILDPLATFORM node:22.14.0-alpine AS web-build",
            dockerfile,
        )

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

    def test_release_gate_runs_for_pr_target_push_and_manual_dispatch(self):
        workflow = ROOT / ".github" / "workflows" / "secrl-lite-release-gate.yml"
        source = workflow.read_text()
        trigger = source.split("\npermissions:\n", 1)[0]

        self.assertIn("\n  pull_request:\n", trigger)
        self.assertNotIn("pull_request_target:", trigger)
        self.assertIn("\n  workflow_dispatch:\n", trigger)
        self.assertNotIn("paths:", trigger)
        self.assertNotIn("paths-ignore:", trigger)

        pull_request = trigger.split("\n  pull_request:\n", 1)[1].split(
            "\n  push:\n", 1
        )[0]
        self.assertIn("main", pull_request)
        self.assertIn("repro/sql-retrieval-subtyping", pull_request)

        push = trigger.split("\n  push:\n", 1)[1].split(
            "\n  workflow_dispatch:\n", 1
        )[0]
        self.assertIn("main", push)
        self.assertIn("repro/sql-retrieval-subtyping", push)

        permissions = source.split("\npermissions:\n", 1)[1].split(
            "\nconcurrency:\n", 1
        )[0]
        self.assertEqual(permissions.strip(), "contents: read")

        concurrency = source.split("\nconcurrency:\n", 1)[1].split(
            "\njobs:\n", 1
        )[0]
        self.assertIn("${{ github.event_name }}", concurrency)

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

    def test_protocol_smoke_rotates_bootstrap_password_before_api_calls(self):
        module = _load_release_gate_module()

        class Client:
            def __init__(self):
                self.csrf = None
                self.calls = []

            def request(self, method, path, payload=None, headers=None):
                self.calls.append((method, path, payload))
                if path == "/api/v1/auth/login":
                    return {"csrf_token": "csrf-1", "password_change_required": True}
                if path == "/api/v1/auth/password":
                    return None
                raise AssertionError(path)

        client = Client()
        with mock.patch.dict(
            "os.environ",
            {
                "SECRL_INITIAL_ADMIN_USERNAME": "admin",
                "SECRL_INITIAL_ADMIN_PASSWORD": "bootstrap-password",
                "SECRL_TEST_ADMIN_PASSWORD": "rotated-password",
            },
            clear=False,
        ):
            module._authenticate(client)

        self.assertEqual(client.csrf, "csrf-1")
        self.assertEqual(
            client.calls,
            [
                (
                    "POST",
                    "/api/v1/auth/login",
                    {"username": "admin", "password": "bootstrap-password"},
                ),
                (
                    "POST",
                    "/api/v1/auth/password",
                    {
                        "current_password": "bootstrap-password",
                        "new_password": "rotated-password",
                    },
                ),
            ],
        )

    def test_release_gate_freezes_all_twelve_public_protocol_cases(self):
        module = _load_release_gate_module()

        class Client:
            def request(self, method, path, payload=None, headers=None):
                self.call = (method, path)
                return {
                    "total": 12,
                    "items": [{"id": f"smoke-{index:03d}"} for index in range(1, 13)],
                }

        client = Client()
        case_ids = module._protocol_case_ids(client)

        self.assertEqual(client.call, ("GET", "/api/v1/benchmarks/protocol-smoke/cases?limit=100"))
        self.assertEqual(case_ids, tuple(f"smoke-{index:03d}" for index in range(1, 13)))

    def test_release_gate_hashes_only_semantic_case_trajectory(self):
        module = _load_release_gate_module()

        class Client:
            def request(self, method, path, payload=None, headers=None):
                step = int(path.rsplit("=", 1)[1])
                return {
                    "case_id": "smoke-001",
                    "attempt_id": "runtime-specific",
                    "artifact_sha256": "runtime-specific",
                    "step": step,
                    "total_steps": 2,
                    "exchange": {"sequence": step + 1, "action": {"type": "yield"}},
                }

        expected = module.hashlib.sha256(
            module._canonical_json(
                [
                    {
                        "case_id": "smoke-001",
                        "exchange": {"sequence": 1, "action": {"type": "yield"}},
                    },
                    {
                        "case_id": "smoke-001",
                        "exchange": {"sequence": 2, "action": {"type": "yield"}},
                    },
                ]
            ).encode()
        ).hexdigest()

        self.assertEqual(
            module._semantic_trajectory_sha256(Client(), "run-specific", ("smoke-001",)),
            expected,
        )


if __name__ == "__main__":
    unittest.main()
