# SecRL Failure Attribution Design

**Date:** 2026-07-20
**Status:** User-approved design
**Taxonomy:** taxonomy_v1
**Experiment:** ExCyTIn-Bench DeepSeek eight-incident reproduction

## 1. Purpose

Build a deterministic, offline failure-attribution tool for SecRL experiment logs.

The tool will first analyze the repaired Incident 5 result and later apply the same taxonomy and data model to all eight incidents. It must preserve official rewards and original experiment files.

## 2. Goals

The tool must:

- Accept explicit Agent, Env, Question JSON, incident, and output-directory paths.
- Treat all input files as read-only.
- Validate JSON structure and question mapping before attribution.
- Identify questions using question index and normalized SHA-256 fingerprints.
- Extract deterministic SQL, trajectory, submission, evaluation, and token features.
- Generate conservative taxonomy_v1 candidate attributions.
- Separate automatic candidates from human-confirmed labels.
- Produce JSONL, CSV, Markdown, human-review, and manifest outputs.
- Run without an LLM, database, Docker, API key, or network access.
- Use Python standard-library unittest tests and no new dependencies.

## 3. Non-goals

The tool will not:

- Change official rewards.
- Rewrite Agent, Env, or Question JSON.
- Resume or run experiments.
- Start Docker or connect to MySQL.
- Call an Agent, Evaluator, or other LLM.
- Treat nodes as a unique question identifier.
- Automatically confirm GOLD or EVALUATOR failures.
- Modify prompts, agents, evaluators, or experiment conditions.
- Extend the existing mutable result-analysis implementation.

## 4. Repository Placement

```text
experiments/failure_analysis/
├── analyze_failures.py
├── models.py
├── identity.py
├── features.py
├── attribution.py
├── reporting.py
└── taxonomy_v1.json

tests/failure_analysis/
├── test_identity.py
├── test_features.py
├── test_attribution.py
└── test_reporting.py
```

Responsibilities:

- `analyze_failures.py`: argument parsing, orchestration, and exit codes.
- `models.py`: dataclasses and schema enums.
- `identity.py`: parsing, canonicalization, fingerprinting, and mapping.
- `features.py`: deterministic trajectory and token features.
- `attribution.py`: taxonomy_v1 candidate rules and review policy.
- `reporting.py`: JSONL, CSV, Markdown, review CSV, and manifest generation.
- `taxonomy_v1.json`: frozen category definitions, policies, and calibration cases.

## 5. Command-Line Interface

Primary usage:

```text
python experiments/failure_analysis/analyze_failures.py
  --agent-json <agent.json>
  --env-json <env.json>
  --question-json <questions.json>
  --incident incident_5
  --output-dir <new_output_directory>
```

Optional inputs:

```text
--taxonomy <taxonomy_v1.json>
--review-csv <completed_human_review.csv>
--max-steps 15
```

The CLI must not import `run_exp.py`, Agent classes, Evaluator classes, Docker, or MySQL.

## 6. Identity and Mapping

Mapping priority:

1. Zero-based position in the canonical Question JSON.
2. SHA-256 of the normalized complete question dictionary.
3. SHA-256 of normalized question text.
4. Nodes as an auxiliary diagnostic field only.

Canonical question serialization uses:

- UTF-8;
- sorted dictionary keys;
- compact JSON separators;
- preserved Unicode;
- no mutation of the source object.

Stable review identity is the tuple:

```text
incident + question_index + question_fingerprint_sha256
```

Agent and Env entries are mapped independently to the Question JSON. Order may be reported, but order alone is insufficient.

Duplicate nodes with different question fingerprints remain separate questions.

Ambiguous fingerprints, missing questions, extra logs, or conflicting mappings stop behavioral attribution.

## 7. Attribution Record

Each question produces one structured record containing:

```text
schema_version
taxonomy_version
incident
question_index
question_fingerprint_sha256
question_text_fingerprint_sha256
nodes
control_status

reward_official
reward_bucket
golden_answer
submitted_answer

agent_source_index
env_source_index
mapping_status
log_complete

sql_total
sql_success
sql_failure
empty_result_count
duplicate_query_count
steps
max_steps
submitted
submitted_at_step_limit

gold_evidence_match
gold_evidence_steps
evaluator_fields_complete

agent_prompt_tokens
agent_completion_tokens
agent_total_tokens
evaluator_tokens

primary_cause_candidate
primary_cause_status
secondary_cause_candidates
confidence
evidence

needs_human_review
human_review_reasons
reviewed_primary
reviewed_secondary
review_status
review_notes
```

