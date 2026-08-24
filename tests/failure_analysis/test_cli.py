import csv
import io
import json
import socket
import subprocess
import sys
import tempfile
import types
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from experiments.failure_analysis.analyze_failures import main, parse_args


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


def taxonomy():
    return {
        "taxonomy_version": "taxonomy_v1",
        "categories": [
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
        ],
        "always_human_review": ["EVALUATOR", "GOLD", "UNKNOWN"],
        "loop_and_step_limit_normally_secondary": True,
        "review_sampling": {
            "seed": 20260720,
            "rate": 0.1,
            "minimum_per_nonempty_category": 1,
        },
        "calibration": [],
    }


def question():
    return {
        "answer": "server01",
        "context": "context",
        "end_alert": "alert-end",
        "end_entities": ["server01"],
        "question": "Which server was involved?",
        "shortest_alert_path": ["node-a", "node-b"],
        "solution": "server01 was involved",
        "start_alert": "alert-start",
        "start_entities": ["user-a"],
    }


def submit_step():
    return {
        "action": "server01",
        "observation": "",
        "reward": 1.0,
        "done": True,
        "info": {
            "query_success": True,
            "submit": True,
            "submitted_answer": "server01",
            "reward": 1.0,
            "check_ans_response": "correct",
            "check_ans_reflection": "correct",
            "check_sol_response": "correct",
            "check_sol_reflection": "correct",
        },
    }


def write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True),
        encoding="utf-8",
    )


def make_fixture(root):
    root = Path(root)
    item = question()
    questions_path = root / "questions.json"
    agent_path = root / "agent.json"
    env_path = root / "env.json"
    taxonomy_path = root / "taxonomy.json"
    write_json(questions_path, [item])
    write_json(
        agent_path,
        [
            {
                "nodes": ["node-a", "node-b"],
                "question_dict": item,
                "reward": 1.0,
                "trials": {},
            }
        ],
    )
    write_json(
        env_path,
        [
            {
                "nodes": ["node-a", "node-b"],
                "question": item,
                "reward": 1.0,
                "trajectory": [submit_step()],
            }
        ],
    )
    write_json(taxonomy_path, taxonomy())
    return {
        "agent": agent_path,
        "env": env_path,
        "questions": questions_path,
        "taxonomy": taxonomy_path,
        "output": root / "output",
    }


def argv(paths, *extra):
    return [
        "--agent-json",
        str(paths["agent"]),
        "--env-json",
        str(paths["env"]),
        "--question-json",
        str(paths["questions"]),
        "--incident",
        "incident_5",
        "--output-dir",
        str(paths["output"]),
        "--taxonomy",
        str(paths["taxonomy"]),
        *extra,
    ]


def forbidden_activity(*args, **kwargs):
    raise AssertionError(f"forbidden external activity: {args!r} {kwargs!r}")


class ExternalActivityGuard:
    def __init__(self, git_returncode=0, git_stdout="abc123\n"):
        self.git_result = subprocess.CompletedProcess(
            ["git", "rev-parse", "HEAD"],
            git_returncode,
            stdout=git_stdout,
            stderr="",
        )
        self.git_patch = patch(
            "experiments.failure_analysis.analyze_failures.subprocess.run",
            return_value=self.git_result,
        )
        self.create_connection_patch = patch(
            "socket.create_connection",
            side_effect=forbidden_activity,
        )
        self.socket_connect_patch = patch.object(
            socket.socket,
            "connect",
            forbidden_activity,
        )
        connector = types.ModuleType("mysql.connector")
        connector.connect = forbidden_activity
        mysql = types.ModuleType("mysql")
        mysql.connector = connector
        pymysql = types.ModuleType("pymysql")
        pymysql.connect = forbidden_activity
        self.module_patch = patch.dict(
            sys.modules,
            {
                "mysql": mysql,
                "mysql.connector": connector,
                "pymysql": pymysql,
            },
        )

    def __enter__(self):
        self.git_mock = self.git_patch.start()
        self.create_connection_patch.start()
        self.socket_connect_patch.start()
        self.module_patch.start()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.module_patch.stop()
        self.socket_connect_patch.stop()
        self.create_connection_patch.stop()
        self.git_patch.stop()

    def assert_only_git_rev_parse(self):
        self.git_mock.assert_called_once()
        positional, keyword = self.git_mock.call_args
        self.assert_git_command(positional[0])
        self.assert_git_options(keyword)

    @staticmethod
    def assert_git_command(command):
        if command != ["git", "rev-parse", "HEAD"]:
            raise AssertionError(f"unexpected subprocess command: {command!r}")

    @staticmethod
    def assert_git_options(options):
        if options.get("shell") is True:
            raise AssertionError("git command must not use a shell")
        if options.get("check") is not False:
            raise AssertionError("git command must use check=False")
        if options.get("text") is not True:
            raise AssertionError("git command must use text=True")


