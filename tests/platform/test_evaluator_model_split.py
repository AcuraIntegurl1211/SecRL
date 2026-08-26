"""Runner-side contract tests for split evaluator model configuration."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from secrl_platform.agents.builtin import builtin_manifest
from secrl_platform.agents.protocol import AgentRevisionRef
from secrl_platform.benchmarks.secrl import SecRLAdapter, SecRLRunSpec
from secrl_platform.config import Settings
from secrl_platform.models.secrets import (
    SecretStore,
    encrypted_secret_to_json,
)
from secrl_platform.runner import process as runner_process
from secrl_platform.runner.recovery import RunnerRepository
from secrl_platform.storage.artifacts import LocalArtifactStore
from secrl_platform.storage.database import create_engine_and_session
from secrl_platform.storage.orm import ModelConfigRevisionORM, SecretRefORM

_CASE_ID = "incident_134:0:f85431d5ee76a2f65908ea5dc308418ff5328582d4ee45c0b73b80eaa0dd5ec7"


def _agent_revision_ref() -> AgentRevisionRef:
    manifest = builtin_manifest("secrl-baseline-v1")
    return AgentRevisionRef(
        id=manifest.agent_id,
        manifest=manifest,
        manifest_sha256=manifest.sha256(),
    )


class SplitEvaluatorBundleWiringTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.session_factory = create_engine_and_session(
            root / "platform.sqlite3",
            create=True,
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
        self.agent_model_id = "11111111-1111-1111-1111-111111111111"
        self.evaluator_model_id = "22222222-2222-2222-2222-222222222222"
        self._insert_model_config(
            self.agent_model_id,
            "agent model",
            "a" * 64,
        )
        self._insert_model_config(
            self.evaluator_model_id,
            "evaluator model",
            "b" * 64,
        )

    def tearDown(self) -> None:
        self.directory.cleanup()

    def _insert_model_config(self, model_id: str, name: str, sha256: str) -> None:
        store = SecretStore(bytes.fromhex(self.settings.master_key))
        with self.session_factory.begin() as session:
            secret = SecretRefORM(
                id=f"secret-{sha256[:8]}",
                name=f"{name} credential",
                ciphertext="<filled-below>",
                status="VALID",
            )
            session.add(secret)
            session.flush()
            envelope = store.encrypt(
                "sk-test-key",
                secret_ref_id=secret.id,
                provider="openai-compatible",
            )
            secret.ciphertext = encrypted_secret_to_json(envelope)
            session.add(
                ModelConfigRevisionORM(
                    id=model_id,
                    name=name,
                    provider="openai-compatible",
                    endpoint="https://models.invalid/v1",
                    model="fixture-model",
                    secret_ref_id=secret.id,
                    parameters_json=json.dumps({"max_output_tokens": 64}),
                    pricing_json=json.dumps(
                        {"input_per_million": "1", "output_per_million": "2"}
                    ),
                    sha256=sha256,
                )
            )

    def _create_split_run(self) -> tuple[str, str]:
        adapter = SecRLAdapter(
            run_spec=SecRLRunSpec(max_steps=7, max_str_len=4096, max_entry_return=9)
        )
        handle = RunnerRepository(self.session_factory).create_benchmark_run(
            name="split evaluator wiring",
            adapter=adapter,
            agent_revision=_agent_revision_ref(),
            case_ids=(_CASE_ID,),
            budget={"max_tokens": 100000, "max_cost": "10"},
            model_config_revision_id=self.agent_model_id,
            model_config_sha256="a" * 64,
            run_limits={"max_steps": 7, "max_str_len": 4096, "max_entry_return": 9},
            agent_parameters={},
            selection=None,
            evaluator_model_config_revision_id=self.evaluator_model_id,
            evaluator_model_config_sha256="b" * 64,
        )
        return handle.task_id, handle.run_id

    def test_resolve_adapter_wires_two_distinct_bundles(self) -> None:
        task_id, run_id = self._create_split_run()
        fake_bundle = (
            object(),
            "fixture-model",
            {"max_output_tokens": 16},
            {"input_per_million": 1, "output_per_million": 2},
        )
        captured_calls = []
        evaluator_profiles = []

        def fake_resolve_model_provider(**kwargs):
            captured_calls.append(kwargs)
            return fake_bundle

        def fake_evaluator_class(profile, model_client=None):
            evaluator_profiles.append(profile)
            return mock.MagicMock(name="evaluator")

        with mock.patch.object(
            runner_process,
            "_resolve_model_provider",
            side_effect=fake_resolve_model_provider,
        ), mock.patch.object(
            runner_process, "SecRLEvaluator", side_effect=fake_evaluator_class
        ):
            adapter = runner_process._resolve_adapter(
                settings=self.settings,
                session_factory=self.session_factory,
                task_id=task_id,
                run_id=run_id,
                secrl_query_executor=lambda _scenario, _query: ([], True),
                model_provider_resolver=lambda _host, _port: ("93.184.216.34",),
                secrl_evaluator_resolver=None,
            )

        self.assertIsNotNone(adapter)
        self.assertEqual(len(captured_calls), 2)
        agent_call, evaluator_call = captured_calls
        self.assertEqual(agent_call["model_id"], self.agent_model_id)
        self.assertEqual(
            agent_call.get("sha_key", "model_config_sha256"), "model_config_sha256"
        )
        self.assertEqual(evaluator_call["model_id"], self.evaluator_model_id)
        self.assertEqual(
            evaluator_call["sha_key"], "evaluator_model_config_sha256"
        )
        self.assertEqual(len(evaluator_profiles), 1)
        self.assertEqual(evaluator_profiles[0].model_revision, "b" * 64)

    def test_single_model_task_resolves_one_bundle_with_legacy_binding(self) -> None:
        adapter = SecRLAdapter(
            run_spec=SecRLRunSpec(max_steps=7, max_str_len=4096, max_entry_return=9)
        )
        handle = RunnerRepository(self.session_factory).create_benchmark_run(
            name="legacy single model",
            adapter=adapter,
            agent_revision=_agent_revision_ref(),
            case_ids=(_CASE_ID,),
            budget={"max_tokens": 100000, "max_cost": "10"},
            model_config_revision_id=self.agent_model_id,
            model_config_sha256="a" * 64,
            run_limits={"max_steps": 7, "max_str_len": 4096, "max_entry_return": 9},
            agent_parameters={},
            selection=None,
        )
        captured_calls = []

        def fake_resolve_model_provider(**kwargs):
            captured_calls.append(kwargs)
            return (
                object(),
                "fixture-model",
                {"max_output_tokens": 16},
                {"input_per_million": 1, "output_per_million": 2},
            )

        with mock.patch.object(
            runner_process,
            "_resolve_model_provider",
            side_effect=fake_resolve_model_provider,
        ), mock.patch.object(runner_process, "SecRLEvaluator", mock.MagicMock()):
            runner_process._resolve_adapter(
                settings=self.settings,
                session_factory=self.session_factory,
                task_id=handle.task_id,
                run_id=handle.run_id,
                secrl_query_executor=lambda _scenario, _query: ([], True),
                model_provider_resolver=lambda _host, _port: ("93.184.216.34",),
                secrl_evaluator_resolver=None,
            )

        self.assertEqual(len(captured_calls), 1)
        self.assertEqual(captured_calls[0]["model_id"], self.agent_model_id)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
