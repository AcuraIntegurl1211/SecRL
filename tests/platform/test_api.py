import asyncio
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

from examples.agent_service.app import create_app as create_agent_service_app
from secrl_platform.api.app import _safe_exception_context, create_app
from secrl_platform.agents.builtin import DeterministicSmokeAgent
from secrl_platform.agents.builtin import builtin_manifest
from secrl_platform.agents.protocol import UsageSnapshot
from secrl_platform.benchmarks.protocol import SubmitAction
from secrl_platform.agents.service import HttpxAgentServiceTransport, manifest_sha256
from secrl_platform.auth.passwords import hash_password
from secrl_platform.auth.sessions import SessionStore
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter
from secrl_platform.config import Settings
from secrl_platform.models.secrets import encrypted_secret_from_json
from secrl_platform.models.evaluator import SecRLEvaluator
from secrl_platform.runner.process import capability_signer, run_pending_once
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from sqlalchemy import func, select, text

from secrl_platform.storage.orm import (
    AgentRevisionORM,
    AppSettingORM,
    ArtifactORM,
    AttributionORM,
    AuditEventORM,
    BenchmarkRevisionORM,
    EvaluationTaskORM,
    LocalUserORM,
    ModelConfigRevisionORM,
    RunORM,
    CaseAttemptORM,
    HumanReviewORM,
    SecretRefORM,
)


def valid_smoke_task():
    return {
        "name": "api smoke",
        "benchmark_id": "protocol-smoke",
        "agent_revision_id": "builtin-deterministic-smoke-v1",
        "case_ids": ["smoke-001"],
        "budget": {"max_cases": 1},
    }


class ManifestTransport:
    def __init__(self, manifest):
        self.manifest = manifest
        self.requests = []

    async def request(self, method, url, *, json_body=None, headers=None):
        self.requests.append((method, url, json_body, headers))
        if method == "GET" and url.endswith("/v1/manifest"):
            return self.manifest
        raise AssertionError((method, url))


class ApiTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.session_factory = create_engine_and_session(
            root / "platform.sqlite3",
            create=True,
        )
        with self.session_factory.begin() as session:
            session.add(
                LocalUserORM(
                    username="admin",
                    password_hash=hash_password("correct horse battery staple"),
                    status="ACTIVE",
                )
            )
        self.artifact_store = LocalArtifactStore(root / "artifacts")
        self.settings = Settings(
            data_dir=root,
            master_key="00" * 32,
            session_secret="s" * 32,
            model_provider_allowlist=("models.invalid",),
            secrl_runtime_enabled=True,
            secrl_mysql_password="test-only-readonly-password",
        )
        self.app = create_app(
            settings=self.settings,
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
            model_provider_resolver=lambda _host, _port: ("93.184.216.34",),
        )
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.csrf_token = None

    def tearDown(self):
        self.client.__exit__(None, None, None)
        self.directory.cleanup()

    def login(self):
        response = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "admin",
                "password": "correct horse battery staple",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.csrf_token = response.json()["csrf_token"]
        return response

    def test_internal_error_diagnostics_never_include_exception_message(self):
        marker = "sk-never-log-this-value"
        try:
            raise RuntimeError(marker)
        except RuntimeError as error:
            context = _safe_exception_context(error)

        self.assertEqual(context["exception_type"], "RuntimeError")
        self.assertTrue(any("test_api.py:" in frame for frame in context["frames"]))
        self.assertNotIn(marker, json.dumps(context))

    def test_secret_endpoint_requires_login(self):
        response = self.client.get("/api/v1/models")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["error"]["code"], "AUTHENTICATION_REQUIRED")
        self.assertRegex(
            response.headers["X-Request-ID"],
            r"^[0-9a-f-]{36}$",
        )

    def test_create_task_returns_frozen_spec_hash(self):
        self.login()

        response = self.client.post(
            "/api/v1/tasks",
            json=valid_smoke_task(),
            headers={"X-CSRF-Token": self.csrf_token},
        )

        self.assertEqual(response.status_code, 201, response.text)
        self.assertRegex(response.json()["task_spec_sha256"], r"^[0-9a-f]{64}$")
        self.assertRegex(response.json()["run_id"], r"^[0-9a-f-]{36}$")

    def test_api_created_smoke_agent_is_runner_executable_and_listed_with_run(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        agent = self.client.post(
            "/api/v1/agents",
            json={
                "kind": "BUILT_IN",
                "revision_id": "builtin-deterministic-smoke-v1",
            },
            headers=headers,
        )
        self.assertEqual(agent.status_code, 201, agent.text)
        payload = valid_smoke_task()
        payload["agent_revision_id"] = agent.json()["id"]
        payload["budget"] = {}

        task = self.client.post("/api/v1/tasks", json=payload, headers=headers)

        self.assertEqual(task.status_code, 201, task.text)
        status = asyncio.run(
            run_pending_once(
                settings=self.settings,
                session_factory=self.session_factory,
                artifact_store=self.artifact_store,
            )
        )
        self.assertEqual(status, "SUCCEEDED")
        listed = self.client.get("/api/v1/tasks")
        listed_task = next(
            item for item in listed.json() if item["id"] == task.json()["id"]
        )
        self.assertEqual(listed_task["run_id"], task.json()["run_id"])

        cases = self.client.get(f"/api/v1/runs/{task.json()['run_id']}/cases")
        self.assertEqual(cases.status_code, 200, cases.text)
        self.assertRegex(
            cases.json()[0]["trajectory_artifact"]["sha256"],
            r"^[0-9a-f]{64}$",
        )
        trajectory = self.client.get(
            f"/api/v1/runs/{task.json()['run_id']}/cases/smoke-001/trajectory",
            params={"step": 0},
        )
        self.assertEqual(trajectory.status_code, 200, trajectory.text)
        self.assertEqual(trajectory.json()["step"], 0)
        self.assertGreaterEqual(trajectory.json()["total_steps"], 1)
        self.assertIn(
            trajectory.json()["exchange"]["action"]["type"],
            {"tool_call", "submit"},
        )
        artifacts = self.client.get(
            f"/api/v1/runs/{task.json()['run_id']}/artifacts"
        )
        self.assertEqual(artifacts.status_code, 200, artifacts.text)
        self.assertEqual(artifacts.json()[0]["kind"], "trajectory")

    def test_run_attributions_reviews_and_audit_are_queryable(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        task = self.client.post(
            "/api/v1/tasks",
            json={**valid_smoke_task(), "budget": {}},
            headers=headers,
        ).json()
        self.assertEqual(
            asyncio.run(
                run_pending_once(
                    settings=self.settings,
                    session_factory=self.session_factory,
                    artifact_store=self.artifact_store,
                )
            ),
            "SUCCEEDED",
        )
        with self.session_factory.begin() as session:
            attempt = session.scalar(
                select(CaseAttemptORM).where(
                    CaseAttemptORM.run_id == task["run_id"]
                )
            )
            attribution = AttributionORM(
                case_attempt_id=attempt.id,
                taxonomy="taxonomy_v1",
                label="ANSWER",
                confidence=0.75,
                evidence_json='["trajectory:step:0"]',
            )
            session.add(attribution)
            session.flush()
            attribution_id = attribution.id

        attributions = self.client.get(
            f"/api/v1/runs/{task['run_id']}/attributions"
        )
        self.assertEqual(attributions.status_code, 200, attributions.text)
        self.assertEqual(attributions.json()[0]["id"], attribution_id)
        review = self.client.post(
            f"/api/v1/attributions/{attribution_id}/reviews",
            headers=headers,
            json={
                "primary": "ANSWER",
                "secondary": [],
                "confidence": "high",
                "evidence": ["trajectory:step:0"],
                "notes": "release gate",
            },
        )
        self.assertEqual(review.status_code, 201, review.text)
        audit = self.client.get(f"/api/v1/runs/{task['run_id']}/audit")
        self.assertEqual(audit.status_code, 200, audit.text)
        self.assertEqual(audit.json()[0]["action"], "human_review.append")

    def test_secrl_builtin_agent_and_task_are_persisted_with_frozen_limits(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        registered = self.client.post(
            "/api/v1/agents",
            json={"kind": "BUILT_IN", "revision_id": "secrl-baseline-v1"},
            headers=headers,
        )
        self.assertEqual(registered.status_code, 201, registered.text)
        model = self.client.post(
            "/api/v1/models",
            json={
                "name": "SecRL fixture model",
                "provider": "openai-compatible",
                "endpoint": "https://models.invalid/v1",
                "model": "fixture-model",
                "parameters": {"max_output_tokens": 64, "temperature": 0},
                "pricing": {"input_per_million": "1", "output_per_million": "2"},
            },
            headers={**headers, "X-Model-API-Key": "encrypted-test-key"},
        )
        self.assertEqual(model.status_code, 201, model.text)
        benchmark = self.client.get("/api/v1/benchmarks")
        self.assertEqual(benchmark.status_code, 200)
        secrl = next(
            item for item in benchmark.json()
            if item["manifest"]["benchmark_id"] == "secrl"
        )
        self.assertEqual(secrl["manifest"]["case_count"], 589)
        case_id = "incident_134:0:f85431d5ee76a2f65908ea5dc308418ff5328582d4ee45c0b73b80eaa0dd5ec7"
        created = self.client.post(
            "/api/v1/tasks",
            json={
                "name": "SecRL integration",
                "benchmark_id": "secrl",
                "agent_revision_id": registered.json()["id"],
                "model_config_revision_id": model.json()["id"],
                "case_ids": [case_id],
                "max_steps": 7,
                "max_str_len": 4096,
                "max_entry_return": 9,
                "agent_parameters": {"retry_num": 2},
                "budget": {"max_tokens": 100000, "max_cost": "100"},
            },
            headers=headers,
        )
        self.assertEqual(created.status_code, 201, created.text)
        with self.session_factory() as session:
            task = session.get(EvaluationTaskORM, created.json()["id"])
            run = session.get(RunORM, created.json()["run_id"])
            task_spec = json.loads(task.task_spec_json)
            run_spec = json.loads(run.run_spec_json)
        self.assertEqual(task_spec["dataset_sha256"], secrl["dataset"]["sha256"])
        self.assertEqual(task_spec["agent_parameters"], {"retry_num": 2})
        self.assertEqual(
            task_spec["evaluator_profile"]["model_revision"],
            model.json()["sha256"],
        )
        self.assertEqual(
            run_spec["limits"],
            {"max_steps": 7, "max_str_len": 4096, "max_entry_return": 9},
        )
        self.assertEqual(builtin_manifest("secrl-baseline-v1").sha256(), registered.json()["sha256"])

        class Runtime:
            model_access = "none"
            model_gateway_binding = None
            name = "integration-runtime"

            async def reset(self, episode):
                self.max_steps = episode.max_steps

            async def act(self, _observation):
                return SubmitAction(type="submit", answer="170.54.121.63")

            def usage(self):
                return UsageSnapshot()

            async def close(self):
                return None

        runtime = Runtime()
        status = asyncio.run(
            run_pending_once(
                settings=self.settings,
                session_factory=self.session_factory,
                artifact_store=self.artifact_store,
                model_provider_resolver=lambda _host, _port: ("93.184.216.34",),
                secrl_query_executor=lambda _scenario, _query: ([], True),
                builtin_runtime_resolver=lambda *_args: runtime,
                secrl_evaluator_resolver=lambda profile: SecRLEvaluator(
                    profile,
                    model_client=lambda _prompt, _parameters: {
                        "text": "Analysis: fixture\nIs_Answer_Correct: True",
                        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
                    },
                ),
            )
        )
        self.assertEqual(status, "SUCCEEDED")
        self.assertEqual(runtime.max_steps, 7)
        analyzed = self.client.post(
            f"/api/v1/runs/{created.json()['run_id']}:analyze",
            headers=headers,
        )
        self.assertEqual(analyzed.status_code, 200, analyzed.text)
        self.assertEqual(analyzed.json()["taxonomy_version"], "taxonomy_v1")
        self.assertEqual(analyzed.json()["artifact_visibility"], "RESTRICTED")
        history = self.client.get(
            f"/api/v1/runs/{created.json()['run_id']}/analysis"
        )
        self.assertEqual(len(history.json()), 1)
        restricted = self.client.get(
            f"/api/v1/artifacts/{analyzed.json()['manifest_artifact_id']}"
        )
        self.assertEqual(restricted.status_code, 403)
        self.assertNotIn("170.54.121.63", analyzed.text + history.text + restricted.text)

    def test_secrl_task_rejects_agent_override_of_frozen_max_steps(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        registered = self.client.post(
            "/api/v1/agents",
            json={"kind": "BUILT_IN", "revision_id": "secrl-baseline-v1"},
            headers=headers,
        ).json()
        model = self.client.post(
            "/api/v1/models",
            json={
                "name": "fixture",
                "provider": "openai-compatible",
                "endpoint": "https://models.invalid/v1",
                "model": "fixture",
                "parameters": {"max_output_tokens": 16},
                "pricing": {"input_per_million": "1", "output_per_million": "1"},
            },
            headers={**headers, "X-Model-API-Key": "encrypted-test-key"},
        ).json()
        response = self.client.post(
            "/api/v1/tasks",
            json={
                "name": "invalid override",
                "benchmark_id": "secrl",
                "agent_revision_id": registered["id"],
                "model_config_revision_id": model["id"],
                "case_ids": ["incident_134:0:f85431d5ee76a2f65908ea5dc308418ff5328582d4ee45c0b73b80eaa0dd5ec7"],
                "agent_parameters": {"max_steps": 999},
                "budget": {"max_tokens": 1000, "max_cost": "10"},
            },
            headers=headers,
        )
        self.assertEqual(response.status_code, 422, response.text)

    def test_task_budget_is_validated_and_bound_into_frozen_hash(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        invalid_specs = (
            {"max_cases": -1},
            {"max_tokens": -1},
            {"max_cost": "-0.01"},
            {"unknown_limit": 1},
        )
        for budget in invalid_specs:
            with self.subTest(budget=budget):
                payload = valid_smoke_task()
                payload["budget"] = budget
                response = self.client.post(
                    "/api/v1/tasks",
                    json=payload,
                    headers=headers,
                )
                self.assertEqual(response.status_code, 422)

        first = valid_smoke_task()
        first["budget"] = {"max_cases": 1, "max_tokens": 0, "max_cost": "0"}
        second = valid_smoke_task()
        second["budget"] = {"max_cases": 2, "max_tokens": 0, "max_cost": "0"}
        first_response = self.client.post(
            "/api/v1/tasks",
            json=first,
            headers=headers,
        )
        second_response = self.client.post(
            "/api/v1/tasks",
            json=second,
            headers=headers,
        )

        self.assertEqual(first_response.status_code, 201)
        self.assertEqual(second_response.status_code, 201)
        self.assertNotEqual(
            first_response.json()["task_spec_sha256"],
            second_response.json()["task_spec_sha256"],
        )

    def test_login_stores_only_hashes_and_sets_hardened_cookie(self):
        response = self.login()
        session_id = self.client.cookies.get("secrl_session")
        self.assertIsNotNone(session_id)

        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("Secure", cookie)
        with self.session_factory() as session:
            records = session.scalars(
                select(AppSettingORM).where(
                    AppSettingORM.key.like("api.session.%")
                )
            ).all()
        self.assertEqual(len(records), 1)
        persisted = records[0].key + records[0].value_json
        self.assertNotIn(session_id, persisted)
        self.assertNotIn(self.csrf_token, persisted)
        self.assertNotIn("correct horse battery staple", persisted)

    def test_state_change_requires_csrf_and_logout_revokes_session(self):
        self.login()

        missing = self.client.post("/api/v1/tasks", json=valid_smoke_task())
        wrong = self.client.post(
            "/api/v1/tasks",
            json=valid_smoke_task(),
            headers={"X-CSRF-Token": "wrong"},
        )
        logout = self.client.post(
            "/api/v1/auth/logout",
            headers={"X-CSRF-Token": self.csrf_token},
        )
        after_logout = self.client.get("/api/v1/models")

        self.assertEqual(missing.status_code, 403)
        self.assertEqual(wrong.status_code, 403)
        self.assertEqual(logout.status_code, 204)
        self.assertEqual(after_logout.status_code, 401)

    def test_all_errors_use_envelope_and_request_id(self):
        response = self.client.get("/api/v1/does-not-exist")

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")
        self.assertEqual(
            response.json()["error"]["request_id"],
            response.headers["X-Request-ID"],
        )

    def test_openapi_does_not_expose_secret_fields(self):
        document = json.dumps(self.app.openapi(), sort_keys=True).lower()

        for forbidden in (
            '"password"',
            '"api_key"',
            '"capability_token"',
            '"session_id"',
            '"csrf_token"',
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, document)

    def test_openapi_contains_complete_lite_route_surface(self):
        paths = set(self.app.openapi()["paths"])

        self.assertEqual(
            paths,
            {
                "/api/v1/auth/login",
                "/api/v1/auth/logout",
                "/api/v1/auth/password",
                "/api/v1/health",
                "/api/v1/models",
                "/api/v1/agents",
                "/api/v1/agents/{id}:check",
                "/api/v1/benchmarks",
                "/api/v1/benchmarks/{benchmark_id}/cases",
                "/api/v1/tasks",
                "/api/v1/runs/{id}",
                "/api/v1/runs/{id}:pause",
                "/api/v1/runs/{id}:resume",
                "/api/v1/runs/{id}:cancel",
                "/api/v1/runs/{id}/cases",
                "/api/v1/runs/{id}/cases/{case_id}/trajectory",
                "/api/v1/runs/{id}/cases/{case_id}:retry",
                "/api/v1/runs/{id}:analyze",
                "/api/v1/runs/{id}/analysis",
                "/api/v1/runs/{id}/attributions",
                "/api/v1/runs/{id}/artifacts",
                "/api/v1/runs/{id}/audit",
                "/api/v1/attributions/{id}/reviews",
                "/api/v1/artifacts/{id}/metadata",
                "/api/v1/artifacts/{id}",
                "/api/v1/compare",
            },
        )

    def test_openapi_declares_the_runtime_error_envelope(self):
        document = self.app.openapi()
        expected = {"$ref": "#/components/schemas/ErrorEnvelope"}

        self.assertIn("ErrorEnvelope", document["components"]["schemas"])
        for path, operations in document["paths"].items():
            for method, operation in operations.items():
                if method == "parameters":
                    continue
                for status, response in operation.get("responses", {}).items():
                    if int(status) >= 400:
                        with self.subTest(path=path, method=method, status=status):
                            self.assertEqual(
                                response["content"]["application/json"]["schema"],
                                expected,
                            )

    def test_openapi_snapshot_matches_runtime_exactly(self):
        snapshot_path = (
            Path(__file__).parents[1] / "fixtures" / "platform" / "openapi-v1.json"
        )

        self.assertEqual(
            json.loads(snapshot_path.read_text(encoding="utf-8")),
            self.app.openapi(),
        )

    def test_https_login_cookie_is_secure(self):
        with TestClient(self.app, base_url="https://testserver") as https_client:
            response = https_client.post(
                "/api/v1/auth/login",
                json={
                    "username": "admin",
                    "password": "correct horse battery staple",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("Secure", response.headers["set-cookie"])

    def test_expired_session_is_deleted_and_rejected(self):
        now = [datetime(2026, 8, 15, tzinfo=timezone.utc)]
        sessions = SessionStore(
            self.session_factory,
            now=lambda: now[0],
            ttl=timedelta(seconds=10),
        )
        with self.session_factory() as session:
            user_id = session.scalar(select(LocalUserORM.id))
        grant = sessions.create(user_id)
        now[0] += timedelta(seconds=11)

        self.assertIsNone(sessions.authenticate(grant.session_id))
        with self.session_factory() as session:
            self.assertIsNone(
                session.scalar(
                    select(AppSettingORM).where(
                        AppSettingORM.key.like("api.session.%")
                    )
                )
            )

    def test_initial_admin_must_rotate_password_before_using_api(self):
        with self.session_factory.begin() as session:
            user = session.scalar(select(LocalUserORM))
            session.add(
                AppSettingORM(
                    key=f"auth.password_change_required.{user.id}",
                    value_json="true",
                )
            )

        login = self.login()
        self.assertTrue(login.json()["password_change_required"])
        blocked = self.client.get("/api/v1/tasks")
        self.assertEqual(blocked.status_code, 403)
        self.assertEqual(
            blocked.json()["error"]["code"], "PASSWORD_CHANGE_REQUIRED"
        )

        changed = self.client.post(
            "/api/v1/auth/password",
            headers={"X-CSRF-Token": self.csrf_token},
            json={
                "current_password": "correct horse battery staple",
                "new_password": "new correct horse battery staple",
            },
        )
        self.assertEqual(changed.status_code, 204, changed.text)
        self.assertEqual(self.client.get("/api/v1/tasks").status_code, 200)
        self.client.post(
            "/api/v1/auth/logout", headers={"X-CSRF-Token": self.csrf_token}
        )
        old_login = self.client.post(
            "/api/v1/auth/login",
            json={"username": "admin", "password": "correct horse battery staple"},
        )
        new_login = self.client.post(
            "/api/v1/auth/login",
            json={
                "username": "admin",
                "password": "new correct horse battery staple",
            },
        )
        self.assertEqual(old_login.status_code, 401)
        self.assertEqual(new_login.status_code, 200)
        self.assertFalse(new_login.json()["password_change_required"])

    def test_model_agent_and_benchmark_resources_are_safe_and_frozen(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        leaked_value = "credential-must-never-echo"

        rejected = self.client.post(
            "/api/v1/models",
            headers=headers,
            json={
                "name": "fixture",
                "provider": "openai-compatible",
                "endpoint": "https://models.invalid/v1",
                "model": "fixture",
                "api_key": leaked_value,
            },
        )
        created_model = self.client.post(
            "/api/v1/models",
            headers=headers,
            json={
                "name": "fixture",
                "provider": "openai-compatible",
                "endpoint": "https://models.invalid/v1",
                "model": "fixture",
                "parameters": {"temperature": 0},
                "pricing": {"input_per_million": "0"},
            },
        )
        created_agent = self.client.post(
            "/api/v1/agents",
            headers=headers,
            json={"revision_id": DeterministicSmokeAgent.revision().id},
        )
        checked = self.client.post(
            f"/api/v1/agents/{created_agent.json()['id']}:check",
            headers=headers,
        )
        benchmarks = self.client.get("/api/v1/benchmarks")

        self.assertEqual(rejected.status_code, 422)
        self.assertNotIn(leaked_value, rejected.text)
        self.assertEqual(created_model.status_code, 201, created_model.text)
        self.assertFalse(created_model.json()["credential_configured"])
        self.assertEqual(created_agent.status_code, 201, created_agent.text)
        self.assertEqual(checked.json()["status"], "valid")
        self.assertEqual(len(benchmarks.json()), 2)
        self.assertNotIn("secret", json.dumps(created_model.json()).lower())

    def test_benchmark_case_browser_is_paginated_and_excludes_gold(self):
        self.login()

        response = self.client.get(
            "/api/v1/benchmarks/secrl/cases",
            params={"scenario": "incident_34", "offset": 0, "limit": 5},
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["benchmark_id"], "secrl")
        self.assertEqual(body["total"], 82)
        self.assertEqual(len(body["items"]), 5)
        for item in body["items"]:
            self.assertEqual(item["scenario_id"], "incident_34")
            self.assertIn("question", item["public_input"])
            self.assertEqual(
                set(item["public_input"]),
                {"incident", "ordinal", "context", "question", "question_sha256"},
            )
            self.assertEqual(len(item["public_input_sha256"]), 64)

        all_cases = self.client.get(
            "/api/v1/benchmarks/secrl/cases", params={"offset": 0, "limit": 1}
        ).json()
        self.assertEqual(all_cases["total"], 589)

    def test_model_key_is_encrypted_and_api_task_is_runner_executable(self):
        self.login()
        headers = {
            "X-CSRF-Token": self.csrf_token,
            "X-Model-API-Key": "sk-runtime-only-secret",
        }
        model = self.client.post(
            "/api/v1/models",
            headers=headers,
            json={
                "name": "encrypted-fixture",
                "provider": "openai-compatible",
                "endpoint": "https://models.invalid/v1",
                "model": "fixture",
                "parameters": {"max_output_tokens": 16},
                "pricing": {
                    "input_per_million": "1",
                    "output_per_million": "1",
                },
            },
        )
        task_payload = valid_smoke_task()
        task_payload["model_config_revision_id"] = model.json().get("id")
        task_payload["budget"] = {}
        task = self.client.post(
            "/api/v1/tasks",
            headers={"X-CSRF-Token": self.csrf_token},
            json=task_payload,
        )

        self.assertEqual(model.status_code, 201, model.text)
        self.assertTrue(model.json()["credential_configured"])
        self.assertNotIn("sk-runtime-only-secret", model.text)
        self.assertEqual(task.status_code, 201, task.text)
        with self.session_factory() as session:
            stored_model = session.get(ModelConfigRevisionORM, model.json()["id"])
            secret = session.get(SecretRefORM, stored_model.secret_ref_id)
            stored_task = session.get(EvaluationTaskORM, task.json()["id"])
            self.assertNotIn("sk-runtime-only-secret", secret.ciphertext)
            self.assertEqual(
                self.app.state.api_context.secret_store.decrypt(
                    encrypted_secret_from_json(secret.ciphertext)
                ),
                "sk-runtime-only-secret",
            )
            self.assertEqual(stored_task.model_config_revision_id, stored_model.id)
            self.assertEqual(
                json.loads(stored_task.task_spec_json)["model_config_sha256"],
                stored_model.sha256,
            )

        status = asyncio.run(
            run_pending_once(
                settings=self.settings,
                session_factory=self.session_factory,
                artifact_store=self.artifact_store,
            )
        )

        self.assertEqual(status, "SUCCEEDED")

    def test_agent_service_registration_check_and_task_creation(self):
        manifest = json.loads(
            (Path("examples/agent_service/manifest.json")).read_text(encoding="utf-8")
        )
        transport = ManifestTransport(manifest)
        settings = Settings(
            data_dir=Path(self.directory.name),
            master_key="00" * 32,
            session_secret="s" * 32,
            agent_service_allowlist=("agent-service-reference",),
            model_provider_allowlist=("models.invalid",),
        )
        try:
            app = create_app(
                settings=settings,
                session_factory=self.session_factory,
                artifact_store=self.artifact_store,
                agent_service_transport=transport,
                agent_service_resolver=lambda _host, _port: ("127.0.0.1",),
            )
        except TypeError as exc:
            self.fail(f"Agent Service API dependencies are unavailable: {exc}")
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "admin",
                    "password": "correct horse battery staple",
                },
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}
            created = client.post(
                "/api/v1/agents",
                headers=headers,
                json={
                    "kind": "SERVICE",
                    "revision_id": manifest["agent_revision_id"],
                    "endpoint": "http://agent-service-reference",
                    "manifest_sha256": manifest_sha256(manifest),
                },
            )
            checked = client.post(
                f"/api/v1/agents/{created.json().get('id', 'missing')}:check",
                headers=headers,
            )
            task_payload = valid_smoke_task()
            task_payload["agent_revision_id"] = created.json().get("id", "missing")
            task_payload["budget"] = {}
            task = client.post(
                "/api/v1/tasks",
                headers=headers,
                json=task_payload,
            )

        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["kind"], "SERVICE")
        self.assertEqual(checked.status_code, 200, checked.text)
        self.assertEqual(checked.json()["status"], "valid")
        self.assertEqual(task.status_code, 201, task.text)
        self.assertEqual(len(transport.requests), 2)
        with self.session_factory() as session:
            service = session.get(AgentRevisionORM, created.json()["id"])
            self.assertEqual(service.service_endpoint, "http://agent-service-reference")
            self.assertEqual(service.service_manifest_sha256, manifest_sha256(manifest))
            stored_task = session.get(EvaluationTaskORM, task.json()["id"])
            task_spec = json.loads(stored_task.task_spec_json)
            self.assertIn("agent_revision_sha256", task_spec)
            self.assertEqual(
                task_spec["agent_revision_sha256"],
                service.sha256,
            )

        async def execute_registered_service_task():
            signer = capability_signer(settings)
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(
                    app=create_agent_service_app(signer)
                ),
                base_url="http://agent-service-reference",
            ) as service_client:
                return await run_pending_once(
                    settings=settings,
                    session_factory=self.session_factory,
                    artifact_store=self.artifact_store,
                    agent_service_transport=HttpxAgentServiceTransport(service_client),
                    agent_service_resolver=lambda _host, _port: ("127.0.0.1",),
                )

        self.assertEqual(asyncio.run(execute_registered_service_task()), "SUCCEEDED")

    def test_model_responses_do_not_echo_parameter_values(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        sensitive_value = "credential-shaped-stop-value"

        created = self.client.post(
            "/api/v1/models",
            headers=headers,
            json={
                "name": "redacted-response",
                "provider": "openai-compatible",
                "endpoint": "https://models.invalid/v1",
                "model": "fixture",
                "parameters": {"stop": sensitive_value, "temperature": 0},
                "pricing": {"input_per_million": "0"},
            },
        )
        listed = self.client.get("/api/v1/models")

        self.assertEqual(created.status_code, 201)
        self.assertNotIn(sensitive_value, created.text)
        self.assertNotIn(sensitive_value, listed.text)
        self.assertEqual(created.json()["parameter_names"], ["stop", "temperature"])

    def test_model_config_rejects_nested_or_url_embedded_credentials(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        credential = "must-never-persist-or-echo"

        nested = self.client.post(
            "/api/v1/models",
            headers=headers,
            json={
                "name": "unsafe",
                "provider": "openai-compatible",
                "endpoint": "https://models.invalid/v1",
                "model": "fixture",
                "parameters": {"transport": {"api_key": credential}},
            },
        )
        url_embedded = self.client.post(
            "/api/v1/models",
            headers=headers,
            json={
                "name": "unsafe-url",
                "provider": "openai-compatible",
                "endpoint": f"https://user:{credential}@models.invalid/v1",
                "model": "fixture",
            },
        )

        self.assertEqual(nested.status_code, 422)
        self.assertEqual(url_embedded.status_code, 422)
        self.assertNotIn(credential, nested.text + url_embedded.text)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count(ModelConfigRevisionORM.id))),
                0,
            )

    def test_model_endpoint_allowlist_rejects_unapproved_host(self):
        try:
            settings = Settings(
                data_dir=Path(self.directory.name),
                master_key="00" * 32,
                session_secret="s" * 32,
                model_provider_allowlist=("models.invalid",),
            )
        except Exception as exc:
            self.fail(f"model provider allowlist setting is unavailable: {exc}")
        app = create_app(
            settings=settings,
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
        )
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "admin",
                    "password": "correct horse battery staple",
                },
            )
            headers = {"X-CSRF-Token": login.json()["csrf_token"]}
            response = client.post(
                "/api/v1/models",
                headers=headers,
                json={
                    "name": "unapproved-endpoint",
                    "provider": "openai-compatible",
                    "endpoint": "https://unapproved.example/v1",
                    "model": "fixture",
                },
            )

        self.assertEqual(response.status_code, 422)
        self.assertEqual(response.json()["error"]["code"], "INVALID_MODEL_CONFIG")

    def test_injected_session_factory_does_not_disable_model_allowlist(self):
        app = create_app(
            session_factory=self.session_factory,
            artifact_store=self.artifact_store,
        )
        with TestClient(app) as client:
            login = client.post(
                "/api/v1/auth/login",
                json={
                    "username": "admin",
                    "password": "correct horse battery staple",
                },
            )
            response = client.post(
                "/api/v1/models",
                headers={"X-CSRF-Token": login.json()["csrf_token"]},
                json={
                    "name": "not-default-allowlisted",
                    "provider": "openai-compatible",
                    "endpoint": "https://models.invalid/v1",
                    "model": "fixture",
                },
            )

        self.assertEqual(response.status_code, 422)

    def test_model_config_uses_allowlist_not_secret_key_patterns(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        credential = "alternate-secret-spelling"

        for key in ("x-api-key", "client_secret", "openai_api_key"):
            with self.subTest(key=key):
                response = self.client.post(
                    "/api/v1/models",
                    headers=headers,
                    json={
                        "name": f"unsafe-{key}",
                        "provider": "openai-compatible",
                        "endpoint": "https://models.invalid/v1",
                        "model": "fixture",
                        "parameters": {key: credential},
                    },
                )
                self.assertEqual(response.status_code, 422)
                self.assertNotIn(credential, response.text)
        with self.session_factory() as session:
            self.assertEqual(
                session.scalar(select(func.count(ModelConfigRevisionORM.id))),
                0,
            )

    def test_run_lifecycle_routes_use_runner_state_machine(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        created = self.client.post(
            "/api/v1/tasks",
            json=valid_smoke_task(),
            headers=headers,
        ).json()
        run_id = created["run_id"]
        repository = RunnerRepository(self.session_factory)
        repository.prepare_for_run(created["id"], run_id)

        pause = self.client.post(f"/api/v1/runs/{run_id}:pause", headers=headers)
        with self.session_factory.begin() as session:
            task = session.get(EvaluationTaskORM, created["id"])
            run = session.get(RunORM, run_id)
            task.status = "PAUSED"
            run.status = "QUEUED"
            run.pause_requested = False
        resume = self.client.post(f"/api/v1/runs/{run_id}:resume", headers=headers)
        cancel = self.client.post(f"/api/v1/runs/{run_id}:cancel", headers=headers)
        fetched = self.client.get(f"/api/v1/runs/{run_id}")

        self.assertEqual(pause.json()["status"], "PAUSE_REQUESTED")
        self.assertEqual(resume.json()["status"], "QUEUED")
        self.assertEqual(cancel.json()["status"], "CANCELED")
        self.assertEqual(fetched.json()["status"], "CANCELED")

    def test_artifact_download_verifies_path_and_hash(self):
        self.login()
        ref = self.artifact_store.put_bytes(
            "trajectory",
            b'{"safe":true}',
            media_type="application/json",
        )
        with self.session_factory.begin() as session:
            artifact = ArtifactORM(
                storage_key=str(ref.path.relative_to(self.artifact_store.root)),
                kind=ref.kind,
                sha256=ref.sha256,
                size_bytes=ref.size,
                ref_type="case_attempt",
                ref_id="fixture",
            )
            session.add(artifact)
            session.flush()
            artifact_id = artifact.id

        metadata = self.client.get(f"/api/v1/artifacts/{artifact_id}/metadata")
        download = self.client.get(f"/api/v1/artifacts/{artifact_id}")
        ref.path.write_bytes(b"tampered")
        tampered = self.client.get(f"/api/v1/artifacts/{artifact_id}")

        self.assertEqual(metadata.json()["sha256"], ref.sha256)
        self.assertEqual(download.content, b'{"safe":true}')
        self.assertEqual(tampered.status_code, 409)
        self.assertEqual(
            tampered.json()["error"]["code"],
            "ARTIFACT_INTEGRITY_ERROR",
        )
        self.assertNotIn(str(ref.path), tampered.text)

    def test_compare_rejects_different_benchmark_revisions(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        left = self.client.post(
            "/api/v1/tasks", json=valid_smoke_task(), headers=headers
        ).json()["id"]
        right = self.client.post(
            "/api/v1/tasks", json=valid_smoke_task(), headers=headers
        ).json()["id"]
        with self.session_factory.begin() as session:
            benchmark = BenchmarkRevisionORM(
                adapter_name="different-revision",
                manifest_json="{}",
                tool_schema_json="[]",
                evaluation_protocol_json="{}",
                sha256="f" * 64,
            )
            session.add(benchmark)
            session.flush()
            session.get(EvaluationTaskORM, right).benchmark_revision_id = benchmark.id

        response = self.client.get(
            "/api/v1/compare", params={"left": left, "right": right}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(
            response.json()["error"]["code"], "BENCHMARK_REVISION_MISMATCH"
        )

    def test_compare_returns_completed_metrics_and_marks_missing_usage(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        task_ids = []
        for name in ("left", "right"):
            created = self.client.post(
                "/api/v1/tasks",
                json={**valid_smoke_task(), "name": name, "budget": {}},
                headers=headers,
            )
            self.assertEqual(created.status_code, 201, created.text)
            task_ids.append(created.json()["id"])
            status = asyncio.run(
                run_pending_once(
                    settings=self.settings,
                    session_factory=self.session_factory,
                    artifact_store=self.artifact_store,
                )
            )
            self.assertEqual(status, "SUCCEEDED")

        response = self.client.get(
            "/api/v1/compare", params={"left": task_ids[0], "right": task_ids[1]}
        )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["revision"]["benchmark_revision_id"], body["left"]["benchmark_revision_id"])
        for side in ("left", "right"):
            metrics = body[side]["metrics"]
            self.assertEqual(metrics["case_count"], 1)
            self.assertEqual(metrics["success_count"], 1)
            self.assertEqual(metrics["success_rate"], 1.0)
            self.assertEqual(metrics["average_reward"], 1.0)
            self.assertEqual(metrics["average_steps"], 3.0)
            self.assertIsNone(metrics["tokens"])
            self.assertIsNone(metrics["estimated_cost"])
            self.assertFalse(metrics["token_cost_available"])
            self.assertGreaterEqual(metrics["duration_seconds"], 0)

    def test_compare_rejects_unfinished_tasks(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        left = self.client.post(
            "/api/v1/tasks", json=valid_smoke_task(), headers=headers
        ).json()["id"]
        right = self.client.post(
            "/api/v1/tasks", json=valid_smoke_task(), headers=headers
        ).json()["id"]

        response = self.client.get(
            "/api/v1/compare", params={"left": left, "right": right}
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["error"]["code"], "TASK_NOT_COMPLETED")

    def test_artifact_download_cannot_swap_bytes_after_verification(self):
        self.login()
        ref = self.artifact_store.put_bytes(
            "trajectory",
            b'{"verified":true}',
            media_type="application/json",
        )
        with self.session_factory.begin() as session:
            artifact = ArtifactORM(
                storage_key=str(ref.path.relative_to(self.artifact_store.root)),
                kind=ref.kind,
                sha256=ref.sha256,
                size_bytes=ref.size,
                ref_type="case_attempt",
                ref_id="toctou-fixture",
            )
            session.add(artifact)
            session.flush()
            artifact_id = artifact.id
        original_verify = self.artifact_store.verify

        def verify_then_swap(artifact_ref):
            result = original_verify(artifact_ref)
            artifact_ref.path.write_bytes(b'{"swapped":true}')
            return result

        self.artifact_store.verify = verify_then_swap

        response = self.client.get(f"/api/v1/artifacts/{artifact_id}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'{"verified":true}')

    def test_interactive_docs_and_openapi_are_not_public_routes(self):
        for path in ("/docs", "/redoc", "/openapi.json"):
            with self.subTest(path=path):
                response = self.client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertEqual(response.json()["error"]["code"], "NOT_FOUND")

    def test_default_app_applies_alembic_head_instead_of_create_all(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = Settings(
                data_dir=Path(directory),
                master_key="00" * 32,
                session_secret="s" * 32,
            )
            app = create_app(settings=settings)

            with TestClient(app) as client:
                self.assertEqual(client.get("/api/v1/health").status_code, 200)

            session_factory = create_engine_and_session(settings.database_path)
            with session_factory() as session:
                revision = session.scalar(text("SELECT version_num FROM alembic_version"))
            self.assertEqual(revision, "0004_analysis_review_persistence")

    def test_human_review_api_is_persistent_append_only_and_audited(self):
        self.login()
        headers = {"X-CSRF-Token": self.csrf_token}
        created = self.client.post(
            "/api/v1/tasks",
            json=valid_smoke_task(),
            headers=headers,
        ).json()

        asyncio.run(
            run_pending_once(
                settings=self.settings,
                session_factory=self.session_factory,
                artifact_store=self.artifact_store,
            )
        )
        with self.session_factory.begin() as session:
            attempt = session.query(CaseAttemptORM).filter_by(run_id=created["run_id"]).one()
            attribution = AttributionORM(
                case_attempt_id=attempt.id,
                taxonomy="taxonomy_v1",
                label="ANSWER",
                confidence=0.6,
                evidence_json='["automatic"]',
            )
            session.add(attribution)
            session.flush()
            attribution_id = attribution.id
        first = self.client.post(
            f"/api/v1/attributions/{attribution_id}/reviews",
            headers=headers,
            json={
                "primary": "GOLD",
                "secondary": ["ANSWER"],
                "confidence": "high",
                "evidence": ["artifact:abc"],
                "notes": "confirmed",
            },
        )
        second = self.client.post(
            f"/api/v1/attributions/{attribution_id}/reviews",
            headers=headers,
            json={
                "primary": "ANSWER",
                "secondary": [],
                "confidence": "medium",
                "evidence": ["artifact:def"],
                "notes": "revised",
            },
        )
        history = self.client.get(f"/api/v1/attributions/{attribution_id}/reviews")
        self.assertEqual(first.status_code, 201, first.text)
        self.assertEqual(second.status_code, 201, second.text)
        self.assertEqual(second.json()["prior_review_id"], first.json()["id"])
        self.assertEqual([item["revision"] for item in history.json()], [1, 2])
        with self.session_factory() as session:
            self.assertEqual(session.get(AttributionORM, attribution_id).label, "ANSWER")
            self.assertEqual(session.query(HumanReviewORM).count(), 2)
            self.assertEqual(session.query(AuditEventORM).filter_by(action="human_review.append").count(), 2)


if __name__ == "__main__":
    unittest.main()