`reward_official` is immutable.

`gold_evidence_match` is one of:

```text
exact
normalized
component
not_found
indeterminate
```

It describes deterministic matching only and does not claim full semantic equivalence.

## 8. Evidence Model

Evidence entries contain traceable source locations rather than copied full logs:

```json
{
  "kind": "sql_error",
  "step": 9,
  "source": "env",
  "field": "trajectory[8].observation",
  "excerpt": "ProgrammingError: ...",
  "excerpt_truncated": false
}
```

Long excerpts are truncated and marked. The source field path and step number remain available for exact manual inspection.

## 9. Normalization

The feature layer separately normalizes:

- SQL whitespace and trailing semicolons;
- IP addresses;
- URLs;
- SHA-1 and SHA-256 hashes;
- GUIDs;
- SIDs;
- UTC timestamps and equivalent offsets;
- process names;
- file names;
- FQDN and short host names.

Raw source values are never replaced.

## 10. taxonomy_v1

Categories:

- `DATA`: missing or incomplete data, empty required tables, or unavailable layer evidence.
- `SQL_EXEC`: SQL syntax, table, column, or database execution failure.
- `SQL_RETRIEVAL`: executable SQL using the wrong table, join, filter, identifier, or time condition.
- `NAVIGATION`: incorrect incident/alert starting point, entity selection, or investigation path.
- `LOOP`: repeated equivalent queries without progress from observations.
- `STEP_LIMIT`: incomplete investigation or answer at the configured maximum step count.
- `REASONING`: evidence obtained but timeline, entity relation, or multi-step reasoning is incorrect.
- `ANSWER`: correct evidence obtained but final answer is incomplete, overbroad, malformed, or contains a wrong entity.
- `EVALUATOR`: submitted and golden answers are materially equivalent but evaluation appears incorrect.
- `GOLD`: question, context, golden answer, solution, or database evidence appears inconsistent.
- `INFRA`: API, parser, submission, logging, mapping, duplication, or execution infrastructure failure.
- `UNKNOWN`: available evidence is insufficient for a reliable cause.

Policy:

- LOOP and STEP_LIMIT normally remain secondary causes.
- DATA requires direct data or schema evidence; an empty query alone is insufficient.
- GOLD, EVALUATOR, and UNKNOWN always require human review.
- Automatic rules produce candidates, not confirmed causes.
- Reward-one controls have no failure cause even if they contain SQL errors, empty results, or a step-15 submission.

## 11. Confirmed Calibration Baseline

The following Incident 5 records were manually reviewed and approved:

| Index | Question fingerprint | Expected primary | Review |
|---:|---|---|---|
| 13 | `f013d6956c8913bde432da326917825d89c1e171178860d9234935afa1fa3641` | none, correct control | confirmed |
| 5 | `428dd97f0387344e9772c82c1c250b31349d963acc62383525f2ef4249a228ff` | none, correct control | confirmed |
| 26 | `ce13a820fdc04738aa3cea8b34c57cb7c512eeddbde39b7e3baa8debad1410cd` | none, correct control | confirmed |
| 10 | `c625cb32197d9e5b136c67bef9324a1eecd18f52661121580cb0177ca7addc2b` | NAVIGATION | confirmed |
| 34 | `22c76729415a21377e7ecdd930ad9f19d9adf82f95d83d26075698a6e2d2bda1` | NAVIGATION | confirmed |
| 55 | `a7e0ef2b978187a4e279d09987e5a05ff924d4edfed723fab5133f763e6e41a5` | GOLD suspected | human review required |
| 79 | `15c2e6b291ad2d8f6b5e99b9576667de284a1743bb607536f763f3abe43258f8` | GOLD suspected | human review required |
| 23 | `6ed33dc5f7f4691e9eb45a4729ebecf11c65345e701296b6100ddf886d3b12ea` | NAVIGATION | confirmed |
| 65 | `816da8a6b0bfa76ccb693433c4a1965d2d5736b9ed20c3087265eee730ebaa4a` | GOLD suspected | human review required |
| 80 | `9c18a3c122464e3c0b58c5b3109082161c93c9b3cd5e3e723947c35e2561cd3f` | NAVIGATION | confirmed |

Suspected GOLD cases remain suspected until their graph path and database evidence are manually reviewed. Their official rewards remain unchanged.

## 12. Attribution Precedence

