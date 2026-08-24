import unittest
from pathlib import Path

from experiments.failure_analysis.attribution import attribute_record, load_taxonomy
from experiments.failure_analysis.models import (
    Evidence,
    FeatureRecord,
    MappedQuestion,
    QuestionIdentity,
)


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_PATH = ROOT / "experiments/failure_analysis/taxonomy_v1.json"


def evidence(kind):
    return Evidence(kind, 1, "env", "trajectory[0].observation", kind, False)


def feature_record(
    *,
    incident="incident_5",
    index=999,
    fingerprint=None,
    reward=0.0,
    golden="expected",
    submitted="wrong",
    gold_match="not_found",
    sql_success=0,
    sql_failure=0,
    empty_results=0,
    duplicates=0,
    steps=1,
    max_steps=15,
    submitted_flag=True,
    evidence_kinds=(),
):
    fingerprint = fingerprint or (f"{index:064x}"[-64:])
    identity = QuestionIdentity(incident, index, fingerprint, "e" * 64)
    question = {"question": "investigate", "answer": golden}
    mapped = MappedQuestion(
        identity=identity,
        question=question,
        agent={"question_dict": question, "reward": reward},
        env={"question": question, "reward": reward, "trajectory": []},
        agent_source_index=0,
        env_source_index=0,
    )
    return FeatureRecord(
        mapped=mapped,
        reward_official=reward,
        submitted_answer=submitted,
        sql_total=sql_success + sql_failure,
        sql_success=sql_success,
        sql_failure=sql_failure,
        empty_result_count=empty_results,
        duplicate_query_count=duplicates,
        steps=steps,
        max_steps=max_steps,
        submitted=submitted_flag,
        submitted_at_step_limit=submitted_flag and steps == max_steps,
        gold_evidence_match=gold_match,
        evidence=[evidence(kind) for kind in evidence_kinds],
    )


class AttributionTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.taxonomy = load_taxonomy(TAXONOMY_PATH)

    def test_reward_one_is_always_a_correct_control(self):
        features = feature_record(
            reward=1.0,
            sql_failure=1,
            empty_results=1,
            duplicates=2,
            steps=15,
            max_steps=15,
            evidence_kinds=("sql_error",),
        )
        result = attribute_record(features, self.taxonomy)
        self.assertIsNone(result.primary_cause_candidate)
        self.assertEqual(result.primary_cause_status, "correct_control")
        self.assertEqual(result.secondary_cause_candidates, [])
        self.assertEqual(result.confidence, "high")
        self.assertFalse(result.needs_human_review)

    def test_all_ten_calibration_records_are_frozen(self):
        for item in self.taxonomy["calibration"]:
            with self.subTest(index=item["question_index"]):
                expected = item["expected_primary"]
                reward = 1.0 if expected is None else 0.0
                features = feature_record(
                    index=item["question_index"],
                    fingerprint=item["question_fingerprint_sha256"],
                    reward=reward,
                )
                result = attribute_record(features, self.taxonomy)
                self.assertEqual(result.primary_cause_candidate, expected)
                if item["review_status"] == "confirmed":
                    expected_status = "correct_control" if expected is None else "confirmed"
                    self.assertEqual(result.primary_cause_status, expected_status)
                    self.assertEqual(result.confidence, "high")
                else:
                    self.assertEqual(result.primary_cause_status, "candidate")
                    self.assertTrue(result.needs_human_review)

    def test_calibration_requires_incident_index_and_fingerprint(self):
        item = next(
            row for row in self.taxonomy["calibration"]
            if row["expected_primary"] == "NAVIGATION"
        )
        features = feature_record(
            index=item["question_index"] + 1,
            fingerprint=item["question_fingerprint_sha256"],
        )
        result = attribute_record(features, self.taxonomy)
        self.assertEqual(result.primary_cause_candidate, "UNKNOWN")

    def test_equivalent_answer_with_failed_reward_is_evaluator_candidate(self):
        features = feature_record(
            golden="host server01",
            submitted="HOST server01.contoso.local",
            gold_match="normalized",
        )
        result = attribute_record(features, self.taxonomy)
        self.assertEqual(result.primary_cause_candidate, "EVALUATOR")
        self.assertTrue(result.needs_human_review)
        self.assertIn("mandatory:EVALUATOR", result.human_review_reasons)

    def test_unrecovered_sql_error_is_sql_exec(self):
        features = feature_record(
            sql_failure=1,
            evidence_kinds=("sql_error",),
        )
        result = attribute_record(features, self.taxonomy)
        self.assertEqual(result.primary_cause_candidate, "SQL_EXEC")
        self.assertEqual(result.confidence, "medium")

    def test_recovered_sql_error_does_not_override_answer_evidence(self):
        features = feature_record(
            gold_match="exact",
            sql_success=1,
            sql_failure=1,
            evidence_kinds=("sql_error",),
        )
        result = attribute_record(features, self.taxonomy)
        self.assertEqual(result.primary_cause_candidate, "ANSWER")

    def test_direct_evidence_selects_supported_primary_categories(self):
        cases = {
            "data_missing": "DATA",
            "retrieval_mismatch": "SQL_RETRIEVAL",
            "navigation_mismatch": "NAVIGATION",
            "reasoning_mismatch": "REASONING",
            "gold_inconsistency": "GOLD",
        }
        for kind, expected in cases.items():
            with self.subTest(kind=kind):
                result = attribute_record(
                    feature_record(evidence_kinds=(kind,)),
                    self.taxonomy,
                )
                self.assertEqual(result.primary_cause_candidate, expected)

    def test_empty_result_alone_is_unknown_not_data(self):
        result = attribute_record(
            feature_record(empty_results=3, sql_success=3),
            self.taxonomy,
        )
        self.assertEqual(result.primary_cause_candidate, "UNKNOWN")
        self.assertTrue(result.needs_human_review)

    def test_loop_and_step_limit_remain_secondary(self):
        result = attribute_record(
            feature_record(
                sql_failure=1,
                duplicates=2,
                steps=15,
                max_steps=15,
                evidence_kinds=("sql_error",),
            ),
            self.taxonomy,
        )
        self.assertEqual(result.primary_cause_candidate, "SQL_EXEC")
        self.assertEqual(
            result.secondary_cause_candidates,
            ["LOOP", "STEP_LIMIT"],
        )

    def test_mandatory_categories_and_low_confidence_require_review(self):
        for category in ("GOLD", "EVALUATOR", "UNKNOWN"):
            with self.subTest(category=category):
                if category == "GOLD":
                    features = feature_record(evidence_kinds=("gold_inconsistency",))
                elif category == "EVALUATOR":
                    features = feature_record(golden="same", submitted="same")
                else:
                    features = feature_record()
                result = attribute_record(features, self.taxonomy)
                self.assertEqual(result.primary_cause_candidate, category)
                self.assertTrue(result.needs_human_review)


if __name__ == "__main__":
    unittest.main()
