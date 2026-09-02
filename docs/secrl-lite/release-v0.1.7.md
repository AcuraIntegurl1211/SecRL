# SecRL Lite v0.1.7

SecRL Lite v0.1.7 adds a precise escape hatch for self-hosted
OpenAI-compatible proxies that only serve plain HTTP: operators can now
approve specific hosts for insecure transport through an explicit
configuration list, while every other guardrail around model endpoints
stays exactly as strict as before. This release contains no benchmark
data changes and introduces no database migration.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Explicit per-host HTTP endpoint allowlist (PR #21)

- New setting `SECRL_ALLOW_INSECURE_MODEL_ENDPOINTS`: a host list
  (JSON array, default empty). An `http://` model endpoint is accepted
  only when its host appears in that list; with the list empty, the
  historical HTTPS-only behavior is unchanged.
- Every other endpoint check is preserved and enforced for insecure
  hosts as well: global-IP requirement (private and loopback addresses
  stay rejected), provider allowlist membership, rejection of embedded
  user information and query/fragment components, DNS resolution to a
  global address, and the correct port-80 default for scheme http.
- The runner needs no configuration: the provider is built from the
  stored endpoint and validated allowlist, and capability-token,
  budget-ledger, and SecretStore semantics are unchanged.
- The API edge enforces the same bounds twice: pydantic schema
  validation for the parameter envelope and endpoint validation at
  model-creation time.

## Backward compatibility

- The new setting defaults to empty: every existing model revision,
  task, and validation path behaves identically to v0.1.6.
- No Alembic migration ships in this release; the endpoint is stored in
  the existing frozen model-revision fields.
- No changes to the runner dispatch, capability tokens, budget
  accounting, or failure analysis.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.7
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

To use an HTTP-only proxy, add its host to both
`SECRL_MODEL_PROVIDER_ALLOWLIST` and
`SECRL_ALLOW_INSECURE_MODEL_ENDPOINTS` (for example via
`compose.override.yaml`, for the api and runner services), recreate the
stack, and add the model revision through the Models page. Keep the
previous source revision and its verified backup until post-upgrade
checks complete.

## Rollback

Stop the v0.1.7 stack, select the previous source revision, and restore
its matching verified backup into an empty target. Do not roll back by
mutating the live volume in place. Re-run health and preflight checks
before resuming work.

## Security boundaries

- The insecure transport exception is **per-host and configuration-
  gated**: only hosts explicitly listed can use http://, and removing a
  host from the list immediately blocks new revisions from using it.
- All other endpoint guardrails remain in force for approved insecure
  hosts: allowlist membership, global-IP requirement, credential
  rejection in URLs, DNS resolution checks, and the existing
  SecretStore/capability/budget semantics for every call.
- Operators should weigh plaintext credential transport before
  approving a host; HTTPS remains the default and the recommendation.

## Known limitations

- The exception list is host-exact; no port- or path-scoped matching in
  this release.
- The setting applies to model endpoints only; Agent Service transport
  rules are unchanged.

## Verification summary

- Tests were written before implementation: approval gating (approved,
  unapproved, empty-list), allowlist enforcement for insecure hosts,
  private-IP rejection, userinfo and query rejection, port-80 default,
  HTTPS default preservation, and API 201/422 paths for both the
  configured and strict applications. Backend suite: 318 passed.
  `compileall`, `git diff --check`, and secret scans were clean.
- PR CI run 33590488953: linux/amd64 Compose and platform SUCCESS,
  linux/amd64 + linux/arm64 image build SUCCESS. Post-merge CI run
  (event=push, head_sha equal to merge commit
  `08ab95e4012e92959e7b40f5d266587427ccfa68`): both jobs SUCCESS.
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
