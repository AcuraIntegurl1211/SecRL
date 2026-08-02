from __future__ import annotations

import argparse
import io
import json
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path
from unittest.mock import patch

from experiments.failure_analysis.models import (
    InputError,
    MappingError,
    OutputCollisionError,
)
from experiments.failure_analysis.retrieval_models import QueryStep, RetrievalEvidenceBundle
from experiments.failure_analysis.retrieval_models import thaw_json_value
from experiments.failure_analysis.retrieval_extract import SourceSpec


def _bundle(index: int = 0) -> RetrievalEvidenceBundle:
    return RetrievalEvidenceBundle(
        incident="incident_5",
        question_index=index,
        question_fingerprint_sha256=(f"{index:064x}")[-64:],
        question_text_fingerprint_sha256="b" * 64,
        question="Which service failed?",
        context="context",
        golden_answer={"answer": "service"},
        golden_solution={"solution": "inspect"},
        submitted_answer="service",
        trajectory_steps=2,
        submitted=True,
        submitted_at_step_limit=False,
        reward_official=0.0,
        reviewed_primary_original="SQL_RETRIEVAL",
        review_notes_original="",
        agent_source_index=index,
        env_source_index=index,
        agent_source_sha256="c" * 64,
        env_source_sha256="d" * 64,
        question_source_sha256="e" * 64,
        query_steps=(QueryStep(1, "SELECT 1", "service", True),),
    )


