import csv
import hashlib
import io
import json
import re
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path

from experiments.failure_analysis.aggregate_failures import (
    EXPECTED_INCIDENTS,
    aggregate,
    load_incident_rows,
    main,
    parse_args,
)
from experiments.failure_analysis.models import InputError, OutputCollisionError


EXPECTED = (
    "incident_5",
    "incident_38",
    "incident_34",
    "incident_39",
    "incident_55",
    "incident_134",
    "incident_166",
    "incident_322",
)


def fingerprint(incident, index):
    return hashlib.sha256(f"{incident}:{index}".encode()).hexdigest()


def record(incident, index=0, **overrides):
    value = {
        "schema_version": "failure_attribution_v1",
        "taxonomy_version": "taxonomy_v1",
        "incident": incident,
        "question_index": index,
        "question_fingerprint_sha256": fingerprint(incident, index),
        "question_text_fingerprint_sha256": hashlib.sha256(
            f"text:{incident}:{index}".encode()
        ).hexdigest(),
        "reward_official": 1.0 if index == 0 else 0.4,
        "primary_cause_candidate": None if index == 0 else "ANSWER",
        "secondary_cause_candidates": [] if index == 0 else ["STEP_LIMIT"],
        "confidence": "high" if index == 0 else "medium",
        "evidence": [] if index == 0 else [{"kind": "answer_mismatch"}],
        "needs_human_review": False,
        "reviewed_primary": None,
        "reviewed_secondary": [],
        "review_status": "unreviewed",
    }
    value.update(overrides)
    return value


def write_jsonl(path, rows):
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(
                json.dumps(
                    row,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            handle.write("\n")


def valid_inputs(root):
    root = Path(root)
    paths = []
    for incident in EXPECTED:
        path = root / f"{incident}_attribution.jsonl"
        rows = [record(incident)]
        if incident == "incident_5":
            rows = [
                record(
                    incident,
                    1,
                    reviewed_primary="ANSWER",
                    review_status="confirmed",
                ),
                record(incident, 0),
            ]
        write_jsonl(path, rows)
        paths.append(path)
    return paths


def incident_number(value):
    return int(value.rsplit("_", 1)[1])


class AggregateTest(unittest.TestCase):
    def test_expected_incidents_and_argument_contract_are_frozen(self):
        self.assertEqual(EXPECTED_INCIDENTS, EXPECTED)
        args = parse_args(
            [
                option
                for index in range(8)
                for option in ("--input-jsonl", f"incident-{index}.jsonl")
            ]
            + ["--output-dir", "aggregate-output"]
        )
        self.assertEqual(len(args.input_jsonl), 8)
        self.assertEqual(args.output_dir, Path("aggregate-output"))

    def test_missing_and_duplicate_incidents_are_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = valid_inputs(temporary)
            with self.assertRaisesRegex(InputError, "missing incident"):
                load_incident_rows(paths[:-1])
            with self.assertRaisesRegex(InputError, "duplicate incident"):
                load_incident_rows(paths[:-1] + [paths[0]])

    def test_mixed_schema_and_taxonomy_versions_are_rejected(self):
        cases = {
            "schema": {"schema_version": "failure_attribution_v2"},
            "taxonomy": {"taxonomy_version": "taxonomy_v2"},
        }
        for name, override in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as temporary:
                paths = valid_inputs(temporary)
                write_jsonl(paths[0], [record("incident_5", **override)])
                with self.assertRaisesRegex(InputError, name):
                    load_incident_rows(paths)

    def test_duplicate_stable_identity_is_rejected(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = valid_inputs(temporary)
            duplicate = record("incident_5", 7)
            write_jsonl(paths[0], [duplicate, dict(duplicate)])
            with self.assertRaisesRegex(InputError, "duplicate stable identity"):
                load_incident_rows(paths)

    def test_malformed_jsonl_identifies_path_and_line(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = valid_inputs(temporary)
            paths[3].write_text('{"broken"\n', encoding="utf-8")
            with self.assertRaisesRegex(InputError, rf"{paths[3].name}.*line 1"):
                load_incident_rows(paths)

    def test_existing_output_directory_is_refused_and_preserved(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = valid_inputs(root)
            output_dir = root / "aggregate"
            output_dir.mkdir()
            marker = output_dir / "user-data.txt"
            marker.write_text("preserve", encoding="utf-8")
            with self.assertRaises(OutputCollisionError):
                aggregate(paths, output_dir)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")

    def test_valid_aggregation_is_sorted_stable_and_totals_match_summary(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = list(reversed(valid_inputs(root)))
            output_dir = root / "aggregate"
            written = aggregate(paths, output_dir)

            self.assertEqual(
                {path.name for path in written},
                {
                    "all_incidents_attribution.csv",
                    "all_incidents_summary.md",
                },
            )
            self.assertEqual(
                {path.name for path in output_dir.iterdir()},
                {
                    "all_incidents_attribution.csv",
                    "all_incidents_summary.md",
                },
            )

            with (
                output_dir / "all_incidents_attribution.csv"
            ).open(encoding="utf-8", newline="") as handle:
                rows = list(csv.DictReader(handle))

            order = [
                (incident_number(row["incident"]), int(row["question_index"]))
                for row in rows
            ]
            self.assertEqual(order, sorted(order))
            self.assertEqual(len(rows), 9)
            self.assertEqual(
                json.loads(rows[1]["secondary_cause_candidates"]),
                ["STEP_LIMIT"],
            )
            self.assertEqual(
                json.loads(rows[1]["evidence"]),
                [{"kind": "answer_mismatch"}],
            )

            summary = (
                output_dir / "all_incidents_summary.md"
            ).read_text(encoding="utf-8")
            overall_match = re.search(r"Overall record count:\s*(\d+)", summary)
            self.assertIsNotNone(overall_match)
            self.assertEqual(int(overall_match.group(1)), len(rows))

            per_incident = {
                incident: int(count)
                for incident, count in re.findall(
                    r"^\| (incident_\d+) \| (\d+) \|$",
                    summary,
                    flags=re.MULTILINE,
                )
            }
            self.assertEqual(set(per_incident), set(EXPECTED))
            self.assertEqual(sum(per_incident.values()), len(rows))
            self.assertEqual(per_incident["incident_5"], 2)
            for incident in set(EXPECTED) - {"incident_5"}:
                self.assertEqual(per_incident[incident], 1)

            for heading in (
                "Official reward distribution",
                "Primary candidate counts",
                "Reviewed primary counts",
                "Review completion counts",
            ):
                self.assertIn(heading, summary)

    def test_main_returns_codes_2_and_4(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = valid_inputs(root)

            missing_args = [
                option
                for path in paths[:-1]
                for option in ("--input-jsonl", str(path))
            ] + ["--output-dir", str(root / "missing-output")]
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(missing_args), 2)
            self.assertIn("eight", stderr.getvalue().lower())

            output_dir = root / "existing-output"
            output_dir.mkdir()
            collision_args = [
                option
                for path in paths
                for option in ("--input-jsonl", str(path))
            ] + ["--output-dir", str(output_dir)]
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                self.assertEqual(main(collision_args), 4)
            self.assertIn("exists", stderr.getvalue().lower())


if __name__ == "__main__":
    unittest.main()
