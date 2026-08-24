# SecRL Failure Attribution Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a deterministic, read-only, offline SecRL failure-attribution CLI that reproduces the approved Incident 5 calibration and emits auditable taxonomy_v1 reports without changing experiment logs or official rewards.

**Architecture:** A small standard-library package separates identity/mapping, feature extraction, attribution policy, and reporting. The CLI validates all inputs and mappings before attribution, then writes every output through temporary files and atomic replacement into a new output directory. Candidate labels remain distinct from imported human-review labels.

**Tech Stack:** Python 3.11 standard library (`argparse`, `csv`, `dataclasses`, `datetime`, `hashlib`, `json`, `math`, `pathlib`, `random`, `re`, `tempfile`, `unittest`); no Docker, MySQL, network, API key, LLM, pandas, or pytest.

---

## Preconditions and fixed constraints

- Work only on `repro/deepseek-pro-flash` after confirming a clean worktree.
- Canonical design: `docs/superpowers/specs/2026-07-20-secrl-failure-attribution-design.md` at commit `d3f2322`.
- Do not rerun Incident 5.
- Do not modify anything under `experiments/final_results/`.
- Do not import `experiments/run_exp.py`, Agent, Evaluator, Docker, or MySQL code.
- Do not overwrite an output directory or any output file.
- Never change `reward_official`.
- Before every commit: show the diff, run the stated tests, list staged files, and obtain explicit user approval.
- Do not push without separate explicit user approval.

## File map

Create these production files:

```text
experiments/failure_analysis/__init__.py
experiments/failure_analysis/analyze_failures.py
experiments/failure_analysis/aggregate_failures.py
experiments/failure_analysis/models.py
experiments/failure_analysis/identity.py
experiments/failure_analysis/features.py
experiments/failure_analysis/attribution.py
experiments/failure_analysis/reporting.py
experiments/failure_analysis/taxonomy_v1.json
```

Create these test files:

```text
tests/failure_analysis/__init__.py
tests/failure_analysis/helpers.py
tests/failure_analysis/test_models.py
tests/failure_analysis/test_identity.py
tests/failure_analysis/test_features.py
tests/failure_analysis/test_attribution.py
tests/failure_analysis/test_reporting.py
tests/failure_analysis/test_cli.py
tests/failure_analysis/test_aggregate.py
```

Responsibilities and public interfaces:

```text
models.py       immutable record/evidence dataclasses and typed exceptions
identity.py     load_json(), sha256_file(), question_identity(), map_logs()
features.py     normalize_*(), extract_features()
attribution.py  load_taxonomy(), attribute_record(), apply_human_review()
reporting.py    build_row(), select_review_rows(), apply_human_review(), write_outputs()
analyze_failures.py  parse_args(), run(), main()
aggregate_failures.py  load_incident_rows(), aggregate(), main()
```

## Task 1: Establish package, models, and frozen taxonomy

**Files:**

- Create: `experiments/failure_analysis/__init__.py`
- Create: `experiments/failure_analysis/models.py`
- Create: `experiments/failure_analysis/taxonomy_v1.json`
- Create: `tests/failure_analysis/__init__.py`
- Create: `tests/failure_analysis/test_models.py`

- [ ] **Step 1: Confirm the baseline before editing**

Run:

```bash
cd /home/acuraintegurl/Desktop/SecRL-git
git status --short --branch
git log -1 --oneline --decorate
```

Expected: branch `repro/deepseek-pro-flash`, `ahead 1`, clean worktree, and HEAD `d3f2322`.

- [ ] **Step 2: Write the failing model/taxonomy test**

Create `tests/failure_analysis/__init__.py` as an empty file and create `tests/failure_analysis/test_models.py` with:

```python
import json
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path

from experiments.failure_analysis.models import Evidence, QuestionIdentity


ROOT = Path(__file__).resolve().parents[2]
TAXONOMY = ROOT / "experiments/failure_analysis/taxonomy_v1.json"


class ModelsTest(unittest.TestCase):
    def test_identity_is_immutable(self):
        identity = QuestionIdentity("incident_5", 3, "a" * 64, "b" * 64)
        with self.assertRaises(FrozenInstanceError):
            identity.question_index = 4

    def test_evidence_serializes_with_traceable_location(self):
        evidence = Evidence("sql_error", 9, "env", "trajectory[8].observation", "bad SQL", False)
        self.assertEqual(evidence.as_dict()["step"], 9)

    def test_taxonomy_is_frozen_v1(self):
        data = json.loads(TAXONOMY.read_text(encoding="utf-8"))
        self.assertEqual(data["taxonomy_version"], "taxonomy_v1")
        self.assertEqual(
            data["categories"],
            ["DATA", "SQL_EXEC", "SQL_RETRIEVAL", "NAVIGATION", "LOOP", "STEP_LIMIT",
             "REASONING", "ANSWER", "EVALUATOR", "GOLD", "INFRA", "UNKNOWN"],
        )
        self.assertEqual(data["always_human_review"], ["EVALUATOR", "GOLD", "UNKNOWN"])
        self.assertEqual(len(data["calibration"]), 10)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 3: Run the test and verify the expected red state**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_models -v
```

