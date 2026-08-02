from __future__ import annotations

import csv
import hashlib
import json
import tempfile
import unittest
from copy import deepcopy
from pathlib import Path
from unittest import mock

from experiments.failure_analysis.models import InputError, OutputCollisionError
from experiments.failure_analysis.retrieval_review import select_low_confidence_rows
import experiments.failure_analysis.retrieval_reporting as retrieval_reporting
from experiments.failure_analysis.retrieval_reporting import write_retrieval_outputs


def _row(index: int, *, confidence: str = "high", boundary: str = "NONE") -> dict[str, object]:
    return {
        "incident": "incident_10" if index else "incident_2",
        "question_index": index,
        "question_fingerprint_sha256": f"{index + 1:064x}",
        "question_text_fingerprint_sha256": f"{index + 101:064x}",
        "question": f"Question {index}",
        "context": "Context",
        "golden_answer": ["gold", index],
        "golden_solution": {"sql": "SELECT 1"},
        "submitted_answer": "answer",
        "trajectory_steps": 1,
        "submitted": True,
        "submitted_at_step_limit": False,
        "reward_official": 1.0 if index == 0 else 0.0,
        "reviewed_primary_original": "SQL_RETRIEVAL",
        "review_notes_original": "",
        "agent_source_index": 0,
        "env_source_index": 0,
        "agent_source_sha256": "a" * 64,
        "env_source_sha256": "b" * 64,
        "question_source_sha256": "c" * 64,
        "query_steps": [{"step": 1, "sql": "SELECT 1", "observation": "1", "query_success": True}],
        "retrieval_primary_subtype": "SOURCE_SELECTION",
        "auxiliary_tags": ["WRONG_TABLE"],
        "retrieval_outcome": "WRONG_ROW",
        "boundary_flag": boundary,
        "confidence": confidence,
        "decision_status": "needs_review" if confidence == "low" else "reviewed",
        "first_divergence_step": 1,
        "relevant_sql_steps": [1],
        "sql_evidence": "bad source",
        "observation_evidence": "wrong row",
        "gold_evidence_basis": "gold",
        "rationale": "rationale",
        "schema_version": "sql_retrieval_subtyping_v1",
        "overlay_taxonomy_version": "sql_retrieval_taxonomy_v1",
    }


def _inputs(root: Path) -> tuple[dict[str, Path], dict[str, str]]:
    incidents = (5, 38, 34, 39, 55, 134, 166, 322)
    names = ["aggregate_csv", "completed_review_csv", "taxonomy", "evidence_jsonl"]
    names.extend(f"manifest_incident_{incident}" for incident in incidents)
    for incident in incidents:
        names.extend(
            [
                f"agent_incident_{incident}",
                f"env_incident_{incident}",
                f"question_incident_{incident}",
            ]
        )
    paths: dict[str, Path] = {}
    hashes: dict[str, str] = {}
    for name in names:
        path = root / f"{name}.json"
        path.write_text(name, encoding="utf-8")
        paths[name] = path.resolve()
        hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return paths, hashes


