from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
import hashlib
from pydantic import ValidationError

from secrl_platform.benchmarks.protocol import Submission
from secrl_platform.benchmarks.secrl import SecRLAdapter
from secrl_platform.models.evaluator import (
    EvaluatorGatewayClient,
    EvaluatorParameterOverride,
    SecRLEvaluator,
    official_secrl_profile,
)
from secrl_platform.models.gateway import GatewayResponse
from secrl_platform.models.providers import Usage


class _FakeGateway:
    def __init__(self):
        self.requests = []

    async def complete(self, request):
        self.requests.append(request)
        return GatewayResponse(
            text="Analysis: equivalent\nIs_Answer_Correct: True",
            usage=Usage(prompt=11, completion=3),
            estimated_cost=None,
            pricing_profile_sha256="0" * 64,
        )


class SecRLEvaluatorTest(unittest.TestCase):
    def test_official_profile_binds_frozen_legacy_evaluator_source(self):
        source = Path("secgym/evaluator.py").read_bytes()
        profile = official_secrl_profile(formal=True)
        self.assertEqual(
            hashlib.sha256(source).hexdigest(),
            "b146af231c0b63d7252c5b7852c62f0ba59ab40980b65be5d003cbc2f08d05e2",
        )
        self.assertEqual(profile.source_sha256, hashlib.sha256(source).hexdigest())
        self.assertTrue(profile.answer_reflection)
        self.assertTrue(profile.solution_reflection)
        self.assertTrue(profile.step_checking)

    def test_official_solution_reward_matches_legacy_discount_algorithm(self):
        responses = iter(
            [
                "Analysis: wrong\nIs_Answer_Correct: False",
                "Reflection: checked\nAnalysis: wrong\nIs_Answer_Correct: False",
                '{"step_0":{"is_step_correct":"True"},"step_1":{"is_step_correct":"True"},"step_2":{"is_step_correct":"False"}}',
                '{"step_0":{"is_step_correct":"True"},"step_1":{"is_step_correct":"True"},"step_2":{"is_step_correct":"False"}}',
            ]
        )

        def model_client(_prompt, _parameters):
            return {"text": next(responses), "usage": {"prompt_tokens": 1, "completion_tokens": 1}}

        evaluator = SecRLEvaluator(official_secrl_profile(formal=True), model_client=model_client)
        result = evaluator.evaluate(
            context="Incident context",
            question="Which indicators?",
            gold_answer="gold",
            solution=("find first", "find second", "submit"),
            submitted_answer="partial",
        )
        self.assertAlmostEqual(result.reward, 0.56)
        self.assertEqual(result.usage.prompt_tokens, 4)
        self.assertEqual(result.usage.completion_tokens, 4)

    def test_gateway_client_uses_evaluator_role_and_bound_attempt(self):
        gateway = _FakeGateway()
        client = EvaluatorGatewayClient(
            gateway=gateway,
            model="fixture-model",
            capability_token="evaluator-capability",
            agent_revision_id="secrl-baseline-v1",
            max_output_tokens=64,
        )
        client.bind_attempt(run_id="run-1", case_id="case-1", attempt_id="attempt-1")

        response = client.complete(prompt="private evaluator prompt", parameters={"temperature": 0.0, "seed": 41})

        request = gateway.requests[0]
        self.assertEqual(request.model_role, "evaluator")
        self.assertEqual((request.run_id, request.case_id, request.attempt_id), ("run-1", "case-1", "attempt-1"))
        self.assertEqual(response["usage"]["prompt_tokens"], 11)
        self.assertEqual(response["usage"]["completion_tokens"], 3)
        self.assertEqual(response["usage"]["estimated_cost"], "0")

    def test_official_model_request_contains_gold_only_in_private_prompt(self):
        prompts = []

        def model_client(prompt, _parameters):
            prompts.append(prompt)
            return {"text": "Analysis: equivalent\nIs_Answer_Correct: True", "usage": {}}

        evaluator = SecRLEvaluator(official_secrl_profile(formal=True), model_client=model_client)
        result = evaluator.evaluate(
            question="Which host?",
            gold_answer="secret-host",
            submitted_answer="the same host",
        )
        self.assertIn("Golden Answer: secret-host", prompts[0])
        self.assertNotIn("secret-host", json.dumps(result.request.model_dump(mode="json")))
        self.assertEqual(result.reward, 1.0)

    def test_malformed_official_response_is_not_silently_exact_matched(self):
        evaluator = SecRLEvaluator(
            official_secrl_profile(formal=True),
            model_client=lambda _prompt, _parameters: {"text": "unparseable", "usage": {}},
        )
        with self.assertRaisesRegex(ValueError, "official evaluator response"):
            evaluator.evaluate(question="q", gold_answer="a", submitted_answer="a")

    def test_official_profile_freezes_protocol_inputs(self):
        profile = official_secrl_profile(formal=True)
        self.assertTrue(profile.formal)
        self.assertEqual(profile.parser_version, "secrl-answer-v1")
        self.assertEqual(len(profile.prompt_template_sha256), 64)
        with self.assertRaises(ValidationError):
            profile.temperature = 0.7

    def test_formal_profile_rejects_per_task_overrides(self):
        evaluator = SecRLEvaluator(official_secrl_profile(formal=True))
        with self.assertRaises(ValueError):
            evaluator.evaluate(
                question="q",
                gold_answer="a",
                submitted_answer="a",
                overrides=EvaluatorParameterOverride(temperature=0.8),
            )

    def test_request_hash_and_effective_parameters_are_deterministic(self):
        evaluator = SecRLEvaluator(official_secrl_profile(formal=True))
        first = evaluator.evaluate(question="q", gold_answer="a", submitted_answer="a")
        second = evaluator.evaluate(question="q", gold_answer="a", submitted_answer="a")
        self.assertEqual(first.request.prompt_sha256, second.request.prompt_sha256)
        self.assertEqual(first.request.effective_parameters, {"temperature": 0.0, "seed": 41})
        self.assertEqual(first.reward, 1.0)
        self.assertEqual(first.usage.role, "evaluator")

    def test_raw_response_requires_restricted_access_and_never_enters_request(self):
        evaluator = SecRLEvaluator(official_secrl_profile(formal=True))
        result = evaluator.evaluate(question="q", gold_answer="secret-gold", submitted_answer="wrong")
        self.assertNotIn("secret-gold", json.dumps(result.request.model_dump(mode="json")))
        with self.assertRaises(PermissionError):
            result.read_raw_response()
        self.assertIn("Is_Answer_Correct", result.read_raw_response(evaluator.restricted_access()))

    def test_adapter_uses_frozen_evaluator_profile(self):
        evaluator = SecRLEvaluator(official_secrl_profile(formal=True))
        adapter = SecRLAdapter(evaluator=evaluator)
        case = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())[0]
        lease = adapter.prepare_scenario(case.scenario)
        episode = adapter.start_episode(case, lease)
        result = adapter.evaluate(episode.ref, Submission(answer=adapter.gold_for(case.id, adapter.restricted_access())["answer"]))
        self.assertEqual(result.reward, 1.0)
        restricted = adapter.take_restricted_artifacts(episode.ref)
        self.assertEqual(len(restricted), 1)
        self.assertEqual(restricted[0][0], "evaluator-response")
        self.assertNotIn(
            adapter.gold_for(case.id, adapter.restricted_access())["answer"],
            json.dumps(result.model_dump(mode="json")),
        )


if __name__ == "__main__":
    unittest.main()
