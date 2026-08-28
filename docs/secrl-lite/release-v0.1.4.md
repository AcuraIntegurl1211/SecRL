# SecRL Lite v0.1.4

SecRL Lite v0.1.4 makes the provider request timeout a frozen per-model
configuration parameter and fixes a latent crash in the Models page save
flow. Three consecutive multi-case evaluations on real incident data
died with non-retryable `TIMEOUT` failures because a hard-coded 30 second
request ceiling killed slow, high-context reasoning calls and took the whole
run down with them. This release contains no benchmark data changes and
introduces no database migration.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Configurable per-model request timeout (PR #15)

- `ModelParameters` gains an optional `timeout_seconds` field, bounded to
  1-600 seconds. Omitting it keeps the historical 30 second default, so
  existing model revisions behave exactly as before.
- The runner validates the value via `_model_timeout_from_parameters`
  (rejecting non-numeric and out-of-range values with
  `RUNNER_CONFIGURATION_ERROR`) and passes it to both gateway clients: the
  agent's `LegacyGatewayClient` and the evaluator's
  `EvaluatorGatewayClient`. Each client sets it on every `ModelRequest` it
  emits, so agent and evaluator provider calls are bounded by the timeout
  frozen into the selected model configuration.
- The Models page exposes an optional "Request timeout (s)" field on the
  new-revision form; the value travels through the standard frozen
  `parameters_json` of the configuration revision.
- A slow call that still exceeds the configured timeout fails as `TIMEOUT`
  without hanging the attempt. Post-dispatch timeouts remain non-retryable
  under the no-opaque-replay discipline, because provider usage may already
  have occurred.
- The OpenAPI snapshot was regenerated; the only surface change is the new
  optional parameter field.

### Models page save-flow crash fixed (PR #15)

- Creating a model revision called `event.currentTarget.reset()` after the
  network await, at which point React has already detached `currentTarget`.
  Every successful save therefore threw `Cannot read properties of null
  (reading 'reset')`, which the handler surfaced as a stale
  "Unable to save model" banner even though the revision had been stored.
  The form element is now captured before the await, and the reset, form
  close, and list refresh run on the success path as intended.

## Backward compatibility

- `timeout_seconds` is optional everywhere: API schema validation, runner
  dispatch, and stored revisions all treat absence as the 30 second default.
  v0.1.1/v0.1.2/v0.1.3 tasks and model revisions load and run unchanged.
- No Alembic migration ships in this release; the parameter lives in the
  frozen `parameters_json` of the model revision.
- The Models page fix changes client behavior only; the create endpoint
  contract is unchanged.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.4
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

No migrations run at API startup beyond what v0.1.3 already installed.
Start only the Incident profiles required by the selected tasks, run
preflight before queueing, and keep the previous source revision and its
verified backup until post-upgrade checks complete. To use the new
parameter, create a new model revision with `timeout_seconds` set (for
example 120 seconds for slow providers) and select that revision when
queueing runs.

## Rollback

Stop the v0.1.4 stack, select the previous source revision, and restore its
matching verified backup into an empty target. Do not roll back by mutating
the live volume in place. Re-run health and preflight checks before resuming
work.

## Security boundaries

- No change to SSRF validation, DNS pinning, redirect restrictions,
  SecretStore encryption, or capability token semantics.
- The timeout upper bound is enforced twice: pydantic schema validation at
  the API edge and runner-side validation before dispatch, matching the
  `ModelRequest` field bounds (1-600 seconds).

## Known limitations

- `TIMEOUT` after dispatch remains non-retryable by design; raising the
  timeout is the sanctioned remedy for slow, high-context calls.
- The evaluator shares the timeout of whichever model configuration it uses,
  including split evaluator configurations.

## Verification summary

- Tests were written before implementation: gateway-client timeout
  propagation for the agent and evaluator roles, parameter validation,
  API persistence and 422 rejection, and a frontend test asserting the
  timeout reaches the create payload. Backend suite: 300 passed. Frontend
  suite: 25 passed with a production build. `compileall`, `git diff
  --check`, and secret scans were clean.
- PR CI run 33165540848: linux/amd64 Compose and platform SUCCESS,
  linux/amd64 + linux/arm64 image build SUCCESS. Post-merge CI run
  33166879646: event=push, head_sha equal to merge commit
  `9610ba6128b7c60354510103f97296aae092c578`, both jobs SUCCESS.
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
