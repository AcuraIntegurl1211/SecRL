from __future__ import annotations

import json
import hashlib
import tempfile
import unittest
from pathlib import Path

from secrl_platform.benchmarks.secrl import (
    SECRL_EXPECTED_SCENARIO_COUNTS,
    SecRLAdapter,
    UnsafeSQL,
)


DATASET = Path("secgym/questions/o1/test")


class SecRLImportTest(unittest.TestCase):
    def test_dataset_integrity_and_deterministic_counts(self):
        adapter = SecRLAdapter()
        report = adapter.validate_dataset(DATASET)
        self.assertTrue(report.valid, report.errors)
        self.assertEqual(report.case_count, 589)
        self.assertEqual(report.scenario_counts, SECRL_EXPECTED_SCENARIO_COUNTS)

        first = SecRLAdapter().enumerate_cases(adapter.dataset_ref(), adapter.scope_all())
        second = SecRLAdapter().enumerate_cases(adapter.dataset_ref(), adapter.scope_all())
        self.assertEqual([case.id for case in first], [case.id for case in second])
        self.assertEqual(len(first), 589)
        self.assertEqual(len({case.id for case in first}), 589)

    def test_public_input_contains_no_gold_or_credentials(self):
        adapter = SecRLAdapter()
        case = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())[0]
        serialized = json.dumps(case.public_input, sort_keys=True).lower()
        self.assertNotIn("answer", serialized)
        self.assertNotIn("solution", serialized)
        self.assertNotIn("password", serialized)
        self.assertNotIn("credential", serialized)
        self.assertNotIn("api_key", serialized)

    def test_source_artifact_preserves_all_fields_but_requires_restricted_access(self):
        adapter = SecRLAdapter()
        case = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())[0]
        reference = adapter.source_artifact(case.id)
        self.assertEqual(len(reference.sha256), 64)
        with self.assertRaises(PermissionError):
            adapter.read_source_artifact(case.id)
        source = adapter.read_source_artifact(case.id, adapter.restricted_access())
        self.assertIn("answer", source)
        self.assertIn("solution", source)
        self.assertEqual(reference.sha256, adapter.source_artifact(case.id).sha256)

    def test_only_safe_sql_query_and_submit_tools_are_exposed(self):
        adapter = SecRLAdapter()
        self.assertEqual({tool.name for tool in adapter.tool_definitions()}, {"sql_query", "submit"})
        for query in ("SELECT 1", "SHOW TABLES", "EXPLAIN SELECT 1"):
            self.assertTrue(adapter.validate_sql(query))
        for query in (
            "SELECT 1; DROP TABLE users",
            "DELETE FROM users",
            "UPDATE users SET x=1",
            "INSERT INTO users VALUES (1)",
            "CREATE TABLE users (id INT)",
            "SELECT LOAD_FILE('/etc/passwd')",
            "SELECT * INTO OUTFILE '/tmp/x' FROM users",
            "SET GLOBAL max_connections=1",
            "SELECT 1;;",
        ):
            with self.assertRaises(UnsafeSQL):
                adapter.validate_sql(query)

    def test_scenario_mapping_is_incident_based(self):
        adapter = SecRLAdapter()
        cases = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())
        self.assertEqual({case.scenario.id for case in cases}, set(SECRL_EXPECTED_SCENARIO_COUNTS))
        self.assertTrue(all(case.public_input["incident"] in SECRL_EXPECTED_SCENARIO_COUNTS for case in cases))

    def test_case_identity_hashes_full_canonical_question_record(self):
        adapter = SecRLAdapter()
        case = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())[0]
        source = adapter.read_source_artifact(case.id, adapter.restricted_access())
        canonical = json.dumps(source, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        expected = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
        self.assertEqual(case.public_input["question_sha256"], expected)
        self.assertTrue(case.id.endswith(expected))

    def test_manifest_has_frozen_dataset_hash(self):
        adapter = SecRLAdapter()
        manifest = adapter.manifest()
        self.assertEqual(manifest.benchmark_id, "secrl")
        self.assertEqual(len(manifest.dataset_sha256), 64)
        self.assertEqual(manifest.dataset_sha256, adapter.dataset_ref().sha256)

    def test_missing_or_malformed_dataset_is_reported(self):
        adapter = SecRLAdapter()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.json"
            path.write_text("{}", encoding="utf-8")
            report = adapter.validate_dataset(path)
        self.assertFalse(report.valid)
        self.assertTrue(report.errors)

    def test_restricted_gold_is_not_serializable_as_public_case(self):
        adapter = SecRLAdapter()
        case = adapter.enumerate_cases(adapter.dataset_ref(), adapter.scope_all())[0]
        self.assertNotIn("gold", case.model_dump())
        self.assertNotIn("answer", case.model_dump())


if __name__ == "__main__":
    unittest.main()
