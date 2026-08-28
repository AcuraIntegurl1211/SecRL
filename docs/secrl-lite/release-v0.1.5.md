# SecRL Lite v0.1.5

SecRL Lite v0.1.5 adds live run observability: operators can now watch
token burn, per-case scoring, and budget consumption while a multi-case
evaluation is still running, instead of waiting for the run to finish.
Both surfaces are read-only aggregations over data the runner already
commits, so no runner, billing, or evaluation semantics change. This
release contains no benchmark data changes and introduces no database
migration.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Live run progress (PR #17)

- New endpoint `GET /api/v1/runs/{id}/progress`: completed, failed, and
  correct case counts, reward sum and average reward over scored
  attempts, agent and evaluator token totals, estimated cost against the
  frozen budget, elapsed seconds, the frozen case count, and the current
  checkpoint — one aggregation over the case-attempt metrics the runner
  already persists per case.
- The Run detail page renders a six-card live panel (cases completed,
  correct answers, average reward, tokens, estimated cost, elapsed)
  refreshed every 5 seconds while the page is open, with defensive shape
  guards so partial payloads degrade gracefully.
- The Runs list inlines `completed/frozen · reward · cost` for active
  tasks, polled on the same cadence.
- The endpoint requires authentication like every other route and
  exposes only pre-existing, sanitized metric fields: no prompts,
  answers, credentials, or raw provider responses are reachable through
  it.

### Dashboard overview (PR #17)

- New endpoint `GET /api/v1/overview`: active task count and the average
  reward over scored attempts of runs completed in the last 24 hours.
- The Dashboard's Average reward card, previously a hard-coded
  placeholder, now renders the live aggregate and keeps polling alongside
  the existing task feed.

## Backward compatibility

- Both endpoints are additive; no existing route, schema, or runner
  behavior changed. The OpenAPI surface grows by exactly two paths, and
  the frozen snapshot plus the route-surface test were regenerated to
  match.
- No Alembic migration ships in this release; the aggregation reads the
  existing `case_attempt.metrics_json` and task specification data.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.5
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

No migrations run at API startup beyond what v0.1.4 already installed.

## Rollback

Stop the v0.1.5 stack, select the previous source revision, and restore
its matching verified backup into an empty target. Do not roll back by
mutating the live volume in place. Re-run health and preflight checks
before resuming work.

## Security boundaries

- Both new endpoints sit behind the standard session authentication
  dependency; no anonymous access is possible.
- The aggregation reads only metric summaries the runner already commits:
  reward, correct, token counts, and estimated cost. Prompts, submitted
  answers, gold references, credentials, and raw provider responses
  remain unreachable through the API surface.
- No change to SSRF validation, DNS pinning, redirect restrictions,
  SecretStore encryption, or capability token semantics.

## Known limitations

- Progress granularity is per completed case attempt; token consumption
  inside a single running case is not streamed (the capability ledger
  still bounds it, and per-case totals land when the case settles).
- The 24-hour overview window is fixed; no custom ranges in this release.

## Verification summary

- Tests were written before implementation: progress aggregation with
  mixed succeeded/failed attempts, null averages without scored
  attempts, 404 and authentication paths, the overview 24-hour window,
  and frontend rendering for all three surfaces. Backend suite: 306
  passed. Frontend suite: 28 passed with a production build.
  `compileall`, `git diff --check`, and secret scans were clean.
- PR CI run 33182023917: linux/amd64 Compose and platform SUCCESS,
  linux/amd64 + linux/arm64 image build SUCCESS. Post-merge CI run
  33183016696: event=push, head_sha equal to merge commit
  `2550adbeb51e08f15d9c6b16363013501b1242e7`, both jobs SUCCESS.
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