Expected: import failure because `experiments.failure_analysis.models` does not exist.

- [ ] **Step 4: Add the minimal immutable data contracts**

Create an empty `experiments/failure_analysis/__init__.py`. Create `models.py` with these exact contracts:

```python
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any


SCHEMA_VERSION = "failure_attribution_v1"


class AnalysisError(Exception):
    exit_code = 2


class InputError(AnalysisError):
    exit_code = 2


class MappingError(AnalysisError):
    exit_code = 3


class OutputCollisionError(AnalysisError):
    exit_code = 4


class ReviewError(AnalysisError):
    exit_code = 5


@dataclass(frozen=True)
class QuestionIdentity:
    incident: str
    question_index: int
    question_fingerprint_sha256: str
    question_text_fingerprint_sha256: str


@dataclass(frozen=True)
class Evidence:
    kind: str
    step: int | None
    source: str
    field: str
    excerpt: str
    excerpt_truncated: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class MappedQuestion:
    identity: QuestionIdentity
    question: dict[str, Any]
    agent: dict[str, Any]
    env: dict[str, Any]
    agent_source_index: int
    env_source_index: int


@dataclass
class FeatureRecord:
    mapped: MappedQuestion
    reward_official: float
    submitted_answer: str
    sql_total: int
    sql_success: int
    sql_failure: int
    empty_result_count: int
    duplicate_query_count: int
    steps: int
    max_steps: int
    submitted: bool
    submitted_at_step_limit: bool
    gold_evidence_match: str
    gold_evidence_steps: list[int] = field(default_factory=list)
    evaluator_fields_complete: bool = False
    agent_prompt_tokens: int | None = None
    agent_completion_tokens: int | None = None
    agent_total_tokens: int | None = None
    evaluator_tokens: int | None = None
    evidence: list[Evidence] = field(default_factory=list)


@dataclass
class Attribution:
    primary_cause_candidate: str | None
    primary_cause_status: str
    secondary_cause_candidates: list[str]
    confidence: str
    needs_human_review: bool
    human_review_reasons: list[str]
    reviewed_primary: str | None = None
    reviewed_secondary: list[str] = field(default_factory=list)
    review_status: str = "unreviewed"
    review_notes: str = ""
```

- [ ] **Step 5: Add the frozen taxonomy data**

Create `taxonomy_v1.json` with this complete content:

