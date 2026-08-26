# SecRL Lite v0.1.2

SecRL Lite v0.1.2 adds two platform capabilities to the Lite runtime: an
optional split evaluator model configuration for SecRL tasks, and an opt-in
local development auto-authentication mode with a hardened two-flag fence.
This release contains no benchmark data changes and introduces no database
migration.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Split evaluator model configuration (PR #10)

- SecRL task creation accepts an optional
  `evaluator_model_config_revision_id`. When the field is omitted, behavior
  is identical to v0.1.1: one frozen model config serves both the agent and
  the evaluator. Passing the agent's own config normalizes back to that
  legacy single-config specification.
- A split evaluator config must have a saved encrypted credential, a positive
  output token limit and frozen input/output pricing; otherwise task creation
  fails with `INVALID_TASK_SPEC` before anything is queued.
- Preflight reports a named `evaluator_model_secret` check for split tasks,
  including `not_applicable` when the evaluator intentionally reuses the agent
  config.
- The runner freezes both revision IDs and SHA-256 hashes into the task spec,
  binds the frozen evaluator profile to the evaluator config hash, resolves a
  second provider bundle keyed by `evaluator_model_config_sha256`, and wires
  it into the capability-restricted gateway client whose token allows only the
  `evaluator` model role.
- The New Evaluation page adds an "Evaluator model" dropdown for SecRL with
  preflight and queue payload support. The OpenAPI document gains exactly one
  optional request field and one optional query parameter; the frozen
  snapshot was regenerated accordingly.
- Cost accounting remains separated: `evaluator_*` metrics are priced with
  the evaluator config's own pricing.

### Local development auto-authentication (PR #11)

- Starting the API with both `SECRL_DEV_AUTOAUTH=true` and
  `SECRL_DEV_AUTOAUTH_CONFIRM=yes` serves every request as the local `admin`
  account without a session cookie or CSRF token. The web console opens
  directly into the workspace; no frontend changes were required because the
  existing session probe succeeds naturally.
- The confirmation fence is enforced on every launch path. `create_app`
  rejects startup when the flag is set without the exact confirmation value,
  both when settings are injected directly and on the uvicorn factory path
  (`secrl-lite serve`) where settings materialize at lifespan time.
- When enabled, the API logs an explicit warning banner. If no active admin
  account exists, authentication dependencies fail closed with
  `AUTOAUTH_UNAVAILABLE` instead of serving anonymous access.
- The initial-password rotation gate is unchanged: a fresh deployment still
  completes its one-time administrator password change before auto-
  authenticated access is granted.
- The Compose files do not pass either variable through.

### Critical fence fix during review

The first implementation of the confirmation fence checked only injected
settings. Production launches use `uvicorn.run(..., factory=True)` which calls
`create_app()` without arguments, so a single environment variable would have
silently disabled authentication on the real deployment path while tests
remained green. The fence now runs against lifespan-time effective settings as
well, and a regression test boots the no-argument factory with only
`SECRL_DEV_AUTOAUTH` set and asserts startup refusal.

## Backward compatibility

- v0.1.0 and v0.1.1 task specifications contain no split-evaluator keys and
  continue to resume, recover and display unchanged; absence of the keys means
  legacy single-config semantics everywhere.
- No Alembic migration is required: split references live inside the frozen
  `task_spec_json`, so historical RunSpec hashes are never rewritten.
- The OpenAPI surface only grows by the optional field and query parameter;
  nothing was removed or renamed.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.2
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

No new migrations ship in this release; API startup applies none beyond what
v0.1.1 already installed. Start only the Incident profiles required by the
selected tasks, run preflight before queueing, and keep the previous source
revision and its verified backup until post-upgrade checks complete.

## Rollback

Stop the v0.1.2 stack, select the previous source revision, and restore its
matching verified backup into an empty target. Do not roll back by mutating
the live volume in place. Re-run health and preflight checks before resuming
work.

## Security boundaries

- The split does not weaken any provider boundary: SecretStore encryption,
  SSRF validation, DNS pinning and redirect restrictions apply to the
  evaluator bundle exactly as to the agent bundle.
- The evaluator prompt template remains hash-frozen, per-task parameter
  overrides remain rejected, and evaluator response artifacts stay restricted.
- Dev auto-authentication is a local development convenience with a two-flag
  handshake, fail-closed startup on misconfiguration and a loud warning when
  active. Never enable it on shared hosts, public deployments, or anywhere
  the port reaches beyond the machine itself.

## Known limitations

- Split evaluator configuration applies to SecRL benchmark tasks only;
  Protocol-Smoke keeps its deterministic path.
- Platform host support claims are unchanged from v0.1.1, including the
  documentation-only status of Windows validation and the absence of macOS
  container smoke evidence.
- Auto-authentication intentionally preserves the password-rotation gate, so
  brand-new deployments see one interactive step before frictionless access.

## Verification summary

- Tests were written before implementation for both features. Per-branch
  backend suites finished green (269 passed each, including new API contract,
  runner wiring and auth-fence tests); the frontend suite passed 24 tests and
  a production build; `compileall`, `git diff --check` and secret scans were
  clean; the OpenAPI snapshot test passed after the intentional contract
  update.
- A pre-merge simulation combined both feature branches in merge order and
  ran the full suites on the result (275 backend, 24 frontend) without
  conflicts.
- Post-merge CI on main passed all required jobs twice: run 32935827767 for
  PR #10 (merge commit `5d3df03`) and run 32936882306 for PR #11 (merge
  commit `bd4f700`), each covering the linux/amd64 Compose and platform gate
  plus the linux/amd64 + linux/arm64 image build.
- All verification used mocked providers. No real LLM calls were made and no
  provider spend occurred at any point during development, review or CI.

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
