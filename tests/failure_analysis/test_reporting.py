import csv
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path

from experiments.failure_analysis.models import (
    Attribution,
    Evidence,
    FeatureRecord,
    MappedQuestion,
    OutputCollisionError,
    QuestionIdentity,
    ReviewError,
    SCHEMA_VERSION,
)
from experiments.failure_analysis.reporting import (
    apply_human_review,
    build_row,
    select_review_rows,
    write_outputs,
)


ROW_FIELDS = {
    "schema_version",
    "taxonomy_version",
    "incident",
    "question_index",
    "question_fingerprint_sha256",
    "question_text_fingerprint_sha256",
    "nodes",
    "control_status",
    "reward_official",
    "reward_bucket",
    "golden_answer",
    "submitted_answer",
    "agent_source_index",
    "env_source_index",
    "mapping_status",
    "log_complete",
    "sql_total",
    "sql_success",
    "sql_failure",
    "empty_result_count",
    "duplicate_query_count",
    "steps",
    "max_steps",
    "submitted",
    "submitted_at_step_limit",
    "gold_evidence_match",
    "gold_evidence_steps",
    "evaluator_fields_complete",
    "agent_prompt_tokens",
    "agent_completion_tokens",
    "agent_total_tokens",
    "evaluator_tokens",
    "primary_cause_candidate",
    "primary_cause_status",
    "secondary_cause_candidates",
    "confidence",
    "evidence",
    "needs_human_review",
    "human_review_reasons",
    "reviewed_primary",
    "reviewed_secondary",
    "review_status",
    "review_notes",
}

REVIEW_FIELDS = [
    "incident",
    "question_index",
    "question_fingerprint_sha256",
    "candidate_primary",
    "candidate_secondary",
    "reviewed_primary",
    "reviewed_secondary",
    "review_status",
    "review_notes",
]

CATEGORIES = [
    "DATA",
    "SQL_EXEC",
    "SQL_RETRIEVAL",
    "NAVIGATION",
    "LOOP",
    "STEP_LIMIT",
    "REASONING",
    "ANSWER",
    "EVALUATOR",
    "GOLD",
    "INFRA",
    "UNKNOWN",
]


def sha256_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def taxonomy():
    return {
        "taxonomy_version": "taxonomy_v1",
        "categories": list(CATEGORIES),
        "always_human_review": ["EVALUATOR", "GOLD", "UNKNOWN"],
        "loop_and_step_limit_normally_secondary": True,
        "review_sampling": {
            "seed": 20260720,
            "rate": 0.1,
            "minimum_per_nonempty_category": 1,
        },
        "calibration": [],
    }


def make_feature(index=0, reward=0.0):
    fingerprint = f"{index + 1:064x}"
    identity = QuestionIdentity(
        "incident_5",
        index,
        fingerprint,
        f"{index + 1001:064x}",
    )
    question = {
        "question": f"question-{index}",
        "answer": f"gold-{index}",
        "nodes": [f"node-{index}", f"node-{index + 1}"],
    }
    mapped = MappedQuestion(
        identity=identity,
        question=question,
        agent={
            "question_dict": question,
            "nodes": list(question["nodes"]),
            "reward": reward,
        },
        env={
            "question": question,
            "nodes": list(question["nodes"]),
            "reward": reward,
            "trajectory": [],
        },
        agent_source_index=index + 10,
        env_source_index=index + 20,
    )
    return FeatureRecord(
        mapped=mapped,
        reward_official=reward,
        submitted_answer=f"submitted-{index}",
        sql_total=3,
        sql_success=2,
        sql_failure=1,
        empty_result_count=1,
        duplicate_query_count=1,
        steps=15,
        max_steps=15,
        submitted=True,
        submitted_at_step_limit=True,
        gold_evidence_match="not_found",
        gold_evidence_steps=[],
        evaluator_fields_complete=True,
        agent_prompt_tokens=100,
        agent_completion_tokens=20,
        agent_total_tokens=120,
        evaluator_tokens=None,
        evidence=[
            Evidence(
                "sql_error",
                3,
                "env",
                "trajectory[2].observation",
                "ProgrammingError",
                False,
            )
        ],
    )