```json
{
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
    "UNKNOWN"
  ],
  "definitions": {
    "DATA": "Missing or incomplete data, empty required tables, or unavailable layer evidence.",
    "SQL_EXEC": "SQL syntax, table, column, or database execution failure.",
    "SQL_RETRIEVAL": "Executable SQL using the wrong table, join, filter, identifier, or time condition.",
    "NAVIGATION": "Incorrect incident or alert starting point, entity selection, or investigation path.",
    "LOOP": "Repeated equivalent queries without progress from observations.",
    "STEP_LIMIT": "Incomplete investigation or answer at the configured maximum step count.",
    "REASONING": "Evidence obtained but timeline, entity relation, or multi-step reasoning is incorrect.",
    "ANSWER": "Correct evidence obtained but the final answer is incomplete, overbroad, malformed, or contains a wrong entity.",
    "EVALUATOR": "Submitted and golden answers are materially equivalent but evaluation appears incorrect.",
    "GOLD": "Question, context, golden answer, solution, or database evidence appears inconsistent.",
    "INFRA": "API, parser, submission, logging, mapping, duplication, or execution infrastructure failure.",
    "UNKNOWN": "Available evidence is insufficient for a reliable cause."
  },
  "always_human_review": ["EVALUATOR", "GOLD", "UNKNOWN"],
  "loop_and_step_limit_normally_secondary": true,
  "review_sampling": {
    "seed": 20260720,
    "fraction_per_other_primary_category": 0.1,
    "minimum_per_nonempty_category": 1
  },
  "calibration": [
    {
      "incident": "incident_5",
      "question_index": 13,
      "question_fingerprint_sha256": "f013d6956c8913bde432da326917825d89c1e171178860d9234935afa1fa3641",
      "expected_primary": null,
      "review_status": "confirmed"
    },
    {
      "incident": "incident_5",
      "question_index": 5,
      "question_fingerprint_sha256": "428dd97f0387344e9772c82c1c250b31349d963acc62383525f2ef4249a228ff",
      "expected_primary": null,
      "review_status": "confirmed"
    },
    {
      "incident": "incident_5",
      "question_index": 26,
      "question_fingerprint_sha256": "ce13a820fdc04738aa3cea8b34c57cb7c512eeddbde39b7e3baa8debad1410cd",
      "expected_primary": null,
      "review_status": "confirmed"
    },
    {
      "incident": "incident_5",
      "question_index": 10,
      "question_fingerprint_sha256": "c625cb32197d9e5b136c67bef9324a1eecd18f52661121580cb0177ca7addc2b",
      "expected_primary": "NAVIGATION",
      "review_status": "confirmed"
    },
    {
      "incident": "incident_5",
      "question_index": 34,
      "question_fingerprint_sha256": "22c76729415a21377e7ecdd930ad9f19d9adf82f95d83d26075698a6e2d2bda1",
      "expected_primary": "NAVIGATION",
      "review_status": "confirmed"
    },
    {
      "incident": "incident_5",
      "question_index": 55,
      "question_fingerprint_sha256": "a7e0ef2b978187a4e279d09987e5a05ff924d4edfed723fab5133f763e6e41a5",
      "expected_primary": "GOLD",
      "review_status": "human_review_required"
    },
    {
      "incident": "incident_5",
      "question_index": 79,
      "question_fingerprint_sha256": "15c2e6b291ad2d8f6b5e99b9576667de284a1743bb607536f763f3abe43258f8",
      "expected_primary": "GOLD",
      "review_status": "human_review_required"
    },
    {
      "incident": "incident_5",
      "question_index": 23,
      "question_fingerprint_sha256": "6ed33dc5f7f4691e9eb45a4729ebecf11c65345e701296b6100ddf886d3b12ea",
      "expected_primary": "NAVIGATION",
      "review_status": "confirmed"
    },
    {
      "incident": "incident_5",
      "question_index": 65,
      "question_fingerprint_sha256": "816da8a6b0bfa76ccb693433c4a1965d2d5736b9ed20c3087265eee730ebaa4a",
      "expected_primary": "GOLD",
      "review_status": "human_review_required"
    },
    {
      "incident": "incident_5",
      "question_index": 80,
      "question_fingerprint_sha256": "9c18a3c122464e3c0b58c5b3109082161c93c9b3cd5e3e723947c35e2561cd3f",
      "expected_primary": "NAVIGATION",
      "review_status": "confirmed"
    }
  ]
}
```

- [ ] **Step 6: Verify green state and JSON syntax**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_models -v
python -m json.tool experiments/failure_analysis/taxonomy_v1.json >/dev/null
```

Expected: 3 tests pass; JSON command exits 0.

- [ ] **Step 7: Review checkpoint and commit only after approval**

Run the full new test directory, show the diff, and stage only Task 1 files. Proposed commit message:

```text
Add failure attribution data contracts
```

## Task 2: Implement canonical identity and strict independent mapping

**Files:**

- Create: `experiments/failure_analysis/identity.py`
- Create: `tests/failure_analysis/helpers.py`
- Create: `tests/failure_analysis/test_identity.py`

- [ ] **Step 1: Add deterministic fixtures and failing mapping tests**

In `helpers.py`, define `question(text, answer="answer", nodes=None)` returning a complete small question dictionary; `agent_entry(q, reward=0.0)` returning `{"question_dict": q, "reward": reward}`; and `env_entry(q, reward=0.0, trajectory=None)` returning the same identity plus `trajectory`.

In `test_identity.py`, add tests that assert:

```python
self.assertEqual(canonical_json({"é": 1, "a": 2}), '{"a":2,"é":1}')
self.assertEqual(len(question_identity("incident_5", 0, q).question_fingerprint_sha256), 64)
self.assertEqual(map_logs("incident_5", [agent_entry(q)], [env_entry(q)], [q])[0].identity.question_index, 0)
```

Also include separate tests proving:

```text
1. shuffled Agent and Env arrays still map by full fingerprint;
2. duplicate nodes with different question dictionaries remain distinct;
3. missing Agent entry raises MappingError;
4. extra Env entry raises MappingError;
5. duplicate full question fingerprints raise MappingError;
6. invalid JSON path raises InputError containing that path;
7. a top-level object instead of a list raises InputError.
```

- [ ] **Step 2: Run the targeted test and confirm failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_identity -v
```

Expected: import failure for `identity`.

- [ ] **Step 3: Implement exact canonicalization and mapping rules**

Implement these signatures in `identity.py`:

