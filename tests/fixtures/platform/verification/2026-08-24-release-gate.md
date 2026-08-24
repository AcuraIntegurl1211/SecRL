# SecRL Lite Milestone 4 release-gate evidence

This record contains only sanitized, reproducible metadata. It contains no API
key, password, capability token, database, artifact payload, gold answer, cookie,
or raw service log.

## Revisions and hosts

- Validated code revision: `85418716996b4f3a76a13cca425d0b2bda7a0bf5`.
- Pull request base revision: `7d6ba8bd8b85c4f88e6ef96bdefd003acd5717b1`.
- Ubuntu host: Ubuntu 24.04.4 LTS, `linux/arm64`, Docker 29.6.2,
  Docker Compose 5.3.1.
- macOS host: non-Docker configuration and frontend verification only.
- Windows: Docker Desktop/WSL2 documentation and configuration review only;
  no Windows host execution is claimed.

## Ubuntu arm64 Compose

- Isolated project: `secrlm4gatearm64daaa469`.
- Sanitized `docker compose config` SHA-256:
  `86bf645186906ab3de08eee827f1110e113fdac53f26064676f9340ecebf7962`.
- Web binding: `127.0.0.1:18085`; Web, API, Runner, reference Agent Service,
  and `incident-34` all reached healthy state.
- Installed wheel loaded 589 SecRL cases; the public API returned 589 SecRL
  cases and 12 Protocol-Smoke cases.
- Runtime image: `sha256:cf829c5734bad6211ae24071d15c78dc889700f442ebcf5c6538cdafc16da701`,
  `linux/arm64`, user `secrl`.
- Runner image: `sha256:9e8a9eb59533a8528779f85f72bd9201b39715998022022bf4cbb8777a88959c`,
  `linux/arm64`, user `secrl`.
- Web image: `sha256:89c2dca12f911ea82026487028233e1d5e2b9ca26f69d036ef2245e363e052cb`,
  `linux/arm64`, user `nginx`.
- Reference Agent Service image:
  `sha256:2f4bb9447e20953ba2b358e2c85210d1903f805f74376c0a6b362d067f21df6e`,
  `linux/arm64`, user `agent`.
- Pinned MySQL image:
  `sha256:92dc869678019f65d761155dacac660a904f6245bfe1b7997da0a73b2bfc68c9`,
  `linux/arm64`.

The deterministic built-in Agent and reference Agent Service each completed all
12 Protocol-Smoke cases. Each produced 12 verified artifacts. Their canonical
semantic trajectory SHA-256 values were identical:
`c35424acddb7b7dca7553c7dc9322fcf9a5e0f72a0090fdf4a1a0389b3fa07ba`.
No external LLM was called.

The `incident-34` profile was started from an empty isolated volume. The Runner
could reach its MySQL data plane; the reference Agent Service could not. The
approved checked-in SecRL fixture was replayed inside the arm64 runtime and
matched the frozen dataset SHA-256
`cc1fd79db8627768611b8b230c23d5cb11c19b50ad25f3810dba3fe8adef8e8f`,
reward `1.0`, two steps, observation hashes, raw lengths, and truncation flags.
This is fixture parity evidence, not a claim that a populated production
Incident snapshot was exercised.

## Backup and restore

- Backup schema version: `1`; platform version: `1.0.0`.
- SQLite SHA-256:
  `a78a356c7a28f43a84fb870a6e56caf9b76c8b0b35d871a563db4ad0e472e954`.
- Artifact manifest SHA-256:
  `98e6b7fcce8fb99bfd16499f272ec2483910eccd39d01b9cfba08b9a523b428c`.
- The restored SQLite SHA-256 was identical. Restored counts were six Tasks,
  six Runs, 28 CaseAttempts, 28 Artifacts, zero Attributions, and zero
  HumanReviews.
- A copied backup with a modified SQLite payload was rejected before a target
  directory was created.

## Tests and static checks

- Python 3.11 platform tests: 214 passed.
- Protocol-Smoke/SecRL fixture E2E tests: 8 passed.
- Failure-analysis tests: 156 passed.
- Total Python tests: 378 passed.
- Vitest: 19 passed; ESLint passed; `tsc -b` and Vite production build passed.
- Alembic upgraded an empty SQLite database to
  `0004_analysis_review_persistence (head)`.
- `compileall`, POSIX shell syntax checks, and `git diff --check` passed.
- Playwright verified Dashboard, New Evaluation, Run Detail, Analysis Review,
  and Compare at `1440x900` and `390x844`. Ten handoff screenshots are stored
  outside Git at `/private/tmp/secrl-m4-playwright-9504ef2/`.

## Security checks

- No container in the isolated project mounted `/var/run/docker.sock`.
- The Web service was the only host-published service and bound only to
  `127.0.0.1`.
- Exact transient-secret scans found zero plaintext matches in service logs and
  zero matches across 99 files under `/data`.
- API key setup was verified through the authenticated model API; responses
  exposed only encrypted-credential status. The release workflow did not call
  the configured provider.
- Artifact download hash/path verification, CSRF, secure-cookie proxy handling,
  first-login password rotation, compare revision isolation, Agent Service
  network isolation, and backup tamper rejection passed their regression tests.

## GitHub Actions

- Workflow definition:
  `https://github.com/AcuraIntegurl1211/SecRL/actions/workflows/secrl-lite-release-gate.yml`.
- Source-validation run for `85418716996b4f3a76a13cca425d0b2bda7a0bf5`:
  `https://github.com/AcuraIntegurl1211/SecRL/actions/runs/32692505185`.
- `linux/amd64 Compose and platform`: passed, including clean-volume startup,
  health, frontend build, platform tests, Protocol-Smoke, backup/restore, and
  project cleanup.
- `linux/amd64 + linux/arm64 image build`: the final PR HEAD check is the
  authoritative result because committing this evidence changes the PR HEAD.
- No image was pushed to a registry.

The GitHub runner emitted a non-blocking deprecation annotation because several
official actions still target Node.js 20 while the runner forces Node.js 24.
