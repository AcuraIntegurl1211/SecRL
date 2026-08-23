from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from pydantic import ValidationError

from secrl_platform.benchmarks.protocol import Submission
from secrl_platform.benchmarks.secrl import SecRLAdapter
from secrl_platform.models.evaluator import (
    EvaluatorParameterOverride,
    SecRLEvaluator,
    official_secrl_profile,
)


class SecRLEvaluatorTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