def make_attribution(primary="ANSWER", *, needs_review=False, confidence="medium"):
    return Attribution(
        primary_cause_candidate=primary,
        primary_cause_status="candidate",
        secondary_cause_candidates=["STEP_LIMIT"],
        confidence=confidence,
        needs_human_review=needs_review,
        human_review_reasons=["low_confidence"] if needs_review else [],
    )


def make_row(index=0, primary="ANSWER", *, needs_review=False, confidence="medium"):
    return build_row(
        make_feature(index=index, reward=0.4),
        make_attribution(
            primary,
            needs_review=needs_review,
            confidence=confidence,
        ),
        "taxonomy_v1",
    )


def write_review(path, records):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(records)


def review_record(row, **overrides):
    record = {
        "incident": row["incident"],
        "question_index": row["question_index"],
        "question_fingerprint_sha256": row["question_fingerprint_sha256"],
        "candidate_primary": row["primary_cause_candidate"],
        "candidate_secondary": json.dumps(
            row["secondary_cause_candidates"],
            separators=(",", ":"),
        ),
        "reviewed_primary": "REASONING",
        "reviewed_secondary": '["STEP_LIMIT"]',
        "review_status": "confirmed",
        "review_notes": "human checked",
    }
    record.update(overrides)
    return record


class ReportingTest(unittest.TestCase):
    def test_build_row_emits_the_frozen_record_contract(self):
        feature = make_feature(index=7, reward=0.4)
        attribution = make_attribution("ANSWER", needs_review=True, confidence="low")
        row = build_row(feature, attribution, "taxonomy_v1")

        self.assertEqual(set(row), ROW_FIELDS)
        self.assertEqual(row["schema_version"], SCHEMA_VERSION)
        self.assertEqual(row["taxonomy_version"], "taxonomy_v1")
        self.assertEqual(row["incident"], "incident_5")
        self.assertEqual(row["question_index"], 7)
        self.assertEqual(
            row["question_fingerprint_sha256"],
            feature.mapped.identity.question_fingerprint_sha256,
        )
        self.assertEqual(row["nodes"], ["node-7", "node-8"])
        self.assertEqual(row["reward_official"], 0.4)
        self.assertEqual(row["golden_answer"], "gold-7")
        self.assertEqual(row["submitted_answer"], "submitted-7")
        self.assertEqual(row["agent_source_index"], 17)
        self.assertEqual(row["env_source_index"], 27)
        self.assertEqual(row["sql_total"], 3)
        self.assertEqual(row["sql_success"], 2)
        self.assertEqual(row["sql_failure"], 1)
        self.assertEqual(row["primary_cause_candidate"], "ANSWER")
        self.assertEqual(row["secondary_cause_candidates"], ["STEP_LIMIT"])
        self.assertEqual(row["confidence"], "low")
        self.assertEqual(row["evidence"][0]["kind"], "sql_error")
        self.assertTrue(row["needs_human_review"])
        self.assertEqual(row["review_status"], "unreviewed")

    def test_write_outputs_are_consistent_hashed_and_exact(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "input-taxonomy.json"
            taxonomy_path.write_text(
                json.dumps(taxonomy(), sort_keys=True),
                encoding="utf-8",
            )
            source_paths = {}
            for name in ("agent", "env", "question"):
                path = root / f"{name}.json"
                path.write_text(f'{{"source":"{name}"}}\n', encoding="utf-8")
                source_paths[name] = path

            rows = [
                make_row(0, "ANSWER"),
                make_row(1, "UNKNOWN", needs_review=True, confidence="low"),
            ]
            output_dir = root / "report"
            written = write_outputs(
                rows,
                taxonomy_path,
                "incident_5",
                output_dir,
                source_paths,
                15,
                "abc123",
                False,
            )

            expected_names = {
                "taxonomy_v1.json",
                "incident_5_attribution.jsonl",
                "incident_5_attribution.csv",
                "incident_5_summary.md",
                "human_review.csv",
                "incident_5_analysis_manifest.json",
            }
            self.assertEqual({path.name for path in written}, expected_names)
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                expected_names,
            )

            jsonl_rows = [
                json.loads(line)
                for line in (
                    output_dir / "incident_5_attribution.jsonl"
                ).read_text(encoding="utf-8").splitlines()
            ]
            with (
                output_dir / "incident_5_attribution.csv"
            ).open(encoding="utf-8", newline="") as handle:
                csv_rows = list(csv.DictReader(handle))

            self.assertEqual(len(jsonl_rows), len(csv_rows))
            self.assertEqual(len(jsonl_rows), 2)
            self.assertEqual(
                sum(float(row["reward_official"]) for row in jsonl_rows),
                sum(float(row["reward_official"]) for row in csv_rows),
            )
            self.assertIsInstance(jsonl_rows[0]["nodes"], list)
            self.assertIsInstance(jsonl_rows[0]["evidence"], list)
            self.assertIsInstance(csv_rows[0]["nodes"], str)
            self.assertEqual(json.loads(csv_rows[0]["nodes"]), ["node-0", "node-1"])

            summary = (
                output_dir / "incident_5_summary.md"
            ).read_text(encoding="utf-8")
            self.assertIn("2", summary)
            self.assertIn("0.4", summary)
            self.assertIn("ANSWER", summary)
            self.assertIn("UNKNOWN", summary)
            self.assertIn("official", summary.lower())
            self.assertIn("candidate", summary.lower())

            with (
                output_dir / "human_review.csv"
            ).open(encoding="utf-8", newline="") as handle:
                review_rows = list(csv.DictReader(handle))
                self.assertEqual(handle.closed, False)
            self.assertEqual(list(review_rows[0]), REVIEW_FIELDS)

            manifest = json.loads(
                (
                    output_dir / "incident_5_analysis_manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(
                set(manifest),
                {
                    "schema_version",
                    "taxonomy_version",
                    "incident",
                    "max_steps",
                    "generated_at_utc",
                    "tool_version",
                    "git_commit",
                    "review_applied",
                    "record_count",
                    "mapping_counts",
                    "sources",
                    "outputs",
                },
            )
            self.assertEqual(manifest["record_count"], 2)
            self.assertEqual(
                manifest["mapping_counts"],
                {"agent": 2, "env": 2, "question": 2},
            )
            self.assertTrue(manifest["generated_at_utc"].endswith("Z"))
            self.assertNotIn("incident_5_analysis_manifest.json", manifest["outputs"])

            expected_manifest_outputs = expected_names - {
                "incident_5_analysis_manifest.json"
            }
            self.assertEqual(set(manifest["outputs"]), expected_manifest_outputs)

            for name, item in manifest["sources"].items():
                self.assertEqual(Path(item["path"]), source_paths[name])
                self.assertEqual(item["sha256"], sha256_file(source_paths[name]))

            for filename, item in manifest["outputs"].items():
                self.assertEqual(item["filename"], filename)
                path = output_dir / filename
                self.assertEqual(item["sha256"], sha256_file(path))

    def test_existing_output_directory_or_target_file_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(json.dumps(taxonomy()), encoding="utf-8")
            source = root / "source.json"
            source.write_text("{}", encoding="utf-8")
            source_paths = {"agent": source, "env": source, "question": source}

            for make_target in (
                lambda path: path.mkdir(),
                lambda path: path.write_text("user data", encoding="utf-8"),
            ):
                with self.subTest(target_type=make_target.__code__.co_firstlineno):
                    output_dir = root / f"target-{make_target.__code__.co_firstlineno}"
                    make_target(output_dir)
                    with self.assertRaises(OutputCollisionError):
                        write_outputs(
                            [make_row()],
                            taxonomy_path,
                            "incident_5",
                            output_dir,
                            source_paths,
                            15,
                            None,
                            False,
                        )

    def test_serialization_failure_leaves_no_output_or_temporary_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text(json.dumps(taxonomy()), encoding="utf-8")
            source = root / "source.json"
            source.write_text("{}", encoding="utf-8")
            source_paths = {"agent": source, "env": source, "question": source}
            row = make_row()
            row["evidence"] = [object()]
            before = {path.name for path in root.iterdir()}

            with self.assertRaises(TypeError):
                write_outputs(
                    [row],
                    taxonomy_path,
                    "incident_5",
                    root / "report",
                    source_paths,
                    15,
                    None,
                    False,
                )

            self.assertFalse((root / "report").exists())
            self.assertEqual({path.name for path in root.iterdir()}, before)

    def test_review_identity_mismatch_is_rejected(self):
        rows = [make_row(1)]
        with tempfile.TemporaryDirectory() as temporary:
            review_path = Path(temporary) / "review.csv"
            record = review_record(rows[0], question_index=999)
            write_review(review_path, [record])
            with self.assertRaises(ReviewError):
                apply_human_review(rows, review_path, taxonomy())

    def test_valid_review_changes_only_reviewed_fields(self):
        rows = [make_row(2)]
        original = deepcopy(rows[0])
        with tempfile.TemporaryDirectory() as temporary:
            review_path = Path(temporary) / "review.csv"
            write_review(review_path, [review_record(rows[0])])
            apply_human_review(rows, review_path, taxonomy())

        reviewed_fields = {
            "reviewed_primary",
            "reviewed_secondary",
            "review_status",
            "review_notes",
        }
        for key, value in original.items():
            if key not in reviewed_fields:
                self.assertEqual(rows[0][key], value, key)
        self.assertEqual(rows[0]["reviewed_primary"], "REASONING")
        self.assertEqual(rows[0]["reviewed_secondary"], ["STEP_LIMIT"])
        self.assertEqual(rows[0]["review_status"], "confirmed")
        self.assertEqual(rows[0]["review_notes"], "human checked")
        self.assertEqual(rows[0]["primary_cause_candidate"], "ANSWER")
        self.assertEqual(rows[0]["reward_official"], 0.4)

    def test_invalid_review_rows_are_rejected(self):
        base_row = make_row(3)
        cases = {
            "duplicate identity": [
                review_record(base_row),
                review_record(base_row),
            ],
            "invalid primary category": [
                review_record(base_row, reviewed_primary="NOT_A_CATEGORY"),
            ],
            "invalid secondary category": [
                review_record(base_row, reviewed_secondary='["NOT_A_CATEGORY"]'),
            ],
            "malformed secondary json": [
                review_record(base_row, reviewed_secondary="STEP_LIMIT"),
            ],
            "invalid fingerprint": [
                review_record(base_row, question_fingerprint_sha256="short"),
            ],
        }
        for name, records in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                rows = [deepcopy(base_row)]
                review_path = Path(temporary) / "review.csv"
                write_review(review_path, records)
                with self.assertRaises(ReviewError):
                    apply_human_review(rows, review_path, taxonomy())

    def test_review_selection_is_deterministic_stratified_and_mandatory(self):
        rows = []
        rows.extend(make_row(index, "ANSWER") for index in range(0, 20))
        rows.extend(make_row(index, "NAVIGATION") for index in range(20, 40))
        rows.append(make_row(40, "GOLD"))
        rows.append(make_row(41, "SQL_EXEC", confidence="low"))
        integrity = make_row(42, "DATA")
        integrity["human_review_reasons"] = ["integrity:log_incomplete"]
        rows.append(integrity)

        forward = select_review_rows(deepcopy(rows), taxonomy())
        reverse = select_review_rows(list(reversed(deepcopy(rows))), taxonomy())
        identity = lambda row: (
            row["incident"],
            row["question_index"],
            row["question_fingerprint_sha256"],
        )

        self.assertEqual([identity(row) for row in forward], [identity(row) for row in reverse])
        self.assertEqual([identity(row) for row in forward], sorted(identity(row) for row in forward))
        selected_indexes = {row["question_index"] for row in forward}
        self.assertTrue({40, 41, 42}.issubset(selected_indexes))
        self.assertEqual(
            sum(row["primary_cause_candidate"] == "ANSWER" for row in forward),
            2,
        )
        self.assertEqual(
            sum(row["primary_cause_candidate"] == "NAVIGATION" for row in forward),
            2,
        )
        self.assertEqual(len(forward), 7)


if __name__ == "__main__":
    unittest.main()
