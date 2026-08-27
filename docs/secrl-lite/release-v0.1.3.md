# SecRL Lite v0.1.3

SecRL Lite v0.1.3 fixes a single platform defect in the runner: capability
tokens expired after 300 seconds and were never refreshed, so every
multi-case evaluation whose total duration exceeded the token lifetime
failed deterministically. The fix rotates tokens under an active run lease
and propagates them to every holder without changing the token format,
claims structure, or budget semantics. This release contains no benchmark
data changes and introduces no database migration.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Capability token rotation for long-running evaluations (PR #13)

- The runner engine now invokes a `CapabilityTokenRotator` before every case
  attempt and between agent steps. A token within 60 seconds of expiry is
  re-signed via `CapabilitySigner.refresh()`; when the previous token has
  already expired — for example when a case boundary lands past the original
  lifetime — the rotator re-issues its tracked claims behind the same
  active-run-lease gate so the boundary cannot strand the run.
- Refreshed tokens are propagated in place to every holder: the
  `CapabilityBudgetGuard` (token, claims, and sha256 binding),
  `LegacyGatewayClient`, `EvaluatorGatewayClient`, and the SERVICE-agent
  configuration holder consumed each time the runtime is rebuilt per case.
  Budget accounting keys on `(run_id, agent_revision_id)`, so rotation never
  resets, forks, or double-counts usage.
- The lease gate is now actually wired: `capability_signer()` forwards an
  optional probe, and the runner passes
  `RunnerRepository.run_lease_is_active()` (owner + fence + unexpired
  triple check), closing the previously missing `lease_is_active` wiring
  that would have made any refresh raise `CapabilityScopeError`.
- `CapabilitySigner` gains `inspect()`: a signature-only claims view used by
  the rotator to track claims across rotations. No verification path was
  loosened.
- Engine instances constructed without a rotator behave exactly as before;
  rotation is an opt-in parameter supplied by the runner dispatch.

### Root cause chain (v0.1.2 behavior)

Dispatch issued one capability token with `expires_at = issued_at + 300`
and passed it once to the agent gateway client, the evaluator gateway
client, and the budget guard; closures captured it for the whole run.
The engine loop ran all cases serially inside a single dispatch without
re-issuing, and although `CapabilitySigner.refresh()` existed with a lease
gate, no production code called it and the runner's signer had no
`lease_is_active` callback wired. On any run outliving T+300s the next
gateway verify raised `ExpiredCapability`; because that exception surfaced
inside the agent call, the attempt was classified as
`AGENT_RUNTIME_ERROR` and the run failed. Production evidence:
incident_38 task `4185a5a8` succeeded on three cases (~100s each), then
died on the fourth exactly at task start + 300 seconds.

## Backward compatibility

- Token format, claim structure, budget semantics, and the 300-second
  `max_lifetime_seconds` issuance cap are unchanged. Refreshed tokens keep
  the original claims verbatim except for `issued_at` / `expires_at` /
  `nonce`.
- Runs shorter than the token lifetime follow byte-for-byte identical code
  paths: the rotator only acts when remaining lifetime reaches the
  threshold, and short runs never cross it in practice.
- The new rotation parameter on the engine is optional; dispatch wiring is
  internal to the runner process. There is no API, schema, or OpenAPI
  surface change of any kind.
- No Alembic migration is required: nothing about how tasks, runs, or
  ledger state are persisted changed.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.3
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

No new migrations ship in this release; API startup applies none beyond
what v0.1.2 already installed. In-flight QUEUED tasks keep their frozen
task specs — previously failed long runs can simply be re-queued after the
upgrade. Start only the Incident profiles required by the selected tasks,
run preflight before queueing, and keep the previous source revision and
its verified backup until post-upgrade checks complete.

## Rollback

Stop the v0.1.3 stack, select the previous source revision, and restore its
matching verified backup into an empty target. Do not roll back by mutating
the live volume in place. Re-run health and preflight checks before resuming
work.

## Security boundaries

- Refresh authorization still requires an active run lease held by this
  runner instance: both the refresh path and the already-expired fallback
  path check owner identity, fencing token, and expiry via
  `run_lease_is_active()`. A stale or foreign worker can never mint or
  extend capabilities for a run it does not hold.
- Re-issued claims can never inflate lifetime beyond the signer's
  `max_lifetime_seconds`: refreshed tokens pass through `verify()` and are
  rejected if they exceed the cap.
- The budget ledger remains keyed by `(run_id, agent_revision_id)` across
  rotations, preserving consumption continuity; rotation grants no
  additional tokens or cost headroom.
- Raw capability tokens are never logged, persisted into artifacts, or
  exposed over the API; holders store them as non-repr secrets and compare
  only sha256 bindings.

## Known limitations

- If a single agent step itself takes longer than the remaining token
  lifetime measured at the start of that step, the request can still fail
  during post-response reconciliation; avoiding this requires provider-side
  interruption support and remains future work.
- Rotation addresses the runner capability lifetime only. Task-level budgets
  (`max_tokens`, `max_cost`) continue to bound total spend, unchanged.
- Platform host support claims are unchanged from v0.1.2, including the
  documentation-only status of Windows validation and the absence of macOS
  container smoke evidence.

## Verification summary

- Tests were written first and observed failing before implementation: the
  symptom-pin test reproduced the production failure mode (second case dies
  with `AGENT_RUNTIME_ERROR` past the original expiry, classified exactly as
  observed in production), and the rotation tests initially failed at import
  time against the baseline module. All then passed after the fix.
- The backend suite finished green: 292 tests plus 78 subtests passed
  (`pytest tests/platform -q`), including 17 new rotation tests covering
  multi-case survival past expiry, step-level rotation within a single case,
  lease-gated rejection, holder propagation, and the lease probe itself.
- `compileall`, `git diff --check`, and secret scans were clean.
- Post-merge CI on main passed all required jobs: run `33042325888` for PR
  #13 (merge commit `607ad30`), covering the linux/amd64 Compose and
  platform gate plus the linux/amd64 + linux/arm64 image build.
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