```python
def canonical_json(value: object) -> str
def sha256_text(value: str) -> str
def sha256_file(path: Path) -> str
def load_json(path: Path) -> list[dict[str, Any]]
def question_identity(incident: str, index: int, question: dict[str, Any]) -> QuestionIdentity
def map_logs(
    incident: str,
    agent_entries: list[dict[str, Any]],
    env_entries: list[dict[str, Any]],
    questions: list[dict[str, Any]],
) -> list[MappedQuestion]
```

The implementation must use `json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)`. It must index canonical questions by full fingerprint and question-text fingerprint. Full fingerprint is authoritative; text fingerprint is permitted only when exactly one canonical question matches. Extract a log question from `question_dict`; if absent, accept a dictionary-valued `question`; if neither exists, raise `MappingError` with source name and source index. Reject duplicate canonical full fingerprints, ambiguous text matches, duplicate mapped indexes, missing entries, extras, and non-dictionary list members. Return records in canonical question-index order. Never use `nodes` as a key.

- [ ] **Step 4: Verify targeted and cumulative tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_identity -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
```

Expected: all identity cases and all cumulative tests pass.

- [ ] **Step 5: Review checkpoint and commit only after approval**

Proposed commit message:

```text
Add strict question identity mapping
```

## Task 3: Extract normalized, traceable trajectory features

**Files:**

- Create: `experiments/failure_analysis/features.py`
- Create: `tests/failure_analysis/test_features.py`

- [ ] **Step 1: Write failing normalization and feature tests**

Add table-driven tests for IP, URL, SHA-1, SHA-256, GUID, SID, UTC timestamps with equivalent offsets, process names, filenames, FQDN/short hostnames, SQL whitespace, and trailing semicolons. The expected normalized markers are `<ip>`, `<url>`, `<sha1>`, `<sha256>`, `<guid>`, `<sid>`, `<timestamp>`, `<process>`, `<file>`, and `<host>`.

Add explicit trajectory tests for:

```text
- one successful SQL query;
- one SQL execution failure with Evidence.kind == "sql_error";
- one empty result;
- two SQL queries equivalent after whitespace/semicolon normalization;
- submission before max_steps;
- submission exactly at max_steps;
- max_steps without submission;
- exact, normalized, component, not_found, and indeterminate gold evidence;
- excerpt truncation at 240 characters;
- absent token data produces null fields rather than zero.
```

- [ ] **Step 2: Run and confirm the expected red state**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_features -v
```

Expected: import failure for `features`.

- [ ] **Step 3: Implement feature extraction without semantic guessing**

Implement these signatures:

```python
def normalize_sql(value: str) -> str
def normalize_entities(value: str) -> str
def normalized_equivalent(left: str, right: str) -> bool
def extract_features(mapped: MappedQuestion, max_steps: int) -> FeatureRecord
```

Use regexes with word boundaries and case-insensitive matching where appropriate. Convert parseable ISO-8601 timestamps to UTC before replacing them with `<timestamp>`. Normalize an FQDN and its first label consistently only for comparison, without altering stored source strings.

For each Env trajectory step:

- Treat each step where `info.submit is False` as one SQL action; its `action` field is the raw SQL string saved by `ExcytinEnv.step()`.
- Exclude the submission step from all SQL totals even though its `info.query_success` may be true.
- Read `info.query_success` only as a boolean; a missing value is neither success nor failure and creates a log-integrity review reason.
- Count empty results only for successful SQL observations whose parsed row payload is empty.
- Count duplicate queries by the second and later occurrence of identical normalized SQL.
- Find submission data only where `info.submit is True`.
- Capture the submitted answer from `info.submitted_answer`.
- Add evidence paths using one-based step numbers and zero-based trajectory indexes.
- Clip evidence excerpts to 240 characters and set `excerpt_truncated` correctly.

Resolve the official reward from Agent and Env records. If both contain a reward and they differ numerically, raise `MappingError`; if neither contains one, raise `MappingError`. Read token fields only when directly present; do not infer evaluator tokens from total tokens.

Gold evidence matching is deterministic: `exact` for literal occurrence of the golden answer in observations; `normalized` for equality after entity/whitespace normalization; `component` only when every non-empty structured golden component appears in one or more observations; `not_found` when comparable evidence exists but does not match; `indeterminate` when the golden answer or observations cannot be compared.

