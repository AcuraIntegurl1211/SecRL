"""AdapterCapabilities contract tests: benchmark gating must come from the
adapter, never from hardcoded benchmark ids (CDB integration prerequisite)."""

import unittest

from pydantic import ValidationError

from secrl_platform.benchmarks.protocol import (
    AdapterCapabilities,
    BenchmarkManifest,
)
from secrl_platform.benchmarks.registry import (
    AdapterCapabilitiesError,
    BenchmarkRegistry,
)
from secrl_platform.benchmarks.secrl import SecRLAdapter
from secrl_platform.benchmarks.smoke import ProtocolSmokeAdapter


class AdapterCapabilitiesModelTest(unittest.TestCase):
    def test_three_switches_are_required_booleans(self):
        with self.assertRaises(ValidationError):
            AdapterCapabilities()
        with self.assertRaises(ValidationError):
            AdapterCapabilities(needs_llm_evaluator=True)
        caps = AdapterCapabilities(
            needs_llm_evaluator=True,
            requires_incident_services=False,
            supports_failure_analysis=False,
        )
        self.assertIs(caps.needs_llm_evaluator, True)
        self.assertIs(caps.requires_incident_services, False)
        self.assertIs(caps.supports_failure_analysis, False)

    def test_model_is_frozen_and_rejects_unknown_fields(self):
        caps = AdapterCapabilities(
            needs_llm_evaluator=False,
            requires_incident_services=False,
            supports_failure_analysis=False,
        )
        with self.assertRaises(ValidationError):
            caps.needs_llm_evaluator = True
        with self.assertRaises(ValidationError):
            AdapterCapabilities(
                needs_llm_evaluator=False,
                requires_incident_services=False,
                supports_failure_analysis=False,
                extra_switch=True,
            )


class BuiltinAdapterCapabilitiesTest(unittest.TestCase):
    def test_secrl_declares_all_three_capabilities(self):
        caps = SecRLAdapter().capabilities()
        self.assertEqual(
            caps,
            AdapterCapabilities(
                needs_llm_evaluator=True,
                requires_incident_services=True,
                supports_failure_analysis=True,
            ),
        )

    def test_protocol_smoke_declares_no_capabilities(self):
        caps = ProtocolSmokeAdapter.load_default().capabilities()
        self.assertEqual(
            caps,
            AdapterCapabilities(
                needs_llm_evaluator=False,
                requires_incident_services=False,
                supports_failure_analysis=False,
            ),
        )

    def test_capabilities_are_kept_out_of_frozen_manifest_identity(self):
        # The benchmark revision sha256 pins manifest_json, so an extra field
        # in the manifest dump would silently change historical identity.
        for adapter in (SecRLAdapter(), ProtocolSmokeAdapter.load_default()):
            manifest = adapter.manifest()
            self.assertNotIn("capabilities", manifest.model_dump())
            self.assertNotIn("capabilities", manifest.model_dump(mode="json"))


class CapableStubAdapter:
    def __init__(self, benchmark_id="stub", *, capabilities=None):
        self._benchmark_id = benchmark_id
        self._capabilities = capabilities

    def manifest(self):
        return BenchmarkManifest(
            benchmark_id=self._benchmark_id,
            name="Stub",
            version="1",
        )

    def capabilities(self):
        if self._capabilities is None:
            raise RuntimeError("capabilities exploded")
        return self._capabilities


