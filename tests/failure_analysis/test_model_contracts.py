import json
import unittest
from pathlib import Path

from experiments.failure_analysis.models import (
    SCHEMA_VERSION,
    AnalysisError,
    Attribution,
    FeatureRecord,
    InputError,
    MappedQuestion,
    MappingError,
    OutputCollisionError,
    QuestionIdentity,
    ReviewError,
)


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / "experiments/failure_analysis/taxonomy_v1.json"


class ModelContractsTest(unittest.TestCase):
    def make_mapped_question(self):
        identity = QuestionIdentity("incident_5", 0, "a" * 64, "b" * 64)
        return MappedQuestion(
            identity=identity,
            question={"question": "q", "answer": "a"},
            agent={"reward": 0},
            env={"reward": 0, "trajectory": []},
            agent_source_index=2,
            env_source_index=3,
        )

    def test_schema_and_exit_codes_are_frozen(self):
        self.assertEqual(SCHEMA_VERSION, "failure_attribution_v1")
        self.assertTrue(issubclass(InputError, AnalysisError))
        self.assertEqual(InputError.exit_code, 2)
        self.assertEqual(MappingError.exit_code, 3)
        self.assertEqual(OutputCollisionError.exit_code, 4)
        self.assertEqual(ReviewError.exit_code, 5)

    def test_mapped_question_preserves_independent_source_indexes(self):
        mapped = self.make_mapped_question()
        self.assertEqual(mapped.agent_source_index, 2)
        self.assertEqual(mapped.env_source_index, 3)

    def test_feature_record_uses_independent_mutable_defaults(self):
        values = dict(
            reward_official=0.0,
            submitted_answer="",
            sql_total=0,
            sql_success=0,
            sql_failure=0,
            empty_result_count=0,
            duplicate_query_count=0,
            steps=0,
            max_steps=15,
            submitted=False,
            submitted_at_step_limit=False,
            gold_evidence_match="indeterminate",
        )
        first = FeatureRecord(mapped=self.make_mapped_question(), **values)
        second = FeatureRecord(mapped=self.make_mapped_question(), **values)
        first.gold_evidence_steps.append(1)
        self.assertEqual(second.gold_evidence_steps, [])
        self.assertEqual(second.evidence, [])
        self.assertIsNone(second.evaluator_tokens)

    def test_attribution_separates_candidate_and_reviewed_fields(self):
        attribution = Attribution(
            primary_cause_candidate="GOLD",
            primary_cause_status="candidate",
            secondary_cause_candidates=["STEP_LIMIT"],
            confidence="low",
            needs_human_review=True,
            human_review_reasons=["mandatory:GOLD"],
        )
        self.assertEqual(attribution.primary_cause_candidate, "GOLD")
        self.assertIsNone(attribution.reviewed_primary)
        self.assertEqual(attribution.reviewed_secondary, [])
        self.assertEqual(attribution.review_status, "unreviewed")

    def test_taxonomy_policies_and_calibration_identities_are_frozen(self):
        data = json.loads(TAXONOMY.read_text(encoding="utf-8"))
        self.assertEqual(
            data["categories"],
            [
                "DATA", "SQL_EXEC", "SQL_RETRIEVAL", "NAVIGATION",
                "LOOP", "STEP_LIMIT", "REASONING", "ANSWER",
                "EVALUATOR", "GOLD", "INFRA", "UNKNOWN",
            ],
        )
        self.assertEqual(
            data["always_human_review"],
            ["EVALUATOR", "GOLD", "UNKNOWN"],
        )
        self.assertTrue(data["loop_and_step_limit_normally_secondary"])
        self.assertEqual(data["review_sampling"]["seed"], 20260720)
        self.assertEqual(
            {item["question_index"] for item in data["calibration"]},
            {5, 10, 13, 23, 26, 34, 55, 65, 79, 80},
        )
        self.assertTrue(
            all(
                item["incident"] == "incident_5"
                for item in data["calibration"]
            )
        )


if __name__ == "__main__":
    unittest.main()
