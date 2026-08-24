# SecRL Lite v0.1.0

SecRL Lite v0.1.0 is the first local-first management release for running and
reviewing reproducible SecRL benchmark evaluations. This document is the release
candidate changelog and integration checklist; the release is not tagged until
the Integration PR and the automatic post-merge gate on `main` both succeed.

## Core capabilities

- Authenticated model, Agent, Agent Service, Benchmark, task, run, comparison,
  failure-attribution, and HumanReview management.
- Encrypted model credentials, structured Agent actions, persistent budgets,
  Runner leases/fencing, pause/resume/cancel, and content-addressed artifacts.
- Frozen Protocol-Smoke and SecRL datasets, official evaluator wrapping,
  fixture parity, trajectory inspection, and auditable analysis/review records.
- SQLite WAL persistence plus hash-verified backup, restore, and tamper rejection.

## Start with Docker Compose

Copy `.env.example` to `.env`, fill every required value with a unique local
secret, then start the local-only platform:

```sh
docker compose build
docker compose up -d
curl --fail http://127.0.0.1:8080/api/v1/health
```

Incident MySQL services are opt-in through explicit profiles. The default Web
publish remains bound to `127.0.0.1`; no service receives the Docker Socket.

## Supported architectures and release gates

- GitHub Actions builds and tests `linux/amd64`; Buildx/QEMU builds the runtime,
  Web, and reference Agent Service for both `linux/amd64` and `linux/arm64`.
- Ubuntu 24.04 arm64 has real Compose, health, Protocol-Smoke, SecRL fixture,
  persistence, backup/restore, and tamper-rejection evidence in
  `tests/fixtures/platform/verification/2026-08-24-release-gate.md`.
- The validated development candidate is
  `29b26f3914b786624bd9b91192102c267ab6b43e`; its automatic push gate is
  <https://github.com/AcuraIntegurl1211/SecRL/actions/runs/32696283933>.
- Integration PRs and post-merge pushes to `main` run the same complete gate.

## Security boundaries

- The platform is local-first, uses least-privilege `contents: read` CI, and
  neither stores model API keys in browser storage nor returns secret plaintext.
- Gold answers, database credentials, capability tokens, and evaluator-private
  inputs remain outside Agent context, public API responses, logs, and artifacts.
- Agent Services return structured Actions only. Benchmark tools execute in the
  platform, and no platform component receives a Docker Socket.
- Plain HTTP Agent Service endpoints and model providers remain subject to
  allowlists, DNS validation, redirect restrictions, SSRF checks, and budgets.

## Backup, restore, and upgrade

Create and verify a backup before changing images or the data volume. Migrations
run automatically on API startup; keep the previous image and its matching
backup until post-upgrade queries and artifact hashes have been checked.

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.0
docker compose up -d --build
./scripts/lite-backup.sh ./backups/v0.1.0
```

Restore accepts only an empty target and an exactly compatible platform/schema
version. Pre-release backups whose manifest says `1.0.0` must be restored with
the matching pre-release binary; create a fresh `0.1.0` backup after upgrade.
Rollback means stopping v0.1.0, selecting the previous image, and restoring its
matching verified backup rather than modifying the live volume in place.

## Known limitations

- One Runner process and SQLite only; multi-worker, Redis, PostgreSQL, and public
  Internet deployment are outside Lite scope.
- Incident databases are supplied and managed externally through explicit
  profiles; the platform runtime never creates, deletes, or rebuilds containers.
- macOS has configuration/frontend validation but no Docker host smoke evidence.
  Windows is Docker Desktop/WSL2 documentation validation only.
- No public image is published by CI. Operators build locally from the audited
  source revision.

## Integration inventory

- Original SecRL research baseline: four commits for deterministic experiment
  selection, DeepSeek compatibility, and reproduction documentation.
- Failure analysis: 43 commits for identity, taxonomy, attribution, SQL retrieval
  subtyping, atomic reports, review validation, and regression coverage.
- Lite Milestones 1–4: platform contracts, encrypted runtime, API/Runner,
  SecRL adapters/evaluator, analysis/HumanReview, Web UI, Compose, and recovery.
- CI, documentation, and release evidence: multi-architecture release gate,
  Ubuntu arm64 evidence, operational/security guides, and post-merge triggering.

The Integration PR adds no raw experiment results, database files, caches,
build output, API keys, repository secrets, or Docker Socket mounts. Existing
research logs already present on `main` are not changed by this integration.

## Release preparation verification

- Local Python 3.12 locked environment: 218 platform, eight E2E/parity, and 156
  failure-analysis tests passed (382 total).
- Frontend: ESLint passed, Vitest passed 19 tests, TypeScript/Vite production
  build passed, and `npm audit --audit-level=low` found zero vulnerabilities.
- Alembic upgraded an empty database to
  `0004_analysis_review_persistence (head)`; compileall, POSIX shell syntax, and
  `git diff --check` passed.
- A CLI backup recorded platform version `0.1.0`; restore reproduced the exact
  SQLite SHA-256, while a tampered copy was rejected without creating a target.
- The Integration PR must still pass the complete linux/amd64 Compose and
  amd64/arm64 image jobs. After merge, the same workflow must run automatically
  as a `push` event on `main` before any tag or GitHub Release is created.
