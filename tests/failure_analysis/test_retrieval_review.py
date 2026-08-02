from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import fields, replace
from pathlib import Path

from experiments.failure_analysis.models import ReviewError
from experiments.failure_analysis.retrieval_models import (
    QueryStep,
    RetrievalDecision,
    RetrievalEvidenceBundle,
    thaw_json_value,
)
from experiments.failure_analysis.retrieval_review import (
    apply_completed_review,
    load_overlay_taxonomy,
    select_low_confidence_rows,
)


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY_PATH = ROOT / "experiments/failure_analysis/sql_retrieval_taxonomy_v1.json"
EVIDENCE_FIELDS = [field.name for field in fields(RetrievalEvidenceBundle)]
DECISION_FIELDS = [field.name for field in fields(RetrievalDecision)]
REVIEW_FIELDS = EVIDENCE_FIELDS + DECISION_FIELDS


def _bundle(incident: str = "incident_2", question_index: int = 0):
    return replace(
        RetrievalEvidenceBundle.fixture_for_test(),
        incident=incident,
        question_index=question_index,
    )


def _serialize(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "True" if value else "False"
    if isinstance(value, (dict, list, tuple)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return str(value)


def _bundle_cells(bundle: RetrievalEvidenceBundle) -> dict[str, str]:
    cells: dict[str, str] = {}
    for field in EVIDENCE_FIELDS:
        value = getattr(bundle, field)
        if field in {"golden_answer", "golden_solution"}:
            value = thaw_json_value(value)
        elif field == "query_steps":
            value = [
                {
                    "step": item.step,
                    "sql": item.sql,
                    "observation": item.observation,
                    "query_success": item.query_success,
                }
                for item in value
            ]
        cells[field] = _serialize(value)
    return cells


def _decision_cells(**overrides: object) -> dict[str, str]:
    values: dict[str, object] = {
        "retrieval_primary_subtype": "TEMPORAL_SCOPE",
        "auxiliary_tags": ["WRONG_TIME"],
        "retrieval_outcome": "WRONG_ROW",
        "boundary_flag": "NONE",
        "confidence": "high",
        "decision_status": "reviewed",
        "first_divergence_step": 1,
        "relevant_sql_steps": [1],
        "sql_evidence": "step=1 sql=SELECT service",
        "observation_evidence": "step=1 observation=example-service",
        "gold_evidence_basis": "gold answer comparison",
        "rationale": "The time predicate diverges from the requested interval.",
    }
    values.update(overrides)
    return {field: _serialize(values[field]) for field in DECISION_FIELDS}


def _write_csv(path: Path, bundles: list[RetrievalEvidenceBundle], **overrides: object) -> None:
    rows: list[dict[str, str]] = []
    for bundle in bundles:
        row = _bundle_cells(bundle)
        row.update(_decision_cells(**overrides))
        rows.append(row)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


class RetrievalReviewTest(unittest.TestCase):
    def test_load_overlay_taxonomy_validates_and_returns_taxonomy(self):
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        self.assertEqual(taxonomy["version"], "sql_retrieval_taxonomy_v1")
        for field in (
            "primary_subtypes",
            "auxiliary_tags",
            "outcomes",
            "boundary_flags",
            "confidence",
            "decision_statuses",
        ):
            self.assertTrue(taxonomy[field])

    def test_apply_completed_review_merges_valid_temporal_scope_without_mutating_bundle(self):
        bundle = _bundle()
        before = bundle
        with tempfile.TemporaryDirectory() as directory:
            review_path = Path(directory) / "review.csv"
            _write_csv(review_path, [bundle])
            merged = apply_completed_review(
                [bundle], review_path, load_overlay_taxonomy(TAXONOMY_PATH)
            )

        self.assertEqual(bundle, before)
        self.assertEqual(len(merged), 1)
        row = merged[0]
        self.assertEqual(row["incident"], "incident_2")
        self.assertEqual(row["question_index"], 0)
        self.assertEqual(row["reward_official"], bundle.reward_official)
        self.assertEqual(row["query_steps"], [{
            "step": 1,
            "sql": "SELECT service FROM events LIMIT 1",
            "observation": "example-service",
            "query_success": True,
        }])
        self.assertEqual(row["retrieval_primary_subtype"], "TEMPORAL_SCOPE")
        self.assertEqual(row["auxiliary_tags"], ["WRONG_TIME"])
        self.assertEqual(row["relevant_sql_steps"], [1])
        self.assertEqual(row["schema_version"], "sql_retrieval_subtyping_v1")
        self.assertEqual(row["overlay_taxonomy_version"], "sql_retrieval_taxonomy_v1")

    def test_apply_completed_review_sorts_by_numeric_incident_and_question(self):
        bundles = [_bundle("incident_10", 0), _bundle("incident_2", 4)]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            _write_csv(path, bundles)
            rows = apply_completed_review(
                bundles, path, load_overlay_taxonomy(TAXONOMY_PATH)
            )
        self.assertEqual(
            [(row["incident"], row["question_index"]) for row in rows],
            [("incident_2", 4), ("incident_10", 0)],
        )

    def test_immutable_bundle_cells_are_rejected_before_merge(self):
        bundle = _bundle()
        mutations = {
            "incident": "incident_9",
            "question_index": "1",
            "question_fingerprint_sha256": "f" * 64,
            "question_text_fingerprint_sha256": "f" * 64,
            "question": "changed",
            "context": "changed context",
            "golden_answer": '{"answer":"different"}',
            "golden_solution": '{"solution":"different"}',
            "query_steps": '[{"step":1,"sql":"SELECT changed","observation":"example-service","query_success":true}]',
            "submitted_answer": "different",
            "agent_source_index": "3",
            "env_source_index": "3",
            "agent_source_sha256": "f" * 64,
            "env_source_sha256": "f" * 64,
            "question_source_sha256": "f" * 64,
            "reward_official": "0.5",
            "reviewed_primary_original": "OTHER",
            "review_notes_original": "changed",
            "trajectory_steps": "3",
            "submitted": "False",
            "submitted_at_step_limit": "True",
        }
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "review.csv"
                row = _bundle_cells(bundle)
                row.update(_decision_cells())
                row[field] = value
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                    writer.writeheader()
                    writer.writerow(row)
                with self.assertRaises(ReviewError):
                    apply_completed_review([bundle], path, taxonomy)

    def test_review_header_must_be_exact_and_rows_must_cover_all_bundles(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for label, header in (
                ("missing", REVIEW_FIELDS[:-1]),
                ("extra", REVIEW_FIELDS + ["extra"]),
                ("duplicate", REVIEW_FIELDS[:-1] + [REVIEW_FIELDS[-2]]),
            ):
                with self.subTest(header=label):
                    path = root / f"{label}.csv"
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.writer(handle)
                        writer.writerow(header)
                    with self.assertRaises(ReviewError):
                        apply_completed_review([bundle], path, taxonomy)
            empty = root / "empty.csv"
            empty.write_text("", encoding="utf-8")
            with self.assertRaises(ReviewError):
                apply_completed_review([bundle], empty, taxonomy)

            valid_row = _bundle_cells(bundle)
            valid_row.update(_decision_cells())
            for label, rows in (
                ("missing_row", []),
                ("duplicate_identity", [valid_row, valid_row]),
            ):
                path = root / f"{label}.csv"
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                    writer.writeheader()
                    writer.writerows(rows)
                with self.assertRaises(ReviewError):
                    apply_completed_review([bundle], path, taxonomy)

            unknown = dict(valid_row)
            unknown["question_index"] = "99"
            path = root / "unknown.csv"
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                writer.writeheader()
                writer.writerow(unknown)
            with self.assertRaises(ReviewError):
                apply_completed_review([bundle], path, taxonomy)

    def test_decision_validation_rejects_unknown_duplicate_and_inconsistent_values(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        cases = (
            {"retrieval_primary_subtype": "UNKNOWN"},
            {"auxiliary_tags": ["WRONG_TIME", "WRONG_TIME"]},
            {"confidence": "indeterminate", "retrieval_primary_subtype": "TEMPORAL_SCOPE"},
            {"relevant_sql_steps": [1, 1]},
            {"first_divergence_step": 2},
            {"relevant_sql_steps": [2]},
            {"retrieval_primary_subtype": "TEMPORAL_SCOPE", "sql_evidence": "", "observation_evidence": ""},
        )
        for overrides in cases:
            with self.subTest(overrides=overrides), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "review.csv"
                _write_csv(path, [bundle], **overrides)
                with self.assertRaises(ReviewError):
                    apply_completed_review([bundle], path, taxonomy)

    def test_taxonomy_and_decision_list_json_must_be_well_formed(self):
        bundle = _bundle()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
            for field, value in (
                ("version", "sql_retrieval_taxonomy_v2"),
                ("primary_subtypes", []),
                ("auxiliary_tags", ["WRONG_TIME", "WRONG_TIME"]),
                ("confidence", ["high", 1]),
            ):
                with self.subTest(field=field):
                    candidate = dict(raw)
                    candidate[field] = value
                    taxonomy_path = root / f"taxonomy-{field}.json"
                    taxonomy_path.write_text(json.dumps(candidate), encoding="utf-8")
                    with self.assertRaises(ReviewError):
                        load_overlay_taxonomy(taxonomy_path)

            taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
            for field, value in (("auxiliary_tags", "not-json"), ("relevant_sql_steps", "{}")):
                path = root / f"review-{field}.csv"
                row = _bundle_cells(bundle)
                row.update(_decision_cells(**{field: value}))
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                    writer.writeheader()
                    writer.writerow(row)
                with self.assertRaises(ReviewError):
                    apply_completed_review([bundle], path, taxonomy)

    def test_indeterminate_decision_requires_rationale_and_selection_is_sorted_deduplicated(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            _write_csv(
                path,
                [bundle],
                retrieval_primary_subtype="INDETERMINATE",
                confidence="indeterminate",
                decision_status="needs_review",
                first_divergence_step="",
                relevant_sql_steps=[],
                sql_evidence="",
                observation_evidence="",
                rationale="",
            )
            with self.assertRaises(ReviewError):
                apply_completed_review([bundle], path, taxonomy)

        rows = [
            {"incident": "incident_10", "question_index": 3, "question_fingerprint_sha256": "a" * 64, "confidence": "high", "retrieval_primary_subtype": "TEMPORAL_SCOPE", "decision_status": "reviewed", "boundary_flag": "NONE"},
            {"incident": "incident_2", "question_index": 1, "question_fingerprint_sha256": "b" * 64, "confidence": "low", "retrieval_primary_subtype": "TEMPORAL_SCOPE", "decision_status": "reviewed", "boundary_flag": "NONE"},
            {"incident": "incident_2", "question_index": 1, "question_fingerprint_sha256": "b" * 64, "confidence": "indeterminate", "retrieval_primary_subtype": "INDETERMINATE", "decision_status": "needs_review", "boundary_flag": "NONE"},
            {"incident": "incident_1", "question_index": 8, "question_fingerprint_sha256": "c" * 64, "confidence": "high", "retrieval_primary_subtype": "TEMPORAL_SCOPE", "decision_status": "reviewed", "boundary_flag": "SQL_EXEC_POSSIBLE"},
        ]
        selected = select_low_confidence_rows(rows)
        self.assertEqual([(row["incident"], row["question_index"]) for row in selected], [("incident_1", 8), ("incident_2", 1)])
        with self.assertRaises(ReviewError):
            select_low_confidence_rows([{**rows[0], "question_index": "bad"}])


if __name__ == "__main__":
    unittest.main()