class BenchmarkRegistryCapabilitiesTest(unittest.TestCase):
    def test_register_accepts_capable_adapter_and_records_switches(self):
        registry = BenchmarkRegistry()
        caps = AdapterCapabilities(
            needs_llm_evaluator=True,
            requires_incident_services=False,
            supports_failure_analysis=False,
        )
        adapter = CapableStubAdapter(capabilities=caps)
        registry.register(adapter)
        self.assertIs(registry.get("stub"), adapter)
        self.assertEqual(registry.capabilities("stub"), caps)

    def test_register_rejects_adapter_without_capabilities(self):
        class LegacyStub:
            def manifest(self):
                return BenchmarkManifest(
                    benchmark_id="legacy", name="Legacy", version="1"
                )

        registry = BenchmarkRegistry()
        with self.assertRaises(AdapterCapabilitiesError):
            registry.register(LegacyStub())

    def test_register_rejects_adapter_with_invalid_capabilities(self):
        registry = BenchmarkRegistry()
        with self.assertRaises(AdapterCapabilitiesError):
            registry.register(CapableStubAdapter(capabilities={"not": "caps"}))
        with self.assertRaises(AdapterCapabilitiesError):
            registry.register(CapableStubAdapter(capabilities=None))

    def test_builtin_registry_exposes_both_benchmarks(self):
        from secrl_platform.benchmarks.registry import builtin_benchmarks

        registry = builtin_benchmarks()
        self.assertEqual(set(registry.ids()), {"secrl", "protocol-smoke"})
        secrl_caps = registry.capabilities("secrl")
        self.assertTrue(secrl_caps.needs_llm_evaluator)
        self.assertTrue(secrl_caps.requires_incident_services)
        self.assertTrue(secrl_caps.supports_failure_analysis)

        smoke_caps = registry.capabilities("protocol-smoke")
        self.assertFalse(smoke_caps.needs_llm_evaluator)
        self.assertFalse(smoke_caps.requires_incident_services)
        self.assertFalse(smoke_caps.supports_failure_analysis)

    def test_create_returns_fresh_instances_per_call(self):
        from secrl_platform.benchmarks.registry import builtin_benchmarks

        registry = builtin_benchmarks()
        first = registry.create("protocol-smoke")
        second = registry.create("protocol-smoke")
        self.assertIsNot(first, second)
        self.assertIsNot(first, registry.get("protocol-smoke"))
        self.assertEqual(first.manifest(), second.manifest())


class BuildBenchmarkAdapterTest(unittest.TestCase):
    def test_builds_secrl_with_run_limits(self):
        from secrl_platform.benchmarks.registry import build_benchmark_adapter

        adapter = build_benchmark_adapter(
            "secrl",
            {"max_steps": 7, "max_str_len": 100, "max_entry_return": 3},
        )
        self.assertEqual(adapter.manifest().benchmark_id, "secrl")
        self.assertEqual(adapter.run_spec.max_steps, 7)
        self.assertEqual(adapter.run_spec.max_str_len, 100)
        self.assertEqual(adapter.run_spec.max_entry_return, 3)

    def test_builds_secrl_with_default_limits_when_none(self):
        from secrl_platform.benchmarks.registry import build_benchmark_adapter
        from secrl_platform.benchmarks.secrl import SecRLRunSpec

        adapter = build_benchmark_adapter("secrl", None)
        self.assertEqual(adapter.run_spec.max_steps, SecRLRunSpec().max_steps)

    def test_builds_protocol_smoke(self):
        from secrl_platform.benchmarks.registry import build_benchmark_adapter

        adapter = build_benchmark_adapter("protocol-smoke", None)
        self.assertEqual(adapter.manifest().benchmark_id, "protocol-smoke")

    def test_unknown_benchmark_raises_lookup_error(self):
        from secrl_platform.benchmarks.registry import (
            UnknownBenchmarkError,
            build_benchmark_adapter,
        )

        with self.assertRaises(UnknownBenchmarkError):
            build_benchmark_adapter("cdb", None)


