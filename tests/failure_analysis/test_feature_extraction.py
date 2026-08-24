import unittest

from experiments.failure_analysis.features import extract_features
from experiments.failure_analysis.identity import map_logs
from experiments.failure_analysis.models import MappingError
from tests.failure_analysis.helpers import agent_entry, env_entry, question


def query_step(action, observation, success=True):
    return {
        "action": action,
        "observation": observation,
        "reward": 0,
        "done": False,
        "info": {"query_success": success, "submit": False},
    }


def submit_step(answer, reward=1.0, complete=True):
    info = {
        "query_success": True,
        "submit": True,
        "submitted_answer": answer,
        "reward": reward,
    }
    if complete:
        info.update(
            {
                "check_ans_response": "ok",
                "check_ans_reflection": "ok",
                "check_sol_response": "ok",
                "check_sol_reflection": "ok",
            }
        )
    return {
        "action": answer,
        "observation": "",
        "reward": reward,
        "done": True,
        "info": info,
    }


def mapped_question(answer, trajectory, reward=0.0, usage=True):
    item = question("investigate", answer=answer)
    agent = agent_entry(item, reward=reward)
    if usage:
        agent["trials"] = {
            "trial1": {
                "usage_summary": {
                    "deepseek-v4-flash": {
                        "prompt_tokens": 10,
                        "completion_tokens": 5,
                        "total_tokens": 15,
                    }
                }
            }
        }
    env = env_entry(item, reward=reward, trajectory=trajectory)
    return map_logs("incident_5", [agent], [env], [item])[0]


class FeatureExtractionTest(unittest.TestCase):
    def test_extracts_sql_submission_token_and_evidence_features(self):
        trajectory = [
            query_step(" SELECT *  FROM Alerts; ", "[]"),
            query_step("select * from alerts", "[('203.0.113.5',)]"),
            query_step("BAD SQL", "ProgrammingError: unknown column", False),
            submit_step("203.0.113.5", reward=1.0),
        ]
        features = extract_features(
            mapped_question("203.0.113.5", trajectory, reward=1.0),
            max_steps=4,
        )
        self.assertEqual(features.reward_official, 1.0)
        self.assertEqual(features.sql_total, 3)
        self.assertEqual(features.sql_success, 2)
        self.assertEqual(features.sql_failure, 1)
        self.assertEqual(features.empty_result_count, 1)
        self.assertEqual(features.duplicate_query_count, 1)
        self.assertEqual(features.steps, 4)
        self.assertTrue(features.submitted)
        self.assertTrue(features.submitted_at_step_limit)
        self.assertEqual(features.submitted_answer, "203.0.113.5")
        self.assertEqual(features.gold_evidence_match, "exact")
        self.assertEqual(features.gold_evidence_steps, [2])
        self.assertTrue(features.evaluator_fields_complete)
        self.assertEqual(features.agent_prompt_tokens, 10)
        self.assertEqual(features.agent_completion_tokens, 5)
        self.assertEqual(features.agent_total_tokens, 15)
        self.assertIsNone(features.evaluator_tokens)
        errors = [item for item in features.evidence if item.kind == "sql_error"]
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0].step, 3)
        self.assertEqual(errors[0].field, "trajectory[2].observation")

    def test_unfinished_step_limit_is_not_a_step_limit_submission(self):
        trajectory = [
            query_step("select 1", "[(1,)]"),
            query_step("select 2", "[(2,)]"),
            query_step("select 3", "[(3,)]"),
        ]
        features = extract_features(
            mapped_question("answer", trajectory),
            max_steps=3,
        )
        self.assertFalse(features.submitted)
        self.assertFalse(features.submitted_at_step_limit)
        self.assertEqual(features.steps, 3)

    def test_reward_conflict_is_rejected(self):
        item = question("investigate", answer="answer")
        agent = agent_entry(item, reward=1.0)
        env = env_entry(item, reward=0.0, trajectory=[submit_step("answer")])
        mapped = map_logs("incident_5", [agent], [env], [item])[0]
        with self.assertRaisesRegex(MappingError, "reward conflict"):
            extract_features(mapped, max_steps=15)

    def test_absent_usage_stays_unknown_instead_of_zero(self):
        features = extract_features(
            mapped_question("answer", [submit_step("answer")], usage=False),
            max_steps=15,
        )
        self.assertIsNone(features.agent_prompt_tokens)
        self.assertIsNone(features.agent_completion_tokens)
        self.assertIsNone(features.agent_total_tokens)
        self.assertIsNone(features.evaluator_tokens)

    def test_long_evidence_excerpt_is_truncated(self):
        observation = "ProgrammingError: " + "x" * 400
        features = extract_features(
            mapped_question("answer", [query_step("bad", observation, False)]),
            max_steps=15,
        )
        evidence = next(item for item in features.evidence if item.kind == "sql_error")
        self.assertEqual(len(evidence.excerpt), 240)
        self.assertTrue(evidence.excerpt_truncated)

    def test_normalized_gold_evidence_is_detected(self):
        features = extract_features(
            mapped_question(
                "host server01",
                [query_step("select host", "host server01.contoso.local")],
            ),
            max_steps=15,
        )
        self.assertEqual(features.gold_evidence_match, "normalized")
        self.assertEqual(features.gold_evidence_steps, [1])

    def test_structured_gold_components_can_span_steps(self):
        features = extract_features(
            mapped_question(
                ["10.0.0.1", "powershell.exe"],
                [
                    query_step("select ip", "10.0.0.1"),
                    query_step("select process", "powershell.exe"),
                ],
            ),
            max_steps=15,
        )
        self.assertEqual(features.gold_evidence_match, "component")
        self.assertEqual(features.gold_evidence_steps, [1, 2])

    def test_missing_gold_evidence_is_not_found(self):
        features = extract_features(
            mapped_question("expected", [query_step("select value", "different")]),
            max_steps=15,
        )
        self.assertEqual(features.gold_evidence_match, "not_found")
        self.assertEqual(features.gold_evidence_steps, [])

    def test_absent_gold_or_observations_is_indeterminate(self):
        features = extract_features(
            mapped_question("", [submit_step("answer")]),
            max_steps=15,
        )
        self.assertEqual(features.gold_evidence_match, "indeterminate")


if __name__ == "__main__":
    unittest.main()
