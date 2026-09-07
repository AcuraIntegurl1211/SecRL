# SecRL Lite v0.1.8

SecRL Lite v0.1.8 is a patch release fixing the runner-side gap in the
per-host HTTP endpoint allowlist introduced in v0.1.7. No benchmark data
changes and no database migration are included.

This release does not publish public Docker images. Operators build the
audited source revision locally with Docker Compose.

## Highlights

### Insecure endpoint allowlist now applies on the runner path (PR #23)

- v0.1.7 threaded `SECRL_ALLOW_INSECURE_MODEL_ENDPOINTS` into model-creation
  validation but not into the runner. `OpenAICompatibleProvider` validates
  the endpoint again in its constructor and once more before every request,
  and those call sites did not pass the allowlist.
- Effect on v0.1.7: a model revision approved for an `http://` endpoint
  could be created successfully, but any run using it failed at dispatch
  time with `AGENT_RUNTIME_ERROR` because the provider constructor rejected
  the endpoint.
- The fix passes the configured insecure-host list through the provider
  constructor, the per-request endpoint revalidation, and the runner's
  `DeferredSecretProvider` factory, so a revision that passes creation-time
  validation also executes.
- HTTPS-only behavior is unchanged for every host that is not explicitly
  listed, and all other endpoint checks (allowlist membership, global-IP
  requirement, userinfo/query/fragment rejection, DNS resolution) stay
  enforced on the runner path as well.

## Backward compatibility

- No schema, API, or task-format change. Existing model revisions, tasks,
  and RunSpecs are untouched.
- Hosts listed in `SECRL_ALLOW_INSECURE_MODEL_ENDPOINTS` gain exactly the
  behavior v0.1.7 documented; unlisted hosts and all HTTPS endpoints behave
  identically to v0.1.7.

## Upgrade

Back up platform data before upgrading, then follow the standard flow:

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.8
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

Only operators who actually configured HTTP-allowed hosts are affected by
this fix; everyone else may upgrade or skip v0.1.8 and take the change in a
later release.

## Rollback

Stop the v0.1.8 stack, select the previous source revision, and restore its
matching verified backup into an empty target. Do not roll back by mutating
the live volume in place. Re-run health and preflight checks before resuming
work.

## Security boundaries

- The HTTP exception remains per-host and configuration-gated. The runner
  now enforces the same host list as the API edge; no new escape hatch is
  introduced and none is widened.
- All other provider guardrails are unchanged.

## Known limitations

- Unchanged from v0.1.7: the exception list is host-exact with no port or
  path scoping, and it applies to model endpoints only.

## Verification summary

- Tests were written before implementation for the provider construction
  path (approved host accepted, unapproved host still rejected) alongside
  the existing v0.1.7 endpoint tests. Backend suite: 320 passed.
  `compileall`, `git diff --check`, and secret scans were clean.
- Post-merge CI for the fix (PR #23) reported event=push on main with
  head_sha equal to merge commit
  `55fb82153b93982e7cb8b77c9a624f52c98cad91`, and both required jobs —
  linux/amd64 Compose and platform, and linux/amd64 + linux/arm64 image
  build — returned SUCCESS.
- All verification used mocked providers. No real LLM calls were made and
  no provider spend occurred during development, review, or CI.

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