class ThirdPartyBenchmarkTest(unittest.TestCase):
    """Acceptance: a benchmark id the routes have never heard of flows through
    POST /tasks and GET /preflight using only declared capabilities -- the
    exact prerequisite for dropping in CDBAdapter without touching the API."""

    class CdbLikeAdapter:
        BENCHMARK_ID = "cdb-sample"

        def manifest(self):
            return BenchmarkManifest(
                benchmark_id=self.BENCHMARK_ID,
                name="CDB Sample",
                version="0.1.0",
            )

        def capabilities(self):
            # Deterministic coverage scoring, self-contained env, no SecRL
            # failure analyzer.
            return AdapterCapabilities(
                needs_llm_evaluator=False,
                requires_incident_services=False,
                supports_failure_analysis=False,
            )

        def dataset_ref(self):
            from secrl_platform.benchmarks.protocol import DatasetRef

            return DatasetRef(
                dataset_id="cdb-sample",
                version="0.1.0",
                sha256="ab" * 32,
            )

        def enumerate_cases(self, dataset, scope):
            from secrl_platform.benchmarks.protocol import CaseRef, ScenarioRef

            cases = [
                CaseRef(
                    id=f"cdb-{index}",
                    scenario=ScenarioRef(id="cdb-scenario"),
                    public_input={"ordinal": index, "prompt": f"query {index}"},
                )
                for index in (1, 2)
            ]
            if scope.case_ids is not None:
                wanted = set(scope.case_ids)
                cases = [case for case in cases if case.id in wanted]
            return cases

        def tool_definitions(self):
            return []

    def setUp(self):
        from dataclasses import replace

        from secrl_platform.benchmarks.registry import builtin_benchmarks
        from tests.platform.test_api import ApiTest

        # Reuse the full API harness (app, session, auth, artifact store).
        self._harness = ApiTest("test_create_task_returns_frozen_spec_hash")
        self._harness.setUp()
        self.addCleanup(self._harness.tearDown)

        registry = builtin_benchmarks().copy()
        adapter = self.CdbLikeAdapter()
        registry.register(adapter, factory=lambda _limits: adapter)
        self._harness.app.state.api_context = replace(
            self._harness.app.state.api_context, benchmarks=registry
        )

    def test_third_party_benchmark_queues_and_preflights_via_capabilities(self):
        harness = self._harness
        harness.login()
        headers = {"X-CSRF-Token": harness.csrf_token}

        catalog = harness.client.get("/api/v1/benchmarks")
        self.assertEqual(catalog.status_code, 200, catalog.text)
        listed = {
            item["manifest"]["benchmark_id"] for item in catalog.json()
        }
        self.assertIn(self.CdbLikeAdapter.BENCHMARK_ID, listed)

        preflight = harness.client.get(
            "/api/v1/preflight",
            params={
                "benchmark_id": self.CdbLikeAdapter.BENCHMARK_ID,
                "scope_mode": "CASES",
                "case_ids": ["cdb-1"],
            },
        )
        self.assertEqual(preflight.status_code, 200, preflight.text)
        body = preflight.json()
        environment_check = next(
            item for item in body["checks"] if item["name"] == "environment"
        )
        self.assertEqual(environment_check["status"], "not_applicable")
        model_check = next(
            item for item in body["checks"] if item["name"] == "model_secret"
        )
        self.assertEqual(model_check["status"], "not_applicable")
        # No incident services -> no frozen dataset block, mirroring smoke.
        self.assertIsNone(body["dataset"])
        self.assertEqual(body["scope"]["case_count"], 1)

        created = harness.client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "name": "cdb sample run",
                "benchmark_id": self.CdbLikeAdapter.BENCHMARK_ID,
                "agent_revision_id": "builtin-deterministic-smoke-v1",
                "scope_mode": "ALL_BENCHMARK",
                "all_cases": True,
                "budget": {"max_cases": 2},
            },
        )
        self.assertEqual(created.status_code, 201, created.text)
        task = next(
            item
            for item in harness.client.get("/api/v1/tasks").json()
            if item["id"] == created.json()["id"]
        )
        spec = task["task_spec"]
        self.assertEqual(spec["benchmark_id"], self.CdbLikeAdapter.BENCHMARK_ID)
        self.assertEqual(len(spec["case_ids"]), 2)
        # Incident-free benchmark resolves to zero incidents.
        self.assertEqual(spec["selection"]["resolved_incident_count"], 0)

    def test_benchmark_not_in_registry_is_rejected_end_to_end(self):
        harness = self._harness
        harness.login()
        headers = {"X-CSRF-Token": harness.csrf_token}

        task = harness.client.post(
            "/api/v1/tasks",
            headers=headers,
            json={
                "name": "not registered",
                "benchmark_id": "unknown-benchmark",
                "agent_revision_id": "builtin-deterministic-smoke-v1",
                "case_ids": ["x-1"],
                "budget": {"max_cases": 1},
            },
        )
        self.assertEqual(task.status_code, 422, task.text)
        self.assertEqual(task.json()["error"]["code"], "INVALID_TASK_SPEC")

        preflight = harness.client.get(
            "/api/v1/preflight",
            params={"benchmark_id": "unknown-benchmark"},
        )
        self.assertEqual(preflight.status_code, 422, preflight.text)
        self.assertEqual(preflight.json()["error"]["code"], "INVALID_TASK_SPEC")

        cases = harness.client.get("/api/v1/benchmarks/unknown-benchmark/cases")
        self.assertEqual(cases.status_code, 404, cases.text)
        self.assertEqual(cases.json()["error"]["code"], "BENCHMARK_NOT_FOUND")


