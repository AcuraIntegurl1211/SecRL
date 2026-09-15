# SecRL Lite v0.1.10

SecRL Lite v0.1.10 is a feature release. It makes the Agent Service per-request
timeout configurable, so external Agent Services that synchronously await an
upstream model can complete a turn (PR #30), and it carries two adapter-side
improvements to the Flocks bridge example: calibration against the live wire
shapes (PR #28) and an adapter-owned turn budget banner (PR #31), plus the
verified recipe for a tool-less evaluation agent (PR #29). No benchmark data
changes and no database migration are included.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Agent Service timeout is now a configuration knob (PR #30)

- The Agent Service Protocol v1 transport hardcoded a **10-second** budget for
  every request. That is too small for a service whose `:act` synchronously
  awaits an upstream model: the first live SecRL run through the Flocks bridge
  completed its fastest case but lost three consecutive attempts on a slower
  one to retryable `DEADLINE_EXCEEDED` (each attempt consuming two 10s
  transport timeouts).
- `SECRL_AGENT_SERVICE_TIMEOUT_SECONDS` now sets the per-request budget
  (bounds 1-600, default 10). The runner threads it into both the per-run
  `ServiceConfig` and the transport it owns.
- The agent `:check` manifest fetch deliberately keeps the strict 10-second
  budget: a manifest endpoint that cannot answer promptly should still fail
  registration.
- Deployments that want the larger budget must add the variable to
  `compose.override.yaml` for the `api` and `runner` services; the platform
  environment anchor does not pass it through.

### Flocks bridge example: live shapes and a turn budget banner (PR #28, #31)

- Probing a live Flocks deployment showed `GET /api/session/{id}/message`
  returns a **bare JSON array** of `MessageWithParts` entries (role and token
  stats under `info`, text under `parts`) rather than the page object the
  adapter assumed, and that token usage arrives as
  `info.tokens.{input,output,reasoning,cache}`. Both are now handled, with the
  previous shapes kept as fallbacks.
- Assistant replies can be split across several text parts (an empty part
  first); parts are joined instead of the message being discarded.
- Every prompt now opens with an adapter-owned counter banner
  (`[Turn k/max, n remaining]`) that escalates once half the budget is spent
  and demands `SUBMIT` on the last two turns. This is adapter-side state and
  cannot drift with model behavior.
- The README records the verified tool-less agent recipe: Flocks' primary
  `rex` agent silently expands an empty `tools: []` into *all* builtin tools,
  so a dedicated storage custom agent (`tools: []`, `*: deny` permission,
  `delegatable: false`) is required to keep the episode's tool use inside the
  platform.

## Backward compatibility

- With `SECRL_AGENT_SERVICE_TIMEOUT_SECONDS` unset, behavior is byte-identical
  to v0.1.9: the transport budget stays at 10 seconds and no other Agent
  Service semantics change.
- PR #28, #29 and #31 touch `examples/flocks_adapter/` and documentation only.
  The platform never imports the adapter, so no platform behavior, schema, or
  hash changes because of them.
- No schema, API, or task-format change. Existing model revisions, tasks,
  RunSpecs, and BenchmarkRevision hashes are untouched.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.10
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

There is no database migration. Operators who connect an external Agent
Service should additionally set in `compose.override.yaml` (for both `api`
and `runner`):

```yaml
SECRL_AGENT_SERVICE_TIMEOUT_SECONDS: "180"
```

## Rollback

Stop the v0.1.10 stack, select the previous source revision, and restore its
matching verified backup into an empty target. Do not roll back by mutating the
live volume in place. Re-run health and preflight checks before resuming work.

## Security boundaries

- The new timeout is a single bounded knob (validated to 1-600 seconds). It
  does not widen any allowlist, SSRF rule, or endpoint policy, and the strict
  10-second budget is retained on the `:check` path.
- The Flocks bridge still checks the platform capability token for presence
  only and never forwards or verifies it; Flocks credentials remain
  environment-only, and the adapter rejects redirects rather than following
  them.
- Agent Services return structured Actions only. Benchmark tools continue to
  be executed by the platform, which is why the bridge requires a tool-less
  evaluation agent.

## Known limitations

- The Flocks bridge remains **experimental**. It has been exercised against one
  live deployment for a small number of turns, not for a full benchmark run.
  The two integration points confirmed there (tool bypass via the dedicated
  agent, and usage exposure) are configuration-dependent: a different Flocks
  agent or build can change both.
- Platform cost and token accounting cannot observe an external agent's own
  model spend; budgets constrain platform-gateway calls only. External-agent
  spend must be reconciled on the provider side.
- The single live comparison case through the bridge now completes the episode
  and submits, but scored partial credit; answer quality is bounded by the
  configured external model, not by the bridge.

## Verification summary

- PR #30: tests were written before implementation and pin the knob at every
  consumer — transport default (10s), custom budget, expiry against a real
  silent TCP socket, per-request budget semantics, `ServiceConfig` bounds,
  `Settings` validation, and the runner wiring helper. Merge commit
  `6aaf6303b77c09cb8554587231e36186e3df2f48`; post-merge CI reported
  event=push on main with head_sha equal to that commit and both required jobs
  SUCCESS.
- PR #31: five tests pin the banner text, urgency boundaries, clamping, and the
  per-turn prefix observed on real act prompts. Merge commit
  `1a150f5fc04d55af55e61fde4192de671939b7b7`; post-merge CI on main with the
  matching head_sha returned SUCCESS for both jobs.
- PR #28: merge commit `e6e037e603f9a9137bf4fd45aa6ed8543576f73d`, PR CI both
  jobs SUCCESS. PR #29 (documentation): merge commit
  `242107c86a3cb72ca67a5a6167f5ec92607496d8`, PR CI both jobs SUCCESS.
- Combined backend suite on the merged main: 542 passed with 277 subtests
  (pytest) and 383 tests (unittest discovery). `compileall`,
  `git diff --check`, and secret scans were clean.
- All CI verification used mocked providers and a fake Agent Service. The live
  Flocks probing described in the adapter README was a separately authorized
  session against a local deployment; it produced no platform-gateway spend
  and no benchmark data was written.

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