- [ ] **Step 4: Verify targeted and cumulative tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_features -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
```

Expected: all feature and cumulative tests pass.

- [ ] **Step 5: Review checkpoint and commit only after approval**

Proposed commit message:

```text
Extract deterministic failure features
```

## Task 4: Encode conservative taxonomy_v1 candidate attribution

**Files:**

- Create: `experiments/failure_analysis/attribution.py`
- Create: `tests/failure_analysis/test_attribution.py`

- [ ] **Step 1: Write failing precedence and calibration tests**

Create one test for every approved calibration fingerprint. Assert the three reward-one controls return no primary cause even when their fixture contains an SQL error, empty result, duplicate query, or step-limit submission. Assert indexes 10, 34, 23, and 80 produce confirmed calibration candidate `NAVIGATION`. Assert indexes 55, 79, and 65 produce candidate `GOLD`, status `candidate`, and mandatory human review.

Add independent synthetic tests asserting:

```text
- materially equivalent submitted/golden text with reward below one -> EVALUATOR + review;
- unrecovered SQL execution failure -> SQL_EXEC;
- golden evidence found but omitted from answer -> ANSWER;
- repeated equivalent queries add LOOP as secondary;
- max-step incomplete work adds STEP_LIMIT as secondary;
- empty result alone never produces DATA;
- insufficient competing evidence -> UNKNOWN + review;
- GOLD, EVALUATOR, and UNKNOWN always require review;
- LOOP and STEP_LIMIT are not primary when another supported primary exists.
```

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_attribution -v
```

Expected: import failure for `attribution`.

- [ ] **Step 3: Implement one ordered attribution function**

Implement:

```python
def load_taxonomy(path: Path) -> dict[str, Any]
def attribute_record(features: FeatureRecord, taxonomy: dict[str, Any]) -> Attribution
```

Apply this exact order:

```text
1. reward == 1 -> correct control, no failure cause;
2. matching calibration incident + question index + fingerprint -> frozen calibration candidate/status;
3. normalized submitted and golden answers equivalent while reward < 1 -> EVALUATOR;
4. unrecovered SQL execution failures that block usable evidence -> SQL_EXEC;
5. gold evidence exact/normalized/component but answer omits or corrupts it -> ANSWER;
6. successful but misdirected retrieval with direct evidence -> SQL_RETRIEVAL;
7. wrong starting alert/entity/path with direct evidence -> NAVIGATION;
8. obtained evidence combined incorrectly -> REASONING;
9. otherwise -> UNKNOWN.
```

Add `LOOP` when duplicate queries show no intervening progress. Add `STEP_LIMIT` only when the trajectory reaches `max_steps` and remains incomplete; merely submitting at step 15 is not causal. Add `DATA` only from direct missing-data/schema evidence. Set `confidence` to `high` for confirmed calibration/control, `medium` for a single direct deterministic rule, and `low` for ambiguous/default cases. Every candidate is unconfirmed unless the calibration entry says confirmed. Include concise rule names in `human_review_reasons`.

- [ ] **Step 4: Verify targeted and cumulative tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_attribution -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
```

Expected: all attribution and cumulative tests pass.

- [ ] **Step 5: Review checkpoint and commit only after approval**

Proposed commit message:

```text
Add taxonomy v1 attribution rules
```

## Task 5: Generate atomic reports and safely import human review

**Files:**

- Create: `experiments/failure_analysis/reporting.py`
- Create: `tests/failure_analysis/test_reporting.py`

- [ ] **Step 1: Write failing report-integrity tests**

Using `tempfile.TemporaryDirectory()`, test that one call writes exactly:

```text
taxonomy_v1.json
incident_5_attribution.jsonl
incident_5_attribution.csv
incident_5_summary.md
human_review.csv
incident_5_analysis_manifest.json
```

Assert JSONL and CSV have identical record counts and reward totals; Markdown totals match; manifest source/output SHA-256 values recompute; existing output directory or target file raises `OutputCollisionError`; a simulated serialization failure leaves no final-named files; review identity mismatch raises `ReviewError`; valid review updates only reviewed fields and never candidate fields or official reward. Add a deterministic review-selection test with at least 20 rows in each of two nonmandatory primary categories: two separately ordered copies must select the same identities using seed `20260720`, selecting `ceil(10%)` with a minimum of one per nonempty category.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_reporting -v
```

Expected: import failure for `reporting`.

- [ ] **Step 3: Implement canonical rows, review validation, and atomic output**

Implement these signatures:

```python
def build_row(features: FeatureRecord, attribution: Attribution, taxonomy_version: str) -> dict[str, Any]
def select_review_rows(rows: list[dict[str, Any]], taxonomy: dict[str, Any]) -> list[dict[str, Any]]
def apply_human_review(rows: list[dict[str, Any]], review_path: Path, taxonomy: dict[str, Any]) -> None
def write_outputs(
    rows: list[dict[str, Any]],
    taxonomy_path: Path,
    incident: str,
    output_dir: Path,
    source_paths: dict[str, Path],
    max_steps: int,
    git_commit: str | None,
    review_applied: bool,
) -> list[Path]
```