class CliTest(unittest.TestCase):
    def test_help_lists_contract_defaults_and_has_no_overwrite(self):
        stdout = io.StringIO()
        with self.assertRaises(SystemExit) as raised, redirect_stdout(stdout):
            parse_args(["--help"])
        self.assertEqual(raised.exception.code, 0)
        help_text = stdout.getvalue()
        for option in (
            "--agent-json",
            "--env-json",
            "--question-json",
            "--incident",
            "--output-dir",
            "--taxonomy",
            "--review-csv",
            "--max-steps",
        ):
            self.assertIn(option, help_text)
        self.assertIn("15", help_text)
        self.assertIn("taxonomy_v1.json", help_text)
        self.assertNotIn("overwrite", help_text.lower())

    def test_default_taxonomy_is_relative_to_the_cli_module(self):
        args = parse_args(
            [
                "--agent-json",
                "agent.json",
                "--env-json",
                "env.json",
                "--question-json",
                "questions.json",
                "--incident",
                "incident_5",
                "--output-dir",
                "report",
            ]
        )
        expected = (
            Path(sys.modules[main.__module__].__file__).resolve().with_name(
                "taxonomy_v1.json"
            )
        )
        self.assertEqual(args.taxonomy, expected)
        self.assertEqual(args.max_steps, 15)

    def test_success_writes_outputs_without_external_activity(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(temporary)
            stderr = io.StringIO()
            with ExternalActivityGuard() as guard, redirect_stderr(stderr):
                exit_code = main(argv(paths))

            self.assertEqual(exit_code, 0)
            self.assertEqual(stderr.getvalue(), "")
            self.assertTrue(paths["output"].is_dir())
            self.assertEqual(
                {path.name for path in paths["output"].iterdir()},
                {
                    "taxonomy_v1.json",
                    "incident_5_attribution.jsonl",
                    "incident_5_attribution.csv",
                    "incident_5_summary.md",
                    "human_review.csv",
                    "incident_5_analysis_manifest.json",
                },
            )
            manifest = json.loads(
                (
                    paths["output"] / "incident_5_analysis_manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertEqual(manifest["git_commit"], "abc123")
            self.assertFalse(manifest["review_applied"])
            self.assertEqual(
                set(manifest["sources"]),
                {"agent", "env", "question"},
            )
            self.assertEqual(
                Path(manifest["sources"]["agent"]["path"]),
                paths["agent"],
            )
            self.assertEqual(
                Path(manifest["sources"]["env"]["path"]),
                paths["env"],
            )
            self.assertEqual(
                Path(manifest["sources"]["question"]["path"]),
                paths["questions"],
            )
            guard.assert_only_git_rev_parse()

    def test_missing_output_parent_is_created_only_after_successful_mapping(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = make_fixture(root)
            paths["output"] = root / "new-parent" / "output"

            with ExternalActivityGuard() as guard:
                self.assertEqual(main(argv(paths)), 0)
            self.assertTrue(paths["output"].is_dir())
            guard.assert_only_git_rev_parse()

        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            paths = make_fixture(root)
            write_json(paths["agent"], [])
            paths["output"] = root / "failed-parent" / "output"

            stderr = io.StringIO()
            with ExternalActivityGuard() as guard, redirect_stderr(stderr):
                self.assertEqual(main(argv(paths)), 3)
            self.assertFalse(paths["output"].parent.exists())
            self.assertIn("agent", stderr.getvalue().lower())
            guard.git_mock.assert_not_called()

    def test_unavailable_git_commit_is_recorded_as_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(temporary)
            with ExternalActivityGuard(git_returncode=1, git_stdout="") as guard:
                self.assertEqual(main(argv(paths)), 0)
            manifest = json.loads(
                (
                    paths["output"] / "incident_5_analysis_manifest.json"
                ).read_text(encoding="utf-8")
            )
            self.assertIsNone(manifest["git_commit"])
            guard.assert_only_git_rev_parse()

    def test_invalid_input_returns_code_2_without_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(temporary)
            paths["agent"] = Path(temporary) / "missing-agent.json"
            stderr = io.StringIO()
            with ExternalActivityGuard() as guard, redirect_stderr(stderr):
                exit_code = main(argv(paths))
            self.assertEqual(exit_code, 2)
            self.assertIn("missing-agent.json", stderr.getvalue())
            self.assertFalse(paths["output"].exists())
            guard.git_mock.assert_not_called()

    def test_mapping_failure_returns_code_3_without_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(temporary)
            write_json(paths["agent"], [])
            stderr = io.StringIO()
            with ExternalActivityGuard() as guard, redirect_stderr(stderr):
                exit_code = main(argv(paths))
            self.assertEqual(exit_code, 3)
            self.assertIn("agent", stderr.getvalue().lower())
            self.assertFalse(paths["output"].exists())
            guard.git_mock.assert_not_called()

    def test_existing_output_returns_code_4_without_replacing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(temporary)
            paths["output"].mkdir()
            marker = paths["output"] / "user-data.txt"
            marker.write_text("preserve", encoding="utf-8")
            stderr = io.StringIO()
            with ExternalActivityGuard() as guard, redirect_stderr(stderr):
                exit_code = main(argv(paths))
            self.assertEqual(exit_code, 4)
            self.assertEqual(marker.read_text(encoding="utf-8"), "preserve")
            self.assertIn("exists", stderr.getvalue().lower())
            guard.git_mock.assert_not_called()

    def test_invalid_review_returns_code_5_without_creating_output(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(temporary)
            review_path = Path(temporary) / "review.csv"
            with review_path.open("w", encoding="utf-8", newline="") as handle:
                writer = csv.DictWriter(handle, fieldnames=REVIEW_FIELDS)
                writer.writeheader()
                writer.writerow(
                    {
                        "incident": "incident_5",
                        "question_index": 999,
                        "question_fingerprint_sha256": "f" * 64,
                        "candidate_primary": "ANSWER",
                        "candidate_secondary": "[]",
                        "reviewed_primary": "ANSWER",
                        "reviewed_secondary": "[]",
                        "review_status": "confirmed",
                        "review_notes": "wrong identity",
                    }
                )
            stderr = io.StringIO()
            with ExternalActivityGuard() as guard, redirect_stderr(stderr):
                exit_code = main(argv(paths, "--review-csv", str(review_path)))
            self.assertEqual(exit_code, 5)
            self.assertIn("identity", stderr.getvalue().lower())
            self.assertFalse(paths["output"].exists())
            guard.git_mock.assert_not_called()

    def test_unexpected_exception_propagates(self):
        with tempfile.TemporaryDirectory() as temporary:
            paths = make_fixture(temporary)
            with ExternalActivityGuard(), patch(
                "experiments.failure_analysis.analyze_failures.write_outputs",
                side_effect=RuntimeError("unexpected"),
            ), self.assertRaisesRegex(RuntimeError, "unexpected"):
                main(argv(paths))


if __name__ == "__main__":
    unittest.main()
