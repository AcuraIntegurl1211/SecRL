from __future__ import annotations

import csv
import json
import tempfile
import unittest
from dataclasses import fields as dataclass_fields, replace
from pathlib import Path

from experiments.failure_analysis.models import ReviewError
from experiments.failure_analysis.retrieval_extract import write_preparation_files
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
EVIDENCE_FIELDS = (
    "incident",
    "question_index",
    "question_fingerprint_sha256",
    "question_text_fingerprint_sha256",
    "question",
    "context",
    "golden_answer",
    "golden_solution",
    "submitted_answer",
    "trajectory_steps",
    "submitted",
    "submitted_at_step_limit",
    "reward_official",
    "reviewed_primary_original",
    "review_notes_original",
    "agent_source_index",
    "env_source_index",
    "agent_source_sha256",
    "env_source_sha256",
    "question_source_sha256",
    "query_steps",
)
DECISION_FIELDS = (
    "retrieval_primary_subtype",
    "auxiliary_tags",
    "retrieval_outcome",
    "boundary_flag",
    "confidence",
    "decision_status",
    "first_divergence_step",
    "relevant_sql_steps",
    "sql_evidence",
    "observation_evidence",
    "gold_evidence_basis",
    "rationale",
)
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
    def test_dataclass_fields_match_frozen_review_contract(self):
        self.assertEqual(
            tuple(field.name for field in dataclass_fields(RetrievalEvidenceBundle)),
            EVIDENCE_FIELDS,
        )
        self.assertEqual(
            tuple(field.name for field in dataclass_fields(RetrievalDecision)),
            DECISION_FIELDS,
        )

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

    def test_apply_completed_review_accepts_preparation_template_primitive_gold_and_large_cells(self):
        bundle = replace(
            _bundle(),
            golden_answer="syncretic.7z.lockbit",
            golden_solution="SELECT the matching artifact",
            question="q" * 131073,
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            evidence = root / "evidence.jsonl"
            review = root / "review.csv"
            previous_csv_limit = csv.field_size_limit()
            write_preparation_files([bundle], evidence, review)

            merged = apply_completed_review(
                [bundle], review, load_overlay_taxonomy(TAXONOMY_PATH)
            )

        self.assertEqual(csv.field_size_limit(), previous_csv_limit)
        self.assertEqual(merged[0]["golden_answer"], "syncretic.7z.lockbit")
        self.assertEqual(merged[0]["golden_solution"], "SELECT the matching artifact")

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
                ("extra", REVIEW_FIELDS + ("extra",)),
                ("duplicate", REVIEW_FIELDS[:-1] + (REVIEW_FIELDS[-2],)),
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

    def test_taxonomy_lists_are_frozen_in_content_and_order(self):
        canonical = json.loads(TAXONOMY_PATH.read_text(encoding="utf-8"))
        list_fields = (
            "primary_subtypes",
            "auxiliary_tags",
            "outcomes",
            "boundary_flags",
            "confidence",
            "decision_statuses",
        )
        for field in list_fields:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "taxonomy.json"
                modified = json.loads(json.dumps(canonical))
                modified[field] = list(reversed(modified[field]))
                path.write_text(json.dumps(modified), encoding="utf-8")
                with self.assertRaises(ReviewError):
                    load_overlay_taxonomy(path)

        bundle = _bundle()
        with tempfile.TemporaryDirectory() as directory:
            review_path = Path(directory) / "review.csv"
            _write_csv(review_path, [bundle])
            for field in list_fields:
                with self.subTest(caller_field=field):
                    modified = json.loads(json.dumps(canonical))
                    modified[field] = list(reversed(modified[field]))
                    with self.assertRaises(ReviewError):
                        apply_completed_review([bundle], review_path, modified)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "taxonomy-extra.json"
            modified = json.loads(json.dumps(canonical))
            modified["metadata"] = {"owner": "review"}
            path.write_text(json.dumps(modified), encoding="utf-8")
            with self.assertRaises(ReviewError):
                load_overlay_taxonomy(path)
            with self.assertRaises(ReviewError):
                apply_completed_review([bundle], review_path, modified)

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
            {"incident": "incident_10", "question_index": 3, "question_fingerprint_sha256": "a" * 64, "confidence": "high", "retrieval_primary_subtype": "TEMPORAL_SCOPE", "decision_status": "reviewed", "boundary_flag": "NONE", "retrieval_outcome": "UNOBSERVED", "auxiliary_tags": [], "first_divergence_step": None, "relevant_sql_steps": [], "sql_evidence": "", "observation_evidence": "", "gold_evidence_basis": "", "rationale": "queue row"},
            {"incident": "incident_2", "question_index": 1, "question_fingerprint_sha256": "b" * 64, "confidence": "low", "retrieval_primary_subtype": "TEMPORAL_SCOPE", "decision_status": "reviewed", "boundary_flag": "NONE", "retrieval_outcome": "UNOBSERVED", "auxiliary_tags": [], "first_divergence_step": None, "relevant_sql_steps": [], "sql_evidence": "", "observation_evidence": "", "gold_evidence_basis": "", "rationale": "queue row"},
            {"incident": "incident_1", "question_index": 8, "question_fingerprint_sha256": "c" * 64, "confidence": "high", "retrieval_primary_subtype": "TEMPORAL_SCOPE", "decision_status": "reviewed", "boundary_flag": "SQL_EXEC_POSSIBLE", "retrieval_outcome": "UNOBSERVED", "auxiliary_tags": [], "first_divergence_step": None, "relevant_sql_steps": [], "sql_evidence": "", "observation_evidence": "", "gold_evidence_basis": "", "rationale": "queue row"},
        ]
        selected = select_low_confidence_rows(rows)
        self.assertEqual([(row["incident"], row["question_index"]) for row in selected], [("incident_1", 8), ("incident_2", 1)])
        with self.assertRaises(ReviewError):
            select_low_confidence_rows([{**rows[0], "question_index": "bad"}])

    def test_queue_validates_all_decision_fields_and_consistency(self):
        base = {
            "incident": "incident_1",
            "question_index": 0,
            "question_fingerprint_sha256": "a" * 64,
            "retrieval_primary_subtype": "TEMPORAL_SCOPE",
            "retrieval_outcome": "UNOBSERVED",
            "auxiliary_tags": [],
            "confidence": "low",
            "decision_status": "needs_review",
            "boundary_flag": "NONE",
            "first_divergence_step": None,
            "relevant_sql_steps": [],
            "sql_evidence": "",
            "observation_evidence": "",
            "gold_evidence_basis": "",
            "rationale": "queue row",
        }
        invalid_rows = (
            {**base, "retrieval_outcome": "BOGUS"},
            {**base, "auxiliary_tags": "WRONG_TIME"},
            {**base, "auxiliary_tags": ["WRONG_TIME", "WRONG_TIME"]},
            {**base, "auxiliary_tags": ["BOGUS"]},
            {**base, "confidence": "indeterminate"},
            {**base, "retrieval_primary_subtype": "INDETERMINATE", "confidence": "high"},
            {key: value for key, value in base.items() if key != "rationale"},
            {**base, "first_divergence_step": 0},
            {**base, "first_divergence_step": "1"},
            {**base, "relevant_sql_steps": ["1"]},
            {**base, "sql_evidence": None},
        )
        for row in invalid_rows:
            with self.subTest(row=row), self.assertRaises(ReviewError):
                select_low_confidence_rows([row])

    def test_queue_rejects_duplicate_stable_identity_rows(self):
        row = {
            "incident": "incident_1",
            "question_index": 0,
            "question_fingerprint_sha256": "a" * 64,
            "retrieval_primary_subtype": "TEMPORAL_SCOPE",
            "retrieval_outcome": "UNOBSERVED",
            "auxiliary_tags": [],
            "confidence": "low",
            "decision_status": "needs_review",
            "boundary_flag": "NONE",
            "first_divergence_step": None,
            "relevant_sql_steps": [],
            "sql_evidence": "",
            "observation_evidence": "",
            "gold_evidence_basis": "",
            "rationale": "queue row",
        }
        with self.assertRaises(ReviewError):
            select_low_confidence_rows([row, {**row, "confidence": "medium"}])

    def test_path_arguments_never_leak_path_type_errors(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        bad_paths = (None, object(), "not-a-path", Path("\x00"))
        for bad_path in bad_paths:
            with self.subTest(path=repr(bad_path)):
                with self.assertRaisesRegex(ReviewError, "path"):
                    load_overlay_taxonomy(bad_path)  # type: ignore[arg-type]
                with self.assertRaisesRegex(ReviewError, "path"):
                    apply_completed_review([bundle], bad_path, taxonomy)  # type: ignore[arg-type]

    def test_deeply_nested_json_raises_review_error(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        deep = "[" * 10000 + "0" + "]" * 10000
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            taxonomy_path = root / "deep-taxonomy.json"
            taxonomy_text = TAXONOMY_PATH.read_text(encoding="utf-8").rstrip()
            taxonomy_path.write_text(taxonomy_text[:-1] + ',"deep":' + deep + "}", encoding="utf-8")
            with self.assertRaisesRegex(ReviewError, "taxonomy"):
                load_overlay_taxonomy(taxonomy_path)

            for field, value in (
                ("golden_answer", deep),
                ("auxiliary_tags", deep),
                ("relevant_sql_steps", deep),
                ("query_steps", deep),
            ):
                with self.subTest(field=field):
                    path = root / f"deep-{field}.csv"
                    row = _bundle_cells(bundle)
                    row.update(_decision_cells())
                    row[field] = value
                    with path.open("w", encoding="utf-8", newline="") as handle:
                        writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                        writer.writeheader()
                        writer.writerow(row)
                    with self.assertRaisesRegex(ReviewError, field):
                        apply_completed_review([bundle], path, taxonomy)

    def test_duplicate_json_object_keys_raise_review_error(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-golden.csv"
            row = _bundle_cells(bundle)
            row.update(_decision_cells())
            row["golden_answer"] = '{"answer":"example-service","answer":"example-service"}'
            with path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                writer.writeheader()
                writer.writerow(row)
            with self.assertRaisesRegex(ReviewError, "golden_answer"):
                apply_completed_review([bundle], path, taxonomy)

    def test_duplicate_incident_question_index_rejected_even_with_different_fingerprints(self):
        first = _bundle("incident_2", 0)
        second = replace(first, question_fingerprint_sha256="d" * 64)
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "duplicate-index.csv"
            _write_csv(path, [first, second])
            with self.assertRaises(ReviewError):
                apply_completed_review([first, second], path, taxonomy)

        row = {
            "incident": "incident_2",
            "question_index": 0,
            "question_fingerprint_sha256": "a" * 64,
            "retrieval_primary_subtype": "TEMPORAL_SCOPE",
            "retrieval_outcome": "UNOBSERVED",
            "auxiliary_tags": [],
            "confidence": "low",
            "decision_status": "needs_review",
            "boundary_flag": "NONE",
            "first_divergence_step": None,
            "relevant_sql_steps": [],
            "sql_evidence": "",
            "observation_evidence": "",
            "gold_evidence_basis": "",
            "rationale": "queue row",
        }
        with self.assertRaises(ReviewError):
            select_low_confidence_rows([row, {**row, "question_fingerprint_sha256": "b" * 64}])

    def test_indeterminate_primary_rejects_high_confidence(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "review.csv"
            _write_csv(
                path,
                [bundle],
                retrieval_primary_subtype="INDETERMINATE",
                confidence="high",
                decision_status="reviewed",
                first_divergence_step="",
                relevant_sql_steps=[],
                sql_evidence="",
                observation_evidence="",
                rationale="Evidence remains ambiguous.",
            )
            with self.assertRaises(ReviewError):
                apply_completed_review([bundle], path, taxonomy)

    def test_oversized_canonical_decimal_cells_raise_review_error(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        oversized = "9" * 5000
        for field, value in (
            ("incident", "incident_" + oversized),
            ("question_index", oversized),
            ("first_divergence_step", oversized),
            ("trajectory_steps", oversized),
        ):
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "review.csv"
                row = _bundle_cells(bundle)
                row.update(_decision_cells())
                row[field] = value
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                    writer.writeheader()
                    writer.writerow(row)
                with self.assertRaisesRegex(ReviewError, field):
                    apply_completed_review([bundle], path, taxonomy)

    def test_oversized_json_numbers_raise_review_error(self):
        bundle = _bundle()
        taxonomy = load_overlay_taxonomy(TAXONOMY_PATH)
        oversized = "9" * 5000
        cases = (
            ("auxiliary_tags", f"[{oversized}]"),
            ("relevant_sql_steps", f"[{oversized}]"),
            ("golden_answer", f'{{"answer":{oversized}}}'),
            (
                "query_steps",
                f'[{"{"}"step":{oversized},"sql":"SELECT service FROM events LIMIT 1","observation":"example-service","query_success":true}}]',
            ),
        )
        for field, value in cases:
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "review.csv"
                row = _bundle_cells(bundle)
                row.update(_decision_cells())
                row[field] = value
                with path.open("w", encoding="utf-8", newline="") as handle:
                    writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                    writer.writeheader()
                    writer.writerow(row)
                with self.assertRaisesRegex(ReviewError, field):
                    apply_completed_review([bundle], path, taxonomy)


if __name__ == "__main__":
    unittest.main()
