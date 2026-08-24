from __future__ import annotations

import csv
import json
import math
import os
import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from experiments.failure_analysis.identity import canonical_json, question_identity, sha256_file
from experiments.failure_analysis.models import InputError, MappingError, OutputCollisionError
from experiments.failure_analysis.retrieval_extract import (
    SourceSpec,
    build_evidence_bundles,
    load_reviewed_rows,
    load_source_specs,
    write_preparation_files,
)
from experiments.failure_analysis.retrieval_models import QueryStep
from tests.failure_analysis.helpers import agent_entry, env_entry as base_env_entry, question


REVIEW_FIELDS = [
    "incident",
    "question_index",
    "question_fingerprint_sha256",
    "reward_official",
    "steps",
    "max_steps",
    "submitted",
    "submitted_at_step_limit",
    "reviewed_primary",
    "review_notes",
]


def write_json(path: Path, value: object) -> None:
    path.write_text(canonical_json(value), encoding="utf-8")


def review_row(incident: str, index: int, item: dict[str, object], **extra: str) -> dict[str, str]:
    row = {
        "incident": incident,
        "question_index": str(index),
        "question_fingerprint_sha256": question_identity(incident, index, item).question_fingerprint_sha256,
        "reward_official": "1.0",
        "steps": "0",
        "max_steps": "15",
        "submitted": "False",
        "submitted_at_step_limit": "False",
        "reviewed_primary": "SQL_RETRIEVAL",
        "review_notes": "needs semantic inspection",
    }
    row.update(extra)
    return row


def write_review(
    path: Path,
    rows: list[dict[str, str]],
    fieldnames: list[str] | None = None,
) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames or REVIEW_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def env_entry(
    item: dict[str, object],
    reward: float = 0.0,
    trajectory: list[dict[str, object]] | None = None,
) -> dict[str, object]:
    entry = base_env_entry(item, reward, trajectory)
    entry["steps"] = len(entry["trajectory"])
    return entry