`build_row` must emit every field from design section 7. Serialize list/dictionary fields as JSON strings in CSV and as native structures in JSONL. `human_review.csv` must contain these nine fields: incident, question_index, question_fingerprint_sha256, candidate_primary, candidate_secondary, reviewed_primary, reviewed_secondary, review_status, and review_notes. `select_review_rows` must include all mandatory categories and low-confidence/integrity anomalies, then group the remaining rows by primary category, sort each group by stable identity, shuffle a copy with `random.Random(20260720)`, and take `max(1, ceil(group_size * 0.1))`. Return the union sorted by stable identity. `apply_human_review` must match exactly on incident, integer question index, and 64-character fingerprint; reject duplicates, unknown identities, category names outside taxonomy_v1, and malformed secondary JSON.

`write_outputs` must refuse an existing `output_dir`, create a sibling temporary directory, write UTF-8 files with stable sorted JSON keys, reopen and validate JSONL/CSV counts, compute hashes after content is final, write the manifest last, then rename the temporary directory atomically. On any exception, remove only the newly created sibling temporary directory. It must never unlink or replace a user path.

The Markdown summary must include record count, official reward distribution, candidate primary counts, review-status counts, mapping count, SQL success/failure totals, and a warning that candidates do not alter official scoring.

The manifest shape is fixed as `schema_version`, `taxonomy_version`, `incident`, `max_steps`, `generated_at_utc`, `tool_version`, `git_commit`, `review_applied`, `record_count`, `mapping_counts`, `sources`, and `outputs`. Each `sources` value contains the supplied path and SHA-256. Each `outputs` value contains the filename and SHA-256 for taxonomy, JSONL, CSV, Markdown, and review CSV; the manifest does not contain its own hash because a self-hash is not computable. Use an ISO-8601 UTC generation time ending in `Z`.

- [ ] **Step 4: Verify targeted and cumulative tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_reporting -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
```

Expected: report and cumulative tests pass with no files written outside temporary test directories.

- [ ] **Step 5: Review checkpoint and commit only after approval**

Proposed commit message:

```text
Add auditable failure attribution reports
```

## Task 6: Add the offline CLI and exit-code contract

**Files:**

- Create: `experiments/failure_analysis/analyze_failures.py`
- Create: `tests/failure_analysis/test_cli.py`

- [ ] **Step 1: Write failing CLI tests**

Call `main(argv)` directly with temporary fixtures. Cover successful execution and codes 2, 3, 4, and 5. Patch network/socket and database connection entry points to raise if called. Mock `subprocess.run` and assert its only permitted invocation is read-only `git rev-parse HEAD`. Assert `--help` lists all required arguments, default taxonomy path, default `--max-steps 15`, and no overwrite flag.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_cli -v
```

Expected: import failure for `analyze_failures`.

- [ ] **Step 3: Implement argument parsing and orchestration**

Implement:

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace
def run(args: argparse.Namespace) -> list[Path]
def main(argv: list[str] | None = None) -> int
```

Required arguments are `--agent-json`, `--env-json`, `--question-json`, `--incident`, and `--output-dir`. Optional arguments are `--taxonomy`, `--review-csv`, and `--max-steps`. Resolve the default taxonomy relative to `analyze_failures.py`, not the current directory.

`run()` must perform, in order: path validation; JSON loading; strict mapping; feature extraction; attribution; optional review import; report writing. It must not create the output directory until every record has mapped and attributed successfully. Obtain Git commit with a read-only `git rev-parse HEAD`; if unavailable, record JSON null without failing analysis.

`main()` catches only `AnalysisError`, prints one concise error to stderr, and returns its declared exit code. Unexpected exceptions must propagate during tests and produce a traceback when invoked as a script. End the file with `raise SystemExit(main())`.

- [ ] **Step 4: Verify help, targeted tests, and the full suite**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python experiments/failure_analysis/analyze_failures.py --help
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_cli -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
```

Expected: help is concise; all tests pass; no Docker/MySQL/network/API activity occurs.

- [ ] **Step 5: Review checkpoint and commit only after approval**

Proposed commit message:

```text
Add offline failure attribution CLI
```

## Task 7: Validate against the repaired Incident 5 artifact without rerunning it

**Files:**

- Read only: repaired Agent JSON, repaired Env JSON, Incident 5 Question JSON
- Create only: a new analysis directory outside `experiments/final_results/`
- Update tests only if a real schema mismatch reveals a missing adapter; never change the taxonomy to force expected totals.

- [ ] **Step 1: Record source hashes and confirm inputs remain unchanged**

Run:

```bash
cd /home/acuraintegurl/Desktop/SecRL-git
base='experiments/final_results/BaselineAgent_deepseek-v4-flash_c732_alert_level_t0_s15_trial1_repaired_with_c733_q10'
agent="$base/agent_incident_5.json"
env="$base/env_incident_5.json"
question='secgym/questions/o1/test/incident_5_qa_incident_o1-ga_c42.json'
out='experiments/failure_analysis_outputs/incident_5_taxonomy_v1_d3f2322'
sha256sum "$agent" "$env" "$question"
test ! -e "$out"
if [ "${DEEPSEEK_API_KEY+x}" = x ]; then echo 'DEEPSEEK_API_KEY=SET'; else echo 'DEEPSEEK_API_KEY=NOT_SET'; fi
```

