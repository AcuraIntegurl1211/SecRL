from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path


if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))


from experiments.failure_analysis.attribution import (  # noqa: E402
    attribute_record,
    load_taxonomy,
)
from experiments.failure_analysis.features import extract_features  # noqa: E402
from experiments.failure_analysis.identity import load_json, map_logs  # noqa: E402
from experiments.failure_analysis.models import (  # noqa: E402
    AnalysisError,
    InputError,
    OutputCollisionError,
)
from experiments.failure_analysis.reporting import (  # noqa: E402
    apply_human_review,
    build_row,
    write_outputs,
)


DEFAULT_TAXONOMY = Path(__file__).resolve().with_name("taxonomy_v1.json")
REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Deterministic offline SecRL failure attribution",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--agent-json",
        required=True,
        type=Path,
        help="read-only agent result JSON",
    )
    parser.add_argument(
        "--env-json",
        required=True,
        type=Path,
        help="read-only environment trajectory JSON",
    )
    parser.add_argument(
        "--question-json",
        required=True,
        type=Path,
        help="canonical incident question JSON",
    )
    parser.add_argument(
        "--incident",
        required=True,
        help="incident identifier, for example incident_5",
    )
    parser.add_argument(
        "--output-dir",
        required=True,
        type=Path,
        help="new output directory; existing paths are refused",
    )
    parser.add_argument(
        "--taxonomy",
        type=Path,
        default=DEFAULT_TAXONOMY,
        help="frozen taxonomy definition",
    )
    parser.add_argument(
        "--review-csv",
        type=Path,
        default=None,
        help="optional completed human-review CSV",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=15,
        help="experiment step limit used for feature extraction",
    )
    return parser.parse_args(argv)


def _require_file(path: Path, field: str) -> None:
    if not path.is_file():
        raise InputError(f"{field} is not a readable file: {path}")


def _validate_paths(args: argparse.Namespace) -> None:
    _require_file(args.agent_json, "agent JSON")
    _require_file(args.env_json, "env JSON")
    _require_file(args.question_json, "question JSON")
    _require_file(args.taxonomy, "taxonomy")
    if args.review_csv is not None:
        _require_file(args.review_csv, "review CSV")
    if args.output_dir.exists():
        raise OutputCollisionError(
            f"output path already exists: {args.output_dir}"
        )
    if not args.output_dir.parent.is_dir():
        raise InputError(
            f"output parent is not a directory: {args.output_dir.parent}"
        )
    if not args.incident.strip():
        raise InputError("incident must not be empty")
    if args.max_steps <= 0:
        raise InputError("max-steps must be positive")


def _git_commit() -> str | None:
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=REPOSITORY_ROOT,
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    commit = completed.stdout.strip()
    if completed.returncode != 0 or not commit:
        return None
    return commit


def run(args: argparse.Namespace) -> list[Path]:
    _validate_paths(args)

    agent_entries = load_json(args.agent_json)
    env_entries = load_json(args.env_json)
    questions = load_json(args.question_json)
    taxonomy = load_taxonomy(args.taxonomy)

    mapped = map_logs(
        args.incident,
        agent_entries,
        env_entries,
        questions,
    )
    features = [
        extract_features(item, max_steps=args.max_steps) for item in mapped
    ]
    attributions = [
        attribute_record(item, taxonomy) for item in features
    ]
    rows = [
        build_row(item, attribution, taxonomy["taxonomy_version"])
        for item, attribution in zip(features, attributions)
    ]

    review_applied = args.review_csv is not None
    if args.review_csv is not None:
        apply_human_review(rows, args.review_csv, taxonomy)

    source_paths = {
        "agent_json": args.agent_json,
        "env_json": args.env_json,
        "question_json": args.question_json,
    }
    if args.review_csv is not None:
        source_paths["review_csv"] = args.review_csv

    return write_outputs(
        rows,
        args.taxonomy,
        args.incident,
        args.output_dir,
        source_paths,
        args.max_steps,
        _git_commit(),
        review_applied,
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        run(args)
    except AnalysisError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return exc.exit_code
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