class SqlRetrievalCliTest(unittest.TestCase):
    def test_parse_prepare_requires_exactly_eight_manifests(self):
        from experiments.failure_analysis.analyze_sql_retrieval import parse_args

        with self.assertRaises(SystemExit):
            parse_args(["prepare", "--aggregate-csv", "a", "--source-repo-root", "r", "--taxonomy", "t", "--work-dir", "w"])
        args = parse_args(
            [
                "prepare",
                "--aggregate-csv", "a",
                *sum((["--manifest", f"m{i}"] for i in range(8)), []),
                "--source-repo-root", "r",
                "--taxonomy", "t",
                "--work-dir", "w",
            ]
        )
        self.assertEqual(args.mode, "prepare")
        self.assertEqual(len(args.manifest), 8)

    def test_load_evidence_rejects_missing_extra_duplicate_and_type_drift(self):
        from experiments.failure_analysis.analyze_sql_retrieval import _load_evidence_bundles

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "evidence.jsonl"
            bundle = _bundle()
            payload = {field.name: getattr(bundle, field.name) for field in fields(bundle)}
            payload["golden_answer"] = thaw_json_value(payload["golden_answer"])
            payload["golden_solution"] = thaw_json_value(payload["golden_solution"])
            payload["reward_official"] = float(payload["reward_official"])
            payload["query_steps"] = [{"step": 1, "sql": "SELECT 1", "observation": "service", "query_success": True}]
            path.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
            self.assertEqual(_load_evidence_bundles(path), [_bundle()])
            path.write_text(json.dumps({**payload, "extra": 1}) + "\n", encoding="utf-8")
            with self.assertRaises(MappingError):
                _load_evidence_bundles(path)
            path.write_text(json.dumps({**payload, "trajectory_steps": True}) + "\n", encoding="utf-8")
            with self.assertRaises(MappingError):
                _load_evidence_bundles(path)

    def test_compare_evidence_requires_exact_bundle_values(self):
        from experiments.failure_analysis.analyze_sql_retrieval import _compare_evidence_bundles

        with self.assertRaises(MappingError):
            _compare_evidence_bundles([_bundle()], [_bundle(1)])

        fresh = replace(_bundle(), golden_answer={"nested": {"value": True}})
        evidence = replace(_bundle(), golden_answer={"nested": {"value": 1}})
        with self.assertRaises(MappingError):
            _compare_evidence_bundles([fresh], [evidence])

    def test_finalize_provenance_uses_frozen_36_key_names(self):
        from experiments.failure_analysis.analyze_sql_retrieval import (
            EXPECTED_COUNTS,
            _source_provenance,
        )
        from experiments.failure_analysis.retrieval_reporting import _INPUT_KEYS

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            def make(name: str) -> Path:
                path = root / name
                path.write_text(name, encoding="utf-8")
                return path.resolve()

            aggregate = make("aggregate.csv")
            review = make("review.csv")
            taxonomy = make("taxonomy.json")
            evidence = make("evidence.jsonl")
            manifests = {incident: make(f"{incident}-manifest.json") for incident in EXPECTED_COUNTS}
            specs = {
                incident: SourceSpec(
                    incident=incident,
                    agent_path=make(f"{incident}-agent.json"),
                    env_path=make(f"{incident}-env.json"),
                    question_path=make(f"{incident}-question.json"),
                    agent_sha256="",
                    env_sha256="",
                    question_sha256="",
                )
                for incident in EXPECTED_COUNTS
            }
            paths, hashes = _source_provenance(
                aggregate, review, taxonomy, evidence, manifests, specs
            )
            self.assertEqual(set(paths), set(_INPUT_KEYS))
            self.assertEqual(set(hashes), set(_INPUT_KEYS))
            self.assertIn("manifest_incident_5", paths)
            self.assertIn("agent_incident_322", paths)

            specs["incident_5"].agent_path.unlink()
            with self.assertRaises(MappingError):
                _source_provenance(aggregate, review, taxonomy, evidence, manifests, specs)

    def test_duplicate_manifest_identity_is_mapping_error(self):
        from experiments.failure_analysis.analyze_sql_retrieval import (
            EXPECTED_COUNTS,
            _manifest_incidents,
        )

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "manifest.json"
            path.write_text(json.dumps({"incident": "incident_5"}), encoding="utf-8")
            with self.assertRaises(MappingError):
                _manifest_incidents([path] * 8, {incident: object() for incident in EXPECTED_COUNTS})

    def test_main_maps_only_analysis_errors(self):
        from experiments.failure_analysis.analyze_sql_retrieval import main

        with patch("experiments.failure_analysis.analyze_sql_retrieval.parse_args", return_value=argparse.Namespace(mode="prepare")), patch(
            "experiments.failure_analysis.analyze_sql_retrieval.run", side_effect=InputError("bad")
        ):
            stderr = io.StringIO()
            with patch("sys.stderr", stderr):
                self.assertEqual(main([]), 2)

        with patch("experiments.failure_analysis.analyze_sql_retrieval.parse_args", return_value=argparse.Namespace()), patch(
            "experiments.failure_analysis.analyze_sql_retrieval.run", side_effect=RuntimeError("boom")
        ):
            with self.assertRaises(RuntimeError):
                main([])

    def test_prepare_builds_once_and_publishes_only_two_files(self):
        from experiments.failure_analysis.analyze_sql_retrieval import _run_prepare

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            work_dir = root / "work"
            args = argparse.Namespace(work_dir=work_dir)
            fake = (root / "aggregate.csv", {}, {}, {}, [_bundle()])
            with patch("experiments.failure_analysis.analyze_sql_retrieval._build_fresh", return_value=fake) as build, patch(
                "experiments.failure_analysis.analyze_sql_retrieval.write_preparation_files"
            ) as write:
                result = _run_prepare(args)
            build.assert_called_once_with(args)
            write.assert_called_once_with([_bundle()], work_dir / "evidence_bundles.jsonl", work_dir / "review_template.csv")
            self.assertEqual(result, [work_dir / "evidence_bundles.jsonl", work_dir / "review_template.csv"])
            self.assertFalse(work_dir.exists())

    def test_source_extraction_input_error_is_mapping_error(self):
        from experiments.failure_analysis.analyze_sql_retrieval import _build_fresh

        with tempfile.TemporaryDirectory() as directory:
            aggregate = Path(directory) / "aggregate.csv"
            aggregate.write_text("bytes", encoding="utf-8")
            taxonomy = Path(directory) / "taxonomy.json"
            taxonomy.write_text("{}", encoding="utf-8")
            with patch(
                "experiments.failure_analysis.analyze_sql_retrieval._load_inputs",
                return_value=(aggregate, {}, {}, {}),
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._build_input_provenance",
                return_value=({}, {}),
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.load_reviewed_rows",
                return_value=[],
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.build_evidence_bundles",
                side_effect=InputError("source SHA-256 mismatch for env.json"),
            ):
                with self.assertRaises(MappingError):
                    _build_fresh(argparse.Namespace(taxonomy=taxonomy))

    def test_stale_input_snapshot_blocks_before_aggregate_rows_are_loaded(self):
        from experiments.failure_analysis.analyze_sql_retrieval import _build_fresh

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate = root / "aggregate.csv"
            aggregate.write_text("bytes", encoding="utf-8")
            taxonomy = root / "taxonomy.json"
            taxonomy.write_text("{}", encoding="utf-8")
            args = argparse.Namespace(taxonomy=taxonomy)
            old_paths = {"aggregate_csv": aggregate.resolve()}
            old_hashes = {"aggregate_csv": "a" * 64}
            new_paths = {"aggregate_csv": aggregate.resolve()}
            new_hashes = {"aggregate_csv": "b" * 64}

            def loaded(namespace):
                namespace._retrieval_build_snapshot = (old_paths, old_hashes)
                return aggregate, {}, {}, {}

            with patch(
                "experiments.failure_analysis.analyze_sql_retrieval._load_inputs",
                side_effect=loaded,
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._build_input_provenance",
                return_value=(new_paths, new_hashes),
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.load_reviewed_rows",
            ) as load_rows:
                with self.assertRaises(MappingError):
                    _build_fresh(args)
            load_rows.assert_not_called()

    def test_final_provenance_hash_drift_is_mapping_error(self):
        from experiments.failure_analysis.analyze_sql_retrieval import _run_finalize

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence_path = root / "evidence.jsonl"
            review_path = root / "review.csv"
            evidence_path.write_text("{}\n", encoding="utf-8")
            review_path.write_text("header\n", encoding="utf-8")
            taxonomy_path = root / "taxonomy.json"
            taxonomy_path.write_text("{}\n", encoding="utf-8")
            output = root / "nested" / "report"
            args = argparse.Namespace(
                output_dir=output,
                evidence_jsonl=evidence_path,
                completed_review_csv=review_path,
                taxonomy=taxonomy_path,
            )
            fresh = [_bundle()]
            with patch(
                "experiments.failure_analysis.analyze_sql_retrieval._build_fresh_with_hashes",
                return_value=(root / "aggregate.csv", {}, {}, {}, fresh, {}, {}),
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._load_evidence_bundles",
                return_value=fresh,
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._compare_evidence_bundles",
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.apply_completed_review",
                return_value=[{"incident": "incident_5", "question_index": 0, "question_fingerprint_sha256": "0" * 64}],
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.select_low_confidence_rows",
                return_value=[],
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._source_provenance",
                return_value=({}, {}),
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.write_retrieval_outputs",
                side_effect=InputError("input SHA-256 mismatch for source.json"),
            ):
                with self.assertRaises(MappingError):
                    _run_finalize(args)

    def test_post_build_aggregate_hash_drift_blocks_publication(self):
        from experiments.failure_analysis.analyze_sql_retrieval import _run_finalize

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            aggregate = root / "aggregate.csv"
            aggregate.write_text("aggregate", encoding="utf-8")
            evidence_path = root / "evidence.jsonl"
            review_path = root / "review.csv"
            taxonomy_path = root / "taxonomy.json"
            evidence_path.write_text("{}\n", encoding="utf-8")
            review_path.write_text("header\n", encoding="utf-8")
            taxonomy_path.write_text("{}\n", encoding="utf-8")
            args = argparse.Namespace(
                output_dir=root / "report",
                evidence_jsonl=evidence_path,
                completed_review_csv=review_path,
                taxonomy=taxonomy_path,
            )
            fresh = [_bundle()]
            build_paths = {"aggregate_csv": aggregate.resolve()}
            build_hashes = {"aggregate_csv": "a" * 64}
            with patch(
                "experiments.failure_analysis.analyze_sql_retrieval._build_fresh_with_hashes",
                return_value=(aggregate, {}, {}, {}, fresh, build_paths, build_hashes),
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._load_evidence_bundles",
                return_value=fresh,
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._compare_evidence_bundles",
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.apply_completed_review",
                return_value=[{"incident": "incident_5", "question_index": 0, "question_fingerprint_sha256": "0" * 64}],
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.select_low_confidence_rows",
                return_value=[],
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval._source_provenance",
                return_value=({"aggregate_csv": aggregate.resolve()}, {"aggregate_csv": "b" * 64}),
            ), patch(
                "experiments.failure_analysis.analyze_sql_retrieval.write_retrieval_outputs",
            ) as write:
                with self.assertRaises(MappingError):
                    _run_finalize(args)
            write.assert_not_called()


if __name__ == "__main__":
    unittest.main()