Expected hashes:

```text
e1daffc704bae50bd071262418279b34cd1c54d08af84643ef5b78135599100f  agent_incident_5.json
8f89d9efc82a25893b8805b77f0521678280716e0b651a5ab786290294e2d435  env_incident_5.json
8dd53e9a5e789e8fecb74c9965d66daa4f5fd5fbd2c9ae8e46714b79eb838a1a  incident_5_qa_incident_o1-ga_c42.json
```

Require exact equality for all three hashes. Do not proceed on any mismatch.

- [ ] **Step 2: Run the CLI once into a new timestamped analysis path**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python experiments/failure_analysis/analyze_failures.py \
  --agent-json "$agent" \
  --env-json "$env" \
  --question-json "$question" \
  --incident incident_5 \
  --max-steps 15 \
  --output-dir "$out"
```

Expected: exit 0 and exactly the six incident-level outputs. Do not use `experiments/final_results/` and do not pass any overwrite option.

- [ ] **Step 3: Verify the real-data acceptance criteria**

Assert all of the following with a read-only verification script:

```text
98 canonical Question records
98 uniquely mapped Agent entries
98 uniquely mapped Env entries
no duplicate stable identity tuples
46 reward 1, 46 reward 0, 6 reward 0.4
1198 total trajectory steps
1071 SQL successes and 29 SQL failures
all 98 submitted
the ten calibration fingerprints are present
indexes 13, 5, 26 have no failure cause
indexes 10, 34, 23, 80 have NAVIGATION calibration candidate
indexes 55, 79, 65 have GOLD candidate and mandatory review
candidate/review fields are separate
all manifest hashes recompute
source SHA-256 values are unchanged before and after analysis
```

Run this verifier from the repository root after setting `out`, `agent`, `env`, and `question` as in Steps 1-2:

```bash
OUT_DIR="$out" AGENT_JSON="$agent" ENV_JSON="$env" QUESTION_JSON="$question" \
PYTHONDONTWRITEBYTECODE=1 python - <<'PY'
from collections import Counter
from hashlib import sha256
import json
import os
from pathlib import Path

out = Path(os.environ["OUT_DIR"])
rows = [json.loads(line) for line in (out / "incident_5_attribution.jsonl").read_text(encoding="utf-8").splitlines()]
manifest = json.loads((out / "incident_5_analysis_manifest.json").read_text(encoding="utf-8"))

assert len(rows) == 98
identities = {(r["incident"], r["question_index"], r["question_fingerprint_sha256"]) for r in rows}
assert len(identities) == 98
assert Counter(r["reward_official"] for r in rows) == Counter({1: 46, 0: 46, 0.4: 6})
assert sum(r["steps"] for r in rows) == 1198
assert sum(r["sql_success"] for r in rows) == 1071
assert sum(r["sql_failure"] for r in rows) == 29
assert all(r["submitted"] is True for r in rows)

by_index = {r["question_index"]: r for r in rows}
for index in (13, 5, 26):
    assert by_index[index]["primary_cause_candidate"] is None
for index in (10, 34, 23, 80):
    assert by_index[index]["primary_cause_candidate"] == "NAVIGATION"
for index in (55, 79, 65):
    assert by_index[index]["primary_cause_candidate"] == "GOLD"
    assert by_index[index]["needs_human_review"] is True

assert manifest["record_count"] == 98
assert manifest["mapping_counts"] == {"agent": 98, "env": 98, "question": 98}
for filename, metadata in manifest["outputs"].items():
    payload = (out / filename).read_bytes()
    assert sha256(payload).hexdigest() == metadata["sha256"]
for name, path in {
    "agent": Path(os.environ["AGENT_JSON"]),
    "env": Path(os.environ["ENV_JSON"]),
    "question": Path(os.environ["QUESTION_JSON"]),
}.items():
    assert sha256(path.read_bytes()).hexdigest() == manifest["sources"][name]["sha256"]

print("INCIDENT_5_ACCEPTANCE_OK records=98 steps=1198 sql_success=1071 sql_failure=29")
PY
```

Expected: exactly one short `INCIDENT_5_ACCEPTANCE_OK` line.

If any count differs, stop. Inspect source schema and mapping evidence, add the smallest failing regression test, then change only the responsible adapter. Do not rerun Incident 5 and do not relabel records merely to satisfy the acceptance list.

- [ ] **Step 4: Perform final static safety checks**

Run:

```bash
if rg -n '(^|[[:space:]])(import|from)[[:space:]]+(docker|mysql|requests|autogen)|run_exp|LLMEvaluator|BaselineAgent' experiments/failure_analysis; then
  echo 'FORBIDDEN_IMPORT_SCAN_FAILED'
  exit 1