class FailureAnalysisCapabilityGateTest(unittest.TestCase):
    """supports_failure_analysis gates BOTH the auto trigger and the manual
    :analyze endpoint; this pins the manual path at the HTTP boundary."""

    def test_analyze_rejects_a_run_whose_benchmark_lacks_the_capability(self):
        import asyncio

        from secrl_platform.runner.process import run_pending_once
        from tests.platform.test_api import ApiTest, valid_smoke_task

        harness = ApiTest("test_create_task_returns_frozen_spec_hash")
        harness.setUp()
        self.addCleanup(harness.tearDown)
        harness.login()
        headers = {"X-CSRF-Token": harness.csrf_token}
        created = harness.client.post(
            "/api/v1/tasks", headers=headers, json=valid_smoke_task()
        )
        self.assertEqual(created.status_code, 201, created.text)
        status = asyncio.run(
            run_pending_once(
                settings=harness.settings,
                session_factory=harness.session_factory,
                artifact_store=harness.artifact_store,
            )
        )
        self.assertEqual(status, "SUCCEEDED")
        analyzed = harness.client.post(
            f"/api/v1/runs/{created.json()['run_id']}:analyze", headers=headers
        )
        self.assertEqual(analyzed.status_code, 409, analyzed.text)
        self.assertEqual(analyzed.json()["error"]["code"], "ANALYSIS_NOT_READY")


class RunnerAdapterGateTest(unittest.TestCase):
    """The runner resolves the adapter from the registry allowlist; a task
    spec naming a non-allowlisted benchmark must fail configuration closed."""

    def test_runner_rejects_unallowlisted_benchmark_id(self):
        import asyncio
        import json

        from secrl_platform.runner.process import run_pending_once
        from secrl_platform.storage.orm import EvaluationTaskORM
        from tests.platform.test_api import ApiTest, valid_smoke_task

        harness = ApiTest("test_create_task_returns_frozen_spec_hash")
        harness.setUp()
        self.addCleanup(harness.tearDown)
        harness.login()
        created = harness.client.post(
            "/api/v1/tasks",
            headers={"X-CSRF-Token": harness.csrf_token},
            json=valid_smoke_task(),
        )
        self.assertEqual(created.status_code, 201, created.text)
        with harness.session_factory.begin() as session:
            task = session.get(EvaluationTaskORM, created.json()["id"])
            spec = json.loads(task.task_spec_json)
            spec["benchmark_id"] = "cdb-does-not-exist"
            task.task_spec_json = json.dumps(spec, sort_keys=True)

        status = asyncio.run(
            run_pending_once(
                settings=harness.settings,
                session_factory=harness.session_factory,
                artifact_store=harness.artifact_store,
            )
        )
        self.assertEqual(status, "FAILED")
        detail = harness.client.get(f"/api/v1/runs/{created.json()['run_id']}")
        self.assertEqual(detail.json()["failure"]["code"], "RUNNER_CONFIGURATION_ERROR")


if __name__ == "__main__":
    unittest.main()