Rules execute in this order:

1. Validate input and mapping integrity.
2. Apply fingerprint-matched confirmed calibration records.
3. Detect deterministic Evaluator or Golden inconsistencies.
4. Determine whether Golden evidence appeared in observations.
5. Evaluate SQL execution, retrieval, and navigation behavior.
6. Add causal LOOP or STEP_LIMIT secondary candidates.
7. Use UNKNOWN when evidence cannot distinguish competing causes.

Observed features are not automatically causes.

For example, a SQL error is recorded for every affected trajectory, but SQL_EXEC becomes a cause only when the error materially prevents the investigation or is not recovered.

## 13. Human Review

Initial analysis generates a review CSV containing:

```text
incident
question_index
question_fingerprint_sha256
candidate_primary
candidate_secondary
reviewed_primary
reviewed_secondary
review_status
review_notes
```

Mandatory review includes:

- every GOLD candidate;
- every EVALUATOR candidate;
- every UNKNOWN candidate;
- mapping or log-integrity anomalies;
- every low-confidence attribution.

Other categories are reviewed using a fixed-seed stratified sample.

Review import validates the stable identity tuple. A mismatched incident, index, or fingerprint rejects the review file.

Candidate and reviewed fields remain separate in all final outputs.

## 14. Outputs

Incident-level outputs:

```text
taxonomy_v1.json
incident_5_attribution.jsonl
incident_5_attribution.csv
incident_5_summary.md
human_review.csv
incident_5_analysis_manifest.json
```

All-incident outputs:

```text
all_incidents_attribution.csv
all_incidents_summary.md
```

The manifest records:

- source paths and SHA-256;
- taxonomy SHA-256;
- incident and max_steps;
- record and mapping counts;
- generation time;
- tool version and Git commit;
- whether human review was applied;
- output SHA-256 values.

JSONL is the canonical output. CSV and Markdown are generated from the same validated in-memory records.

## 15. Output Safety

- Existing target files cause an error; no automatic overwrite.
- Formal outputs are written to temporary files first.
- Files are atomically renamed only after every format validates.
- Failure must not leave partial files with final names.
- Input files are never opened for writing.
- No output may claim to be an experiment result or repaired experiment log.
- No output changes an official reward.

## 16. Exit Codes

- `0`: analysis completed with complete mapping.
- `2`: invalid arguments, missing input, or invalid JSON.
- `3`: mapping or log-integrity failure; behavioral attribution not performed.
- `4`: output collision; overwrite refused.
- `5`: invalid or mismatched human-review input.

Errors identify the relevant path, source index, question index, fingerprint, and field when available.

## 17. Test Strategy

Tests use standard-library `unittest` and `tempfile.TemporaryDirectory()`.

Required fixture behaviors:

1. SQL execution error.
2. Empty SQL result.
3. Golden evidence appears but the submitted answer omits it.
4. Step-limit submission versus unfinished step-limit trajectory.
5. Agent/Env/Question count mismatch.
6. Duplicate nodes with distinct question fingerprints.
7. Missing question mapping.
8. Reward-one control with error, empty result, or step-15 submission.

Additional coverage:

- Agent and Env reward conflict.
- IP, URL, hash, GUID, SID, timestamp, process, file, and hostname normalization.
- Equivalent timestamp and FQDN answer representations.
- Mandatory review for GOLD, EVALUATOR, and UNKNOWN.
- Review CSV fingerprint mismatch.
- Output collision refusal.
- Recomputable manifest hashes.
- Consistent JSONL, CSV, and Markdown totals.

Implementation must follow test-driven development:

1. Write one failing test.
2. Run it and verify the expected failure.
3. Write the minimum production code.
4. Run the targeted test.
5. Run the full failure-analysis test suite.
6. Refactor only while all tests remain green.

## 18. Reproduction Constraints

- Incident 5 is not rerun.
- Its repaired result remains a mixed c732+c733(q10) artifact.
- Original c732 and c733 files remain unchanged.
- Agent Token accounting excludes complete Evaluator usage.
- Evaluator Token remains unavailable unless directly present in logs.
- No Prompt, Agent, or Evaluator changes occur during the remaining seven events.
- Failure attribution never changes experiment comparability or scoring.

## 19. Git Workflow

Before each modification:

```text
git status --short --branch
```

Before any commit:

- show `git diff`;
- list files to be committed;
- run relevant tests;
- wait for explicit user confirmation.

No push occurs without explicit user confirmation.