else
  echo 'FORBIDDEN_IMPORT_SCAN_OK'
fi
git diff --check
python -m json.tool experiments/failure_analysis/taxonomy_v1.json >/dev/null
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
cache_dir="$(mktemp -d)"
PYTHONPYCACHEPREFIX="$cache_dir" python -m compileall -q experiments/failure_analysis tests/failure_analysis
find "$cache_dir" -type f -delete
find "$cache_dir" -depth -type d -empty -delete
```

Expected: forbidden-import scan OK, no diff-check output, JSON exits 0, all tests pass, compilation exits 0, and only the new temporary cache directory is removed.

- [ ] **Step 5: Review the generated human-review queue manually**

Confirm every GOLD, EVALUATOR, UNKNOWN, mapping/log anomaly, and low-confidence row is queued. Confirm each other nonempty primary category contributes `max(1, ceil(category_count * 0.1))` rows using seed `20260720`. Do not import a review CSV until a human has completed it.

- [ ] **Step 6: Show evidence and request approval for the final implementation commit**

Show:

```text
git status --short --branch
git diff --check
git diff --stat
git diff
full unittest result
Incident 5 acceptance summary
files proposed for staging
```

Only after explicit approval, stage the implementation/tests/taxonomy files. Do not stage generated analysis outputs unless the user separately approves preserving them. Proposed final verification commit message, if a final integration-only change exists:

```text
Validate Incident 5 failure attribution
```

## Task 8: Add deterministic eight-incident aggregation

**Files:**

- Create: `experiments/failure_analysis/aggregate_failures.py`
- Create: `tests/failure_analysis/test_aggregate.py`
- Reuse without modifying: incident-level canonical JSONL files

- [ ] **Step 1: Write failing aggregation tests**

Create eight small JSONL fixtures named for incidents 5, 38, 34, 39, 55, 134, 166, and 322. Assert aggregation rejects a missing incident, a duplicate incident, mixed schema/taxonomy versions, a duplicate stable identity, malformed JSONL, and an existing output directory. Assert a valid aggregation writes exactly `all_incidents_attribution.csv` and `all_incidents_summary.md`, sorted numerically by incident then question index. Assert the CSV total and per-incident totals equal the summary.

- [ ] **Step 2: Run and confirm failure**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_aggregate -v
```

Expected: import failure for `aggregate_failures`.

- [ ] **Step 3: Implement the aggregation-only CLI**

Implement these signatures:

```python
EXPECTED_INCIDENTS = ("incident_5", "incident_38", "incident_34", "incident_39", "incident_55", "incident_134", "incident_166", "incident_322")

def load_incident_rows(paths: list[Path]) -> list[dict[str, Any]]
def aggregate(paths: list[Path], output_dir: Path) -> list[Path]
def parse_args(argv: list[str] | None = None) -> argparse.Namespace
def main(argv: list[str] | None = None) -> int
```

The CLI accepts exactly eight repeated `--input-jsonl` arguments and one `--output-dir`. It reads only incident-level JSONL, requires schema `failure_attribution_v1` and taxonomy `taxonomy_v1`, validates the stable identity tuple, and never reads or modifies experiment logs. Sort by the numeric suffix of `incident`, then `question_index`. Serialize list/dictionary CSV cells as stable JSON. The summary must report overall/per-incident record counts, official reward distributions, primary candidate counts, reviewed primary counts, and review completion counts.

Write both files into a sibling temporary directory, validate their totals, then atomically rename that directory. Refuse any existing output path and remove only the temporary directory on failure. Return exit code 2 for invalid inputs and 4 for collision.

- [ ] **Step 4: Verify targeted and cumulative tests**

Run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest tests.failure_analysis.test_aggregate -v
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
```

Expected: all aggregation and cumulative tests pass.

- [ ] **Step 5: Review checkpoint and commit only after approval**

Proposed commit message:

```text
Add eight-incident attribution aggregation
```

Do not execute real eight-incident aggregation until the seven remaining experiment outputs have separately passed their input, token-budget, and integrity checkpoints.

## Final verification gate

Before declaring implementation complete, run:

```bash
PYTHONDONTWRITEBYTECODE=1 python -m unittest discover -s tests/failure_analysis -v
git diff --check
git status --short --branch
git log --oneline --decorate -8
```

Completion requires all tests green, no unreviewed implementation diff, clean commit boundaries, reproducible Incident 5 output hashes, unchanged experiment input hashes, and explicit user confirmation. Pushing remains a separate decision.