class RetrievalExtractTest(unittest.TestCase):
    def test_load_reviewed_rows_filters_validates_and_sorts_identities(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            one, two = question("one"), question("two")
            path = root / "review.csv"
            write_review(path, [
                review_row("incident_10", 0, one),
                review_row("incident_2", 1, two),
                review_row("incident_2", 0, one, reviewed_primary="ANSWER"),
            ])

            rows = load_reviewed_rows(path)

            self.assertEqual([(row["incident"], row["question_index"]) for row in rows], [("incident_2", "1"), ("incident_10", "0")])
            self.assertEqual(rows[0]["review_notes"], "needs semantic inspection")

            for field, value in (
                ("incident", ""),
                ("incident", "garbage_2"),
                ("incident", "incident_٢"),
                ("question_index", "no"),
                ("question_fingerprint_sha256", "bad"),
                ("question_fingerprint_sha256", "A" * 64),
                ("reward_official", "nan"),
                ("reward_official", "inf"),
                ("reward_official", "not-a-number"),
            ):
                broken = review_row("incident_2", 0, one)
                broken[field] = value
                write_review(path, [broken])
                with self.assertRaises(InputError):
                    load_reviewed_rows(path)

            duplicate = review_row("incident_2", 0, one)
            write_review(path, [duplicate, duplicate])
            with self.assertRaises(InputError):
                load_reviewed_rows(path)

    def test_load_reviewed_rows_requires_canonical_submission_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = question("contract")
            path = root / "review.csv"

            for field in ("steps", "max_steps", "submitted", "submitted_at_step_limit"):
                fieldnames = [name for name in REVIEW_FIELDS if name != field]
                row = review_row("incident_2", 0, item)
                row.pop(field)
                write_review(path, [row], fieldnames)
                with self.subTest(missing=field), self.assertRaises(MappingError):
                    load_reviewed_rows(path)

            integer_invalid = ("", " ", "+1", "-1", "00", "01", "1 ", " 1", "١", "１")
            for field in ("steps", "max_steps"):
                for value in integer_invalid:
                    row = review_row("incident_2", 0, item)
                    row[field] = value
                    write_review(path, [row])
                    with self.subTest(field=field, value=value), self.assertRaises(MappingError):
                        load_reviewed_rows(path)

            boolean_invalid = ("", " ", "1", "0", "true", "false", "TRUE", "False ")
            for field in ("submitted", "submitted_at_step_limit"):
                for value in boolean_invalid:
                    row = review_row("incident_2", 0, item)
                    row[field] = value
                    write_review(path, [row])
                    with self.subTest(field=field, value=value), self.assertRaises(MappingError):
                        load_reviewed_rows(path)

            for extra in (
                {"steps": "0", "submitted": "True", "submitted_at_step_limit": "True"},
                {"steps": "1", "submitted": "False", "submitted_at_step_limit": "True"},
            ):
                write_review(path, [review_row("incident_2", 0, item, **extra)])
                with self.subTest(extra=extra), self.assertRaises(MappingError):
                    load_reviewed_rows(path)

    def test_load_source_specs_resolves_and_validates_sources_before_json_read(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sources = root / "sources"
            sources.mkdir()
            paths = {name: sources / f"{name}.json" for name in ("agent", "env", "question")}
            for path in paths.values():
                path.write_text("not json", encoding="utf-8")
            manifest = root / "manifest.json"
            write_json(manifest, {
                "incident": "incident_2",
                "sources": {
                    name: {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}
                    for name, path in paths.items()
                },
            })

            specs = load_source_specs([manifest], root)

            spec = specs["incident_2"]
            self.assertIsInstance(spec, SourceSpec)
            self.assertEqual(spec.agent_path, paths["agent"])
            with self.assertRaises(FrozenInstanceError):
                spec.incident = "other"

            paths["env"].write_text("changed", encoding="utf-8")
            with self.assertRaises(InputError):
                load_source_specs([manifest], root)

            missing = root / "missing.json"
            write_json(missing, {"incident": "incident_3", "sources": {}})
            with self.assertRaises(InputError):
                load_source_specs([missing], root)
            with self.assertRaises(InputError):
                load_source_specs([manifest, manifest], root)

            paths["env"].write_text("not json", encoding="utf-8")
            malformed_incident = root / "malformed-incident.json"
            write_json(malformed_incident, {
                "incident": "garbage_2",
                "sources": {
                    name: {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}
                    for name, path in paths.items()
                },
            })
            with self.assertRaises(InputError):
                load_source_specs([malformed_incident], root)

            unicode_incident = root / "unicode-incident.json"
            write_json(unicode_incident, {
                "incident": "incident_٢",
                "sources": {
                    name: {"path": str(path.relative_to(root)), "sha256": sha256_file(path)}
                    for name, path in paths.items()
                },
            })
            with self.assertRaises(InputError):
                load_source_specs([unicode_incident], root)

            bad_path = root / "bad-path.json"
            write_json(bad_path, {
                "incident": "incident_4",
                "sources": {
                    "agent": {"path": "no-agent.json", "sha256": "a" * 64},
                    "env": {"path": str(paths["env"].relative_to(root)), "sha256": sha256_file(paths["env"])},
                    "question": {"path": str(paths["question"].relative_to(root)), "sha256": sha256_file(paths["question"])},
                },
            })
            with self.assertRaises(InputError):
                load_source_specs([bad_path], root)

    def test_build_evidence_bundles_preserves_traceable_mapped_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            first = question("first", answer="gold")
            first["context"] = {"z": 1, "a": [2]}
            first["solution"] = {"steps": ["look up gold"]}
            second = question("second")
            source_paths = {name: root / f"{name}.json" for name in ("agent", "env", "question")}
            write_json(source_paths["question"], [first, second])
            write_json(source_paths["agent"], [agent_entry(second, 1.0), agent_entry(first, 1.0)])
            write_json(source_paths["env"], [
                env_entry(second, 1.0, [{"action": "SELECT two", "observation": "[]", "info": {"query_success": None}}]),
                env_entry(first, 1.0, [
                    {"action": "SELECT one", "observation": "gold", "info": {"query_success": True}},
                    {"action": "gold", "observation": "", "info": {"submit": True, "submitted_answer": "gold"}},
                ]),
            ])
            manifest = root / "manifest.json"
            write_json(manifest, {
                "incident": "incident_2",
                "sources": {name: {"path": path.name, "sha256": sha256_file(path)} for name, path in source_paths.items()},
            })
            specs = load_source_specs([manifest], root)
            rows = [
                review_row("incident_2", 1, second, steps="1"),
                review_row("incident_2", 0, first, steps="2", submitted="True"),
            ]

            bundles = build_evidence_bundles(rows, specs, {"incident_2": 2})

            self.assertEqual([bundle.question_index for bundle in bundles], [0, 1])
            bundle = bundles[0]
            self.assertEqual(bundle.question, "first")
            self.assertEqual(bundle.context, '{"a":[2],"z":1}')
            self.assertEqual(bundle.golden_answer, "gold")
            self.assertEqual(bundle.golden_solution["steps"], ("look up gold",))
            self.assertEqual(bundle.submitted_answer, "gold")
            self.assertEqual(bundle.trajectory_steps, 2)
            self.assertTrue(bundle.submitted)
            self.assertFalse(bundle.submitted_at_step_limit)
            self.assertEqual(bundle.reward_official, 1.0)
            self.assertEqual(bundle.review_notes_original, "needs semantic inspection")
            self.assertEqual((bundle.agent_source_index, bundle.env_source_index), (1, 1))
            self.assertEqual(
                (bundle.agent_source_sha256, bundle.env_source_sha256, bundle.question_source_sha256),
                (specs["incident_2"].agent_sha256, specs["incident_2"].env_sha256, specs["incident_2"].question_sha256),
            )
            self.assertEqual([(step.step, step.sql, step.query_success) for step in bundle.query_steps], [(1, "SELECT one", True)])
            self.assertEqual(bundles[1].context, "context")
            self.assertIsNone(bundles[1].query_steps[0].query_success)

            with self.assertRaises(MappingError):
                build_evidence_bundles(rows, specs, {"incident_2": 1})
            wrong = dict(rows[0])
            wrong["question_fingerprint_sha256"] = "0" * 64
            with self.assertRaises(MappingError):
                build_evidence_bundles([wrong, rows[1]], specs, {"incident_2": 2})
            with self.assertRaises(MappingError):
                build_evidence_bundles([rows[0], rows[0]], specs, {"incident_2": 2})
            mismatched_reward = dict(rows[0])
            mismatched_reward["reward_official"] = "0.0"
            with self.assertRaises(MappingError) as raised:
                build_evidence_bundles([mismatched_reward, rows[1]], specs, {"incident_2": 2})
            self.assertIn(mismatched_reward["question_fingerprint_sha256"], str(raised.exception))
            with self.assertRaises(MappingError):
                build_evidence_bundles(rows, specs, {"incident_2": 3})
            with self.assertRaises(MappingError):
                build_evidence_bundles(rows, specs, {"incident_2": 1})
            with_other_incident = {
                **specs,
                "incident_3": replace(specs["incident_2"], incident="incident_3"),
            }
            rows_with_other_incident = [*rows, review_row("incident_3", 0, first)]
            with self.assertRaises(MappingError):
                build_evidence_bundles(
                    rows_with_other_incident,
                    with_other_incident,
                    {"incident_2": 1, "incident_3": 2},
                )
            self.assertEqual(
                len(build_evidence_bundles(rows, specs, {"incident_2": 2})),
                2,
            )

    def test_build_evidence_bundles_derives_submission_from_full_trajectory(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = [question("early"), question("at limit"), question("not submitted")]
            trajectories = [
                [
                    {"action": "SELECT early", "observation": "[]", "info": {"query_success": True}},
                    {"action": "answer", "observation": "", "info": {"submit": True}},
                ],
                [
                    {"action": "SELECT one", "observation": "[]", "info": {"query_success": True}},
                    {"action": "SELECT two", "observation": "[]", "info": {"query_success": True}},
                    {"action": "answer", "observation": "", "info": {"submit": True}},
                ],
                [
                    {"action": "SELECT one", "observation": "[]", "info": {"query_success": True}},
                    {"action": "SELECT two", "observation": "[]", "info": {"query_success": True}},
                ],
            ]
            paths = {name: root / f"{name}.json" for name in ("agent", "env", "question")}
            write_json(paths["question"], items)
            write_json(paths["agent"], [agent_entry(item, 1.0) for item in items])
            write_json(paths["env"], [env_entry(item, 1.0, trajectory) for item, trajectory in zip(items, trajectories)])
            manifest = root / "manifest.json"
            write_json(manifest, {"incident": "incident_2", "sources": {name: {"path": path.name, "sha256": sha256_file(path)} for name, path in paths.items()}})
            specs = load_source_specs([manifest], root)
            rows = [
                review_row("incident_2", 0, items[0], steps="2", max_steps="4", submitted="True"),
                review_row("incident_2", 1, items[1], steps="3", max_steps="3", submitted="True", submitted_at_step_limit="True"),
                review_row("incident_2", 2, items[2], steps="2", max_steps="2"),
            ]

            bundles = build_evidence_bundles(rows, specs, {"incident_2": 3})

            self.assertEqual(
                [(bundle.trajectory_steps, bundle.submitted, bundle.submitted_at_step_limit) for bundle in bundles],
                [(2, True, False), (3, True, True), (2, False, False)],
            )
            self.assertEqual([[step.step for step in bundle.query_steps] for bundle in bundles], [[1], [1, 2], [1, 2]])

    def test_build_evidence_bundles_rejects_submission_contract_mismatches(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = question("mismatch")
            trajectory = [
                {"action": "SELECT one", "observation": "[]", "info": {"query_success": True}},
                {"action": "answer", "observation": "", "info": {"submit": True}},
            ]

            def build(env_overrides=None, row_overrides=None):
                env = env_entry(item, 1.0, trajectory)
                overrides = dict(env_overrides or {})
                if overrides.pop("remove_steps", False):
                    env.pop("steps")
                env.update(overrides)
                paths = {name: root / f"{name}.json" for name in ("agent", "env", "question")}
                write_json(paths["question"], [item])
                write_json(paths["agent"], [agent_entry(item, 1.0)])
                write_json(paths["env"], [env])
                manifest = root / "manifest.json"
                write_json(manifest, {"incident": "incident_2", "sources": {name: {"path": path.name, "sha256": sha256_file(path)} for name, path in paths.items()}})
                row = review_row("incident_2", 0, item, steps="2", max_steps="4", submitted="True")
                row.update(row_overrides or {})
                return build_evidence_bundles([row], load_source_specs([manifest], root), {"incident_2": 1})

            for field, value in (
                ("steps", "1"),
                ("submitted", "False"),
                ("submitted_at_step_limit", "True"),
            ):
                with self.subTest(field=field):
                    with self.assertRaises(MappingError) as raised:
                        build(row_overrides={field: value})
                    message = str(raised.exception)
                    self.assertIn(field, message)
                    self.assertIn("incident_2", message)
                    self.assertIn("question_index=0", message)
                    self.assertIn(question_identity("incident_2", 0, item).question_fingerprint_sha256, message)

            for field, value in (
                ("missing", None),
                ("type", "2"),
                ("bool", True),
                ("mismatch", 1),
            ):
                overrides = {"remove_steps": True} if field == "missing" else {"steps": value}
                with self.subTest(env_steps=field):
                    with self.assertRaises(MappingError) as raised:
                        build(env_overrides=overrides)
                    message = str(raised.exception)
                    self.assertIn("steps", message)
                    self.assertIn("incident_2", message)
                    self.assertIn("question_index=0", message)
                    self.assertIn(question_identity("incident_2", 0, item).question_fingerprint_sha256, message)

            for label, invalid_trajectory in (
                ("not-list", "not a trajectory"),
                ("non-object-step", [trajectory[0], 2]),
            ):
                with self.subTest(trajectory=label):
                    with self.assertRaises(MappingError) as raised:
                        build(env_overrides={"trajectory": invalid_trajectory})
                    self.assertIn("trajectory", str(raised.exception))

            for field, value in (
                ("steps", "01"),
                ("max_steps", "١"),
                ("submitted", "true"),
                ("submitted_at_step_limit", "1"),
            ):
                with self.subTest(direct_row=field), self.assertRaises(MappingError):
                    build(row_overrides={field: value})

    def test_build_evidence_bundles_rejects_reward_conflicts_and_bad_incidents(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = question("only")
            source_paths = {name: root / f"{name}.json" for name in ("agent", "env", "question")}
            write_json(source_paths["question"], [item])
            write_json(source_paths["agent"], [agent_entry(item, 0.0)])
            write_json(source_paths["env"], [env_entry(item, 1.0, [])])
            manifest = root / "manifest.json"
            write_json(manifest, {"incident": "incident_2", "sources": {name: {"path": path.name, "sha256": sha256_file(path)} for name, path in source_paths.items()}})
            specs = load_source_specs([manifest], root)
            rows = [review_row("incident_2", 0, item)]
            with self.assertRaises(MappingError):
                build_evidence_bundles(rows, specs, {"incident_2": 1})
            with self.assertRaises(MappingError):
                build_evidence_bundles([review_row("incident_3", 0, item)], specs, {"incident_2": 1})
            with self.assertRaises(MappingError):
                build_evidence_bundles([], specs, {"incident_2": 1})

    def test_build_evidence_bundles_rejects_source_changed_after_manifest_load(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            item = question("source changes")
            source_paths = {name: root / f"{name}.json" for name in ("agent", "env", "question")}
            write_json(source_paths["question"], [item])
            write_json(source_paths["agent"], [agent_entry(item, 1.0)])
            write_json(source_paths["env"], [env_entry(item, 1.0, [])])
            manifest = root / "manifest.json"
            write_json(manifest, {
                "incident": "incident_2",
                "sources": {
                    name: {"path": path.name, "sha256": sha256_file(path)}
                    for name, path in source_paths.items()
                },
            })
            specs = load_source_specs([manifest], root)
            source_paths["env"].write_text("[]", encoding="utf-8")

            with self.assertRaises(InputError):
                build_evidence_bundles(
                    [review_row("incident_2", 0, item)], specs, {"incident_2": 1}
                )

    def test_write_preparation_files_writes_conservative_review_template_without_overwrite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original_bundle = __import__("experiments.failure_analysis.retrieval_models", fromlist=["RetrievalEvidenceBundle"]).RetrievalEvidenceBundle.fixture_for_test()
            with self.assertRaises(InputError):
                write_preparation_files([original_bundle], root / "bad.jsonl", root / "bad.csv")
            bundle = replace(original_bundle, incident="incident_1")
            with self.assertRaises(InputError):
                write_preparation_files([bundle, bundle], root / "duplicate.jsonl", root / "duplicate.csv")
            with self.assertRaises(InputError):
                write_preparation_files(
                    [replace(bundle, incident="incident_٢")],
                    root / "unicode.jsonl",
                    root / "unicode.csv",
                )
            evidence = root / "nested" / "evidence.jsonl"
            template = root / "nested" / "review.csv"

            write_preparation_files([bundle], evidence, template)

            evidence_row = json.loads(evidence.read_text(encoding="utf-8"))
            self.assertEqual(evidence_row["golden_answer"], {"answer": "example-service"})
            self.assertEqual(evidence_row["query_steps"][0]["step"], 1)
            self.assertEqual(
                {key: evidence_row[key] for key in ("trajectory_steps", "submitted", "submitted_at_step_limit")},
                {"trajectory_steps": 2, "submitted": True, "submitted_at_step_limit": False},
            )
            with template.open(encoding="utf-8", newline="") as handle:
                row = next(csv.DictReader(handle))
            self.assertEqual(
                {key: row[key] for key in ("trajectory_steps", "submitted", "submitted_at_step_limit")},
                {"trajectory_steps": "2", "submitted": "True", "submitted_at_step_limit": "False"},
            )
            self.assertEqual(
                {key: row[key] for key in (
                    "retrieval_primary_subtype", "auxiliary_tags", "retrieval_outcome",
                    "boundary_flag", "confidence", "decision_status", "first_divergence_step",
                    "relevant_sql_steps", "sql_evidence", "observation_evidence", "gold_evidence_basis",
                )},
                {
                    "retrieval_primary_subtype": "INDETERMINATE", "auxiliary_tags": "[]",
                    "retrieval_outcome": "UNOBSERVED", "boundary_flag": "NONE",
                    "confidence": "indeterminate", "decision_status": "needs_review",
                    "first_divergence_step": "", "relevant_sql_steps": "[]",
                    "sql_evidence": "", "observation_evidence": "", "gold_evidence_basis": "",
                },
            )
            self.assertIn("semantic review required", row["rationale"])
            with self.assertRaises(OutputCollisionError):
                write_preparation_files([bundle], evidence, root / "other.csv")

            invalid_parent = root / "must-not-exist"
            with self.assertRaises(InputError):
                write_preparation_files([object()], invalid_parent / "evidence.jsonl", invalid_parent / "review.csv")
            self.assertFalse(invalid_parent.exists())

    def test_write_preparation_files_accepts_review_fields_over_default_csv_limit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_type = __import__(
                "experiments.failure_analysis.retrieval_models",
                fromlist=["RetrievalEvidenceBundle"],
            ).RetrievalEvidenceBundle
            # The review template carries the question verbatim in one CSV cell;
            # this exceeds csv.field_size_limit()'s default 131072-byte bound.
            bundle = replace(
                bundle_type.fixture_for_test(),
                incident="incident_1",
                question="q" * 131073,
            )
            evidence = root / "evidence.jsonl"
            review = root / "review.csv"
            previous_csv_limit = csv.field_size_limit()

            write_preparation_files([bundle], evidence, review)

            self.assertTrue(evidence.is_file())
            self.assertTrue(review.is_file())
            review_text = review.read_text(encoding="utf-8")
            self.assertIn("q" * 131073, review_text)
            self.assertEqual(csv.field_size_limit(), previous_csv_limit)

    def test_write_preparation_files_is_pair_safe_and_validates_bundle_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_type = __import__(
                "experiments.failure_analysis.retrieval_models",
                fromlist=["RetrievalEvidenceBundle"],
            ).RetrievalEvidenceBundle
            bundle = replace(bundle_type.fixture_for_test(), incident="incident_1")

            existing_template = root / "existing" / "review.csv"
            existing_template.parent.mkdir()
            existing_template.write_text("user file", encoding="utf-8")
            with self.assertRaises(OutputCollisionError):
                write_preparation_files(
                    [bundle], root / "new" / "evidence.jsonl", existing_template
                )
            self.assertFalse((root / "new").exists())

            alias = root / "alias" / "nested" / ".." / "out.jsonl"
            with self.assertRaises(InputError):
                write_preparation_files([bundle], root / "alias" / "out.jsonl", alias)
            self.assertFalse((root / "alias").exists())

            evidence = root / "publish" / "evidence.jsonl"
            template = root / "publish" / "review.csv"
            real_link = os.link
            calls = 0

            def fail_second_link(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    raise OSError("simulated second publication failure")
                return real_link(source, destination)

            with patch(
                "experiments.failure_analysis.retrieval_extract.os.link",
                side_effect=fail_second_link,
            ):
                with self.assertRaises(InputError):
                    write_preparation_files([bundle], evidence, template)
            self.assertFalse(evidence.exists())
            self.assertFalse(template.exists())

            race_evidence = root / "race" / "evidence.jsonl"
            race_template = root / "race" / "review.csv"
            calls = 0

            def race_second_link(source, destination):
                nonlocal calls
                calls += 1
                if calls == 2:
                    Path(destination).write_text("user file", encoding="utf-8")
                    raise FileExistsError("simulated publication race")
                return real_link(source, destination)

            with patch(
                "experiments.failure_analysis.retrieval_extract.os.link",
                side_effect=race_second_link,
            ):
                with self.assertRaises(OutputCollisionError):
                    write_preparation_files([bundle], race_evidence, race_template)
            self.assertFalse(race_evidence.exists())
            self.assertEqual(race_template.read_text(encoding="utf-8"), "user file")

            invalid_bundles = (
                replace(bundle, question_index=True),
                replace(bundle, agent_source_index=True),
                replace(bundle, reward_official=math.nan),
                replace(bundle, question_fingerprint_sha256="A" * 64),
                replace(bundle, trajectory_steps=True),
                replace(bundle, trajectory_steps=-1),
                replace(bundle, submitted=1),
                replace(bundle, submitted_at_step_limit=0),
                replace(bundle, trajectory_steps=0, submitted=True, submitted_at_step_limit=True),
                replace(bundle, submitted=False, submitted_at_step_limit=True),
                replace(bundle, query_steps=[QueryStep(1, "SELECT 1", "[]", True)]),
                replace(bundle, query_steps=(QueryStep(0, "SELECT 1", "[]", True),)),
                replace(bundle, query_steps=(QueryStep(True, "SELECT 1", "[]", True),)),
                replace(bundle, query_steps=(QueryStep(1, 2, "[]", True),)),
                replace(bundle, query_steps=(QueryStep(1, "SELECT 1", "[]", "true"),)),
            )
            for index, invalid_bundle in enumerate(invalid_bundles):
                output_dir = root / f"invalid-{index}"
                with self.assertRaises(InputError):
                    write_preparation_files(
                        [invalid_bundle], output_dir / "evidence.jsonl", output_dir / "review.csv"
                    )
                self.assertFalse(output_dir.exists())

    def test_write_preparation_files_rejects_inconsistent_trajectory_contract_before_parent_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_type = __import__(
                "experiments.failure_analysis.retrieval_models",
                fromlist=["RetrievalEvidenceBundle"],
            ).RetrievalEvidenceBundle
            bundle = replace(bundle_type.fixture_for_test(), incident="incident_1")
            invalid_bundles = (
                (
                    "submitted-without-steps",
                    replace(
                        bundle,
                        trajectory_steps=0,
                        submitted=True,
                        submitted_at_step_limit=False,
                    ),
                    "trajectory_steps",
                ),
                (
                    "query-step-out-of-range",
                    replace(
                        bundle,
                        trajectory_steps=1,
                        query_steps=(QueryStep(2, "SELECT 1", "[]", True),),
                    ),
                    "query step",
                ),
            )
            for label, invalid_bundle, expected_field in invalid_bundles:
                output_dir = root / label
                with self.subTest(label=label):
                    with self.assertRaises(InputError) as raised:
                        write_preparation_files(
                            [invalid_bundle],
                            output_dir / "evidence.jsonl",
                            output_dir / "review.csv",
                        )
                    self.assertIn(expected_field, str(raised.exception))
                    self.assertFalse(output_dir.exists())

    def test_write_preparation_files_rejects_unencodable_values_before_parent_creation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            bundle_type = __import__(
                "experiments.failure_analysis.retrieval_models",
                fromlist=["RetrievalEvidenceBundle"],
            ).RetrievalEvidenceBundle
            bundle = replace(bundle_type.fixture_for_test(), incident="incident_1")
            invalid_bundles = (
                replace(bundle, question="bad\ud800"),
                replace(bundle, golden_answer={"nested": ["bad\ud800"]}),
                replace(bundle, query_steps=(QueryStep(1, "bad\ud800", "ok", True),)),
                replace(bundle, query_steps=(QueryStep(1, "ok", "bad\ud800", True),)),
            )
            for index, invalid_bundle in enumerate(invalid_bundles):
                output_dir = root / f"surrogate-{index}"
                with self.assertRaises(InputError):
                    write_preparation_files(
                        [invalid_bundle],
                        output_dir / "evidence.jsonl",
                        output_dir / "review.csv",
                    )
                self.assertFalse(output_dir.exists())


if __name__ == "__main__":
    unittest.main()
