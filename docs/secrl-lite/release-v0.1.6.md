# SecRL Lite v0.1.6

SecRL Lite v0.1.6 turns post-run diagnosis from a manual chore into an
automatic one: completed SecRL runs now trigger failure analysis by
themselves, every attribution carries a human-readable explanation, and
the Runs list finally shows a successful run in green instead of the
same alarming red as a failure. This release contains one Alembic
migration and no benchmark data changes.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Automatic failure analysis on run completion (PR #19)

- When a SecRL run reaches SUCCEEDED, the runner dispatch triggers the
  failure analyzer automatically: the versioned attribution set is
  registered before the operator opens the Analysis tab.
- The trigger is best-effort and idempotent: an existing registered
  analysis is respected (no duplicate revisions from re-dispatch), and
  any analyzer problem is logged without ever affecting the run result.
  Explicit `:analyze` calls keep their append-only revision semantics
  for deliberate re-analysis.
- Smoke and non-SecRL benchmarks are unaffected. The behavior can be
  disabled with `SECRL_AUTO_FAILURE_ANALYSIS=false`.

### Explanations on every attribution (PR #19)

- Attribution records now carry an `explanation` field composed from the
  recorded run features. UNKNOWN attributions no longer arrive
  unexplained: the reason spells out step-limit exhaustion without a
  submission, placeholder answers after failed exploration, empty-result
  query counts, SQL error rates, and duplicate-query looping signals.
- The field flows through the analyzer output, the registration mapping,
  a new Alembic column (0005_attribution_explanation), the attributions
  API payload, and the Run detail Analysis tab.

### Task status colors on the Runs list (PR #19)

- The status badge previously rendered every task status in the same red
  tone. SUCCEEDED now renders green, RUNNING and QUEUED render amber,
  and terminal-failure statuses (FAILED, BUDGET_EXHAUSTED, CANCELED,
  INTERRUPTED, PAUSED) keep the red tone. Infrastructure tones
  (healthy / degraded / offline) are unchanged.

## Backward compatibility

- The automatic trigger only adds analysis registrations for completed
  SecRL runs; failed tasks still require manual investigation, and
  non-SecRL benchmarks are unaffected.
- The Alembic migration adds one nullable-safe column
  (`attribution.explanation`, TEXT NOT NULL DEFAULT '').
  v0.1.x attributions load unchanged with an empty explanation.
- The OpenAPI surface is unchanged by this release; the explanation
  rides the existing attributions payload.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.6
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

Alembic migration 0005 runs automatically at API startup. Start only the
Incident profiles required by the selected tasks and run preflight
before queueing.

## Rollback

Stop the v0.1.6 stack, select the previous source revision, and restore
its matching verified backup into an empty target. Do not roll back by
mutating the live volume in place. The 0005 column is additive; the
previous revision runs fine against a database that already carries it,
but the sanctioned rollback path remains restore-into-empty-target.

## Security boundaries

- The automatic analyzer runs with the same hash-verified, restricted
  materialization as the manual flow; no new data paths are exposed.
- Attribution explanations are derived from existing sanitized run
  metrics only: no prompts, submitted answers, gold references,
  credentials, or raw provider responses enter the field.
- Status-color changes are presentation-only.

## Known limitations

- The explanation composes from deterministic run features; it does not
  (yet) include model-generated reasoning about the failure.
- Automatic analysis covers SecRL SUCCEEDED runs; failed runs still need
  manual investigation.

## Verification summary

- Tests were written before implementation: automatic trigger and
  idempotence on SecRL success, suppression for smoke runs, explanation
  composition across attribution branches (never-submitted, placeholder,
  ANSWER), the Alembic head migration, the updated frozen-report
  contract, and the badge tone matrix. Backend suites: 467 + 161
  passed. Frontend suite: 37 passed with a production build.
  `compileall`, `git diff --check`, and secret scans were clean.
- PR CI run 33235450533: linux/amd64 Compose and platform SUCCESS,
  linux/amd64 + linux/arm64 image build SUCCESS. Post-merge CI: event=push,
  head_sha equal to merge commit
  `680b93d3c875dfe504a4d48056e548098ea9b6ef`, both jobs SUCCESS.
- All verification used mocked providers. No real LLM calls were made and
  no provider spend occurred at any point during development, review, or CI.

## Security and distribution

- Secrets are displayed only as configured or missing; plaintext values are
  never returned by the API or rendered in the UI.
- Agent Services return structured Actions only. Benchmark tools are executed
  by the platform.
- No API keys, databases, caches, experiment results, trajectories, raw
  provider responses, or no-truncation output belong in this release notes
  change.
- CI does not publish public Docker images. Build images locally from the
  reviewed source revision.
