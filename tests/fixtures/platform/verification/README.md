# Milestone 4 verification evidence

This directory is reserved for small, non-sensitive verification metadata.
It must not contain SQLite files, artifact payloads, screenshots with secrets,
API keys, gold answers, capability tokens or experiment result directories.

The reproducible commands are:

```sh
python -m unittest discover -s tests/platform -t . -v
python -m unittest discover -s tests/e2e -t . -v
python -m unittest discover -s tests/failure_analysis -t . -v
npm --prefix web ci
npm --prefix web run lint
npm --prefix web test
npm --prefix web run build
```

Sanitized Docker, Playwright, architecture, backup, and test evidence for the
Milestone 4 gate is recorded in
[`2026-08-24-release-gate.md`](2026-08-24-release-gate.md). Screenshot binaries
remain outside Git and are referenced only by their handoff location.
