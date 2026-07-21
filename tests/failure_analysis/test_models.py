import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from experiments.failure_analysis.models import Evidence, QuestionIdentity


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / "experiments/failure_analysis/taxonomy_v1.json"


class ModelsTest(unittest.TestCase):
    def test_identity_is_immutable(self):
        identity = QuestionIdentity(
            "incident_5",
            3,
            "a" * 64,
            "b" * 64,
        )
        with self.assertRaises(FrozenInstanceError):
            identity.question_index = 4

    def test_evidence_serializes_with_traceable_location(self):
        evidence = Evidence(
            "sql_error",
            9,
            "env",
            "trajectory[8].observation",
            "bad SQL",
            False,
        )
        self.assertEqual(evidence.as_dict()["step"], 9)

    def test_taxonomy_is_frozen_v1(self):
        data = json.loads(TAXONOMY.read_text(encoding="utf-8"))
        self.assertEqual(data["taxonomy_version"], "taxonomy_v1")
        self.assertEqual(len(data["categories"]), 12)
        self.assertEqual(len(data["calibration"]), 10)