class RetrievalReportingTest(unittest.TestCase):
    def test_writes_exactly_five_atomic_outputs_and_consistent_rows(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            rows = [_row(1, confidence="low"), _row(0)]
            before = deepcopy(rows)
            queue = select_low_confidence_rows(rows)
            output_dir = root / "report"

            result = write_retrieval_outputs(rows, queue, output_dir, paths, hashes, "abc123")

            self.assertEqual({path.name for path in result}, {
                "sql_retrieval_subtypes.csv",
                "sql_retrieval_subtypes.jsonl",
                "sql_retrieval_subtypes_summary.md",
                "low_confidence_review_queue.csv",
                "analysis_manifest.json",
            })
            self.assertEqual({path.name for path in output_dir.iterdir()}, {path.name for path in result})
            self.assertEqual(rows, before)
            json_rows = [json.loads(line) for line in (output_dir / "sql_retrieval_subtypes.jsonl").read_text().splitlines()]
            with (output_dir / "sql_retrieval_subtypes.csv").open(newline="", encoding="utf-8") as handle:
                csv_rows = list(csv.DictReader(handle))
            self.assertEqual([(r["incident"], r["question_index"]) for r in json_rows], [("incident_2", 0), ("incident_10", 1)])
            self.assertEqual(len(csv_rows), len(json_rows))
            self.assertEqual(json.loads(csv_rows[0]["auxiliary_tags"]), json_rows[0]["auxiliary_tags"])
            self.assertEqual(json_rows[1]["decision_status"], "needs_review")

            manifest = json.loads((output_dir / "analysis_manifest.json").read_text())
            self.assertEqual(manifest["record_count"], 2)
            self.assertEqual(manifest["output_count"], 5)
            self.assertEqual(manifest["git_commit"], "abc123")
            self.assertEqual(manifest["input_manifest"]["aggregate_csv"]["path"], str(paths["aggregate_csv"]))
            for name, digest in manifest["output_hashes"].items():
                self.assertEqual(digest, hashlib.sha256((output_dir / name).read_bytes()).hexdigest())

    def test_summary_has_deterministic_counts_and_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            rows = [_row(0), _row(1, confidence="low", boundary="SQL_EXEC_POSSIBLE")]
            write_retrieval_outputs(rows, select_low_confidence_rows(rows), root / "report", paths, hashes, None)
            summary = (root / "report" / "sql_retrieval_subtypes_summary.md").read_text()
            self.assertIn("Total records: 2", summary)
            self.assertIn("incident_2", summary)
            self.assertIn("incident_10", summary)
            self.assertIn("SQL_EXEC_POSSIBLE", summary)
            self.assertIn("Low-confidence review queue count: 1", summary)
            self.assertIn("does not alter official scoring", summary)

    def test_rejects_bad_provenance_and_collision_without_touching_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            rows = [_row(0)]
            with self.assertRaises(InputError):
                write_retrieval_outputs(rows, [], root / "bad", paths, {"extra": "0" * 64}, None)
            target = root / "target"
            target.mkdir()
            (target / "sentinel").write_text("keep", encoding="utf-8")
            with self.assertRaises(OutputCollisionError):
                write_retrieval_outputs(rows, [], target, paths, hashes, None)
            self.assertEqual((target / "sentinel").read_text(), "keep")

    def test_rejects_frozen_schema_missing_or_reordered_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            row = _row(0)
            missing = dict(row)
            missing.pop("context")
            with self.assertRaises(InputError):
                write_retrieval_outputs([missing], [], root / "missing", paths, hashes, None)
            reordered = {key: row[key] for key in reversed(tuple(row))}
            with self.assertRaises(InputError):
                write_retrieval_outputs([reordered], [], root / "reordered", paths, hashes, None)

    def test_rejects_bool_numeric_fields_and_invalid_query_steps(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            for field, value in (
                ("reward_official", True),
                ("agent_source_index", True),
                ("trajectory_steps", True),
            ):
                row = _row(0)
                row[field] = value
                with self.subTest(field=field), self.assertRaises(InputError):
                    write_retrieval_outputs([row], [], root / field, paths, hashes, None)
            row = _row(0)
            row["query_steps"] = [{"step": 0, "sql": "SELECT 1", "observation": "1", "query_success": True}]
            with self.assertRaises(InputError):
                write_retrieval_outputs([row], [], root / "query-steps", paths, hashes, None)

    def test_reopens_large_csv_fields_with_bounded_limit_and_restores(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            row = _row(0)
            large_evidence = "e" * 131073
            row["sql_evidence"] = large_evidence
            previous_csv_limit = csv.field_size_limit()
            csv.field_size_limit(131072)
            try:
                with mock.patch.object(
                    retrieval_reporting.csv,
                    "field_size_limit",
                    wraps=csv.field_size_limit,
                ) as field_size_limit:
                    write_retrieval_outputs(
                        [row], [], root / "large", paths, hashes, None
                    )
                bumped_limits = [
                    call.args[0]
                    for call in field_size_limit.call_args_list
                    if call.args and call.args[0] > 131072
                ]
                self.assertTrue(bumped_limits)
                self.assertLessEqual(max(bumped_limits), len(large_evidence) + 10000)
                self.assertEqual(csv.field_size_limit(), 131072)
            finally:
                csv.field_size_limit(previous_csv_limit)

    def test_provenance_key_set_is_frozen_and_dangling_target_collides(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            row = _row(0)
            missing_paths = dict(paths)
            missing_hashes = dict(hashes)
            missing_paths.pop("taxonomy")
            missing_hashes.pop("taxonomy")
            with self.assertRaises(InputError):
                write_retrieval_outputs([row], [], root / "missing-provenance", missing_paths, missing_hashes, None)
            extra_paths = dict(paths)
            extra_hashes = dict(hashes)
            extra = root / "extra.json"
            extra.write_text("extra", encoding="utf-8")
            extra_paths["extra"] = extra.resolve()
            extra_hashes["extra"] = hashlib.sha256(extra.read_bytes()).hexdigest()
            with self.assertRaises(InputError):
                write_retrieval_outputs([row], [], root / "extra-provenance", extra_paths, extra_hashes, None)
            target = root / "dangling"
            target.symlink_to(root / "missing-target")
            with self.assertRaises(OutputCollisionError):
                write_retrieval_outputs([row], [], target, paths, hashes, None)
            self.assertTrue(target.is_symlink())

    def test_symlink_loop_in_input_provenance_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            first = root / "loop-a"
            second = root / "loop-b"
            first.symlink_to(second)
            second.symlink_to(first)
            paths["taxonomy"] = first
            hashes["taxonomy"] = "0" * 64
            with self.assertRaises(InputError):
                write_retrieval_outputs([_row(0)], [], root / "loop-output", paths, hashes, None)

    def test_nul_output_path_is_input_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            with self.assertRaises(InputError):
                write_retrieval_outputs([_row(0)], [], Path(f"{root}/bad\x00output"), paths, hashes, None)

    def test_atomic_publish_collision_preserves_sentinel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            target = root / "raced"
            def race(source: Path, destination: Path) -> None:
                target.mkdir()
                (target / "sentinel").write_text("user", encoding="utf-8")
                raise FileExistsError(17, "target appeared", str(target))

            with mock.patch.object(retrieval_reporting, "_atomic_rename_noreplace", race):
                with self.assertRaises(OutputCollisionError):
                    write_retrieval_outputs([_row(0)], [], target, paths, hashes, None)
            self.assertEqual((target / "sentinel").read_text(), "user")
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".raced.")], [])

    def test_atomic_publish_collision_preserves_empty_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            target = root / "replaced"
            def replace_then_publish(source: Path, destination: Path) -> None:
                target.mkdir()
                raise FileExistsError(17, "target appeared", str(target))

            with mock.patch.object(retrieval_reporting, "_atomic_rename_noreplace", replace_then_publish):
                with self.assertRaises(OutputCollisionError):
                    write_retrieval_outputs([_row(0)], [], target, paths, hashes, None)
            self.assertTrue(target.is_dir())
            self.assertEqual(list(target.iterdir()), [])
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".replaced.")], [])

    def test_atomic_publish_collision_preserves_unknown_user_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            target = root / "occupied"
            def occupy_then_publish(source: Path, destination: Path) -> None:
                target.mkdir()
                (target / "sql_retrieval_subtypes.csv").write_text("user", encoding="utf-8")
                raise FileExistsError(17, "target appeared", str(target))

            with mock.patch.object(retrieval_reporting, "_atomic_rename_noreplace", occupy_then_publish):
                with self.assertRaises(OutputCollisionError):
                    write_retrieval_outputs([_row(0)], [], target, paths, hashes, None)
            self.assertEqual(
                (target / "sql_retrieval_subtypes.csv").read_text(),
                "user",
            )

    def test_review_queue_must_match_policy_and_serialization_failure_cleans_temp(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            paths, hashes = _inputs(root)
            rows = [_row(0)]
            with self.assertRaises(InputError):
                write_retrieval_outputs(rows, [_row(0, confidence="low")], root / "bad-queue", paths, hashes, None)
            with mock.patch("experiments.failure_analysis.retrieval_reporting.json.dumps", side_effect=TypeError("boom")):
                with self.assertRaises(InputError):
                    write_retrieval_outputs(rows, [], root / "failed", paths, hashes, None)
            self.assertFalse((root / "failed").exists())
            self.assertEqual([p.name for p in root.iterdir() if p.name.startswith(".failed.")], [])

            def fail_write(*args: object, **kwargs: object) -> None:
                raise OSError("write failed")

            with mock.patch("experiments.failure_analysis.retrieval_reporting._write_text", side_effect=fail_write):
                with self.assertRaises(InputError):
                    write_retrieval_outputs(rows, [], root / "write-failed", paths, hashes, None)
            self.assertFalse((root / "write-failed").exists())

            with (
                mock.patch("experiments.failure_analysis.retrieval_reporting._write_text", side_effect=fail_write),
                mock.patch("experiments.failure_analysis.retrieval_reporting.shutil.rmtree", side_effect=OSError("cleanup failed")),
            ):
                with self.assertRaises(InputError):
                    write_retrieval_outputs(rows, [], root / "cleanup-failed", paths, hashes, None)


if __name__ == "__main__":
    unittest.main()
