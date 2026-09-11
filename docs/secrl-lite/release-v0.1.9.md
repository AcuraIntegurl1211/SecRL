# SecRL Lite v0.1.9

SecRL Lite v0.1.9 is a feature release. It decouples benchmark behavior from
hardcoded benchmark ids behind explicit adapter capabilities (PR #25) and adds
a reference bridge that lets an external Agent Service speak the platform's
Agent Service Protocol v1 to a Flocks deployment (PR #26). No benchmark data
changes and no database migration are included.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Adapter capabilities replace benchmark-id gates (PR #25)

- Task creation, preflight, the benchmark catalog, the runner, and failure
  analysis previously branched on `benchmark_id == "secrl"`. Adding a new
  deterministic benchmark meant editing those branches.
- Adapters now declare three explicit switches — `needs_llm_evaluator`,
  `requires_incident_services`, `supports_failure_analysis` — and every gate
  reads them instead of comparing ids. Adapters register into a shared
  `BenchmarkRegistry` that validates and freezes capabilities at registration
  and builds a fresh instance per run, so an unregistered benchmark fails
  closed at every entry point.
- The switches are deliberately kept **out** of `BenchmarkManifest`: the
  manifest is pinned into `BenchmarkRevision`/`TaskSpec`/`RunSpec` SHA-256
  identities, so this release changes no historical hash.
- This is the integration point for a new benchmark: register an adapter plus
  a factory and its capabilities; no API-route branch is required.

### Flocks bridge as an Agent Service v1 example (PR #26)

- `examples/flocks_adapter/` is a self-contained FastAPI service that exposes
  the four endpoints the platform's runner consumes (`GET /v1/manifest`,
  `POST /v1/sessions`, `POST /v1/sessions/{id}:act`,
  `POST /v1/sessions/{id}:close`) and translates each turn onto Flocks' native
  session API.
- It is an example integration, not part of the platform package: the platform
  never imports it, and it depends only on the frozen protocol models.
- The platform capability token is checked for presence only and never
  forwarded to Flocks; Flocks receives its own bearer token from the
  environment. The manifest is static per configuration so its SHA-256 can be
  pinned at registration, and the model identifier is embedded in the manifest
  version so a model change yields a new agent revision.

## Backward compatibility

- No schema, API, or task-format change. Existing model revisions, tasks,
  RunSpecs, and BenchmarkRevision hashes are untouched.
- PR #25 is behavior-preserving: the built-in capability declarations reproduce
  the previous hardcoded semantics exactly — SecRL reports all three switches
  true and protocol-smoke reports all three false, matching the branches they
  replace. The SecRL evaluator-profile construction and the SecRL failure
  analyzer materialization remain SecRL-specific by design.
- PR #26 is purely additive under `examples/`; it changes no platform module.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.9
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

There is no database migration and no configuration change required to keep
existing SecRL runs behaving as before. Operators adopting the Flocks bridge
additionally set the adapter's environment and add its host to
`SECRL_AGENT_SERVICE_ALLOWLIST`; see `examples/flocks_adapter/README.md`.

## Rollback

Stop the v0.1.9 stack, select the previous source revision, and restore its
matching verified backup into an empty target. Do not roll back by mutating the
live volume in place. Re-run health and preflight checks before resuming work.

## Security boundaries

- The Flocks adapter reads its Flocks credential from the environment only; no
  secret is hardcoded or logged, and the platform capability token is never
  forwarded to Flocks.
- Agent Service endpoints are governed by `SECRL_AGENT_SERVICE_ALLOWLIST`, a
  separate list from the model-provider allowlist; the pinned-internal-HTTP
  endpoint policy is unchanged.
- Agent Services return structured Actions only. Benchmark tools are executed
  by the platform. No benchmark guardrail is widened by this release.

## Known limitations

- The Flocks bridge is **experimental**: it has not been exercised against a
  live Flocks deployment. Two integration points remain unconfirmed with the
  Flocks side — whether the dedicated evaluation agent truly bypasses all tool
  execution, and whether Flocks exposes per-message token usage (the adapter
  reports zeros and marks the manifest `+no-usage` when it is absent). Until
  both are verified, treat single-case smoke results as indicative only.
- Adapter capabilities are runtime switches validated at registration; they
  are intentionally not part of any frozen manifest hash, so a change to a
  benchmark's declared capabilities must be covered by the consumer tests that
  assert each switch.

## Verification summary

- PR #25: tests were written before implementation. The new
  `tests/platform/test_adapter_capabilities.py` (18 cases) asserts every
  consumer of every switch and includes a third-party stub benchmark that
  flows through task creation and preflight with zero route changes — the
  new-benchmark readiness acceptance. Post-merge CI reported event=push on
  main with head_sha equal to merge commit
  `290c32f13f0f62257be50e5ed523827992008066`, and both required jobs —
  linux/amd64 Compose and platform, and linux/amd64 + linux/arm64 image
  build — returned SUCCESS.
- PR #26: an independent review found that Flocks has shipped two status
  payload shapes when awaiting idle; the fix (commit
  `3b38597c024e56410fb14a9a464399a18ac00f5c`) accepts both and was re-verified
  by PR CI (run 34578634172, both jobs SUCCESS). The 30 offline cases in
  `tests/platform/test_flocks_adapter.py` inject a fake Flocks ASGI app and
  drive the adapter through the platform's own `AgentServiceRuntime` to prove
  wire compatibility, manifest-hash registration, correlation and sequence
  handling, the grammar/correction path, usage passthrough and `+no-usage`
  downgrade, capability presence, and fail-closed behavior.
- Combined backend suite on the merged main: 527 passed with 277 subtests
  (pytest) and 368 tests (unittest discovery). `compileall`,
  `git diff --check`, and secret scans were clean.
- All verification used mocked providers and a fake Flocks service. No real
  LLM or Flocks calls were made and no provider spend occurred during
  development, review, or CI.

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
