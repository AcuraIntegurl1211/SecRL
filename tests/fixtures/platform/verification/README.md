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

Docker/Playwright evidence is recorded in the handoff report when those tools
are available on the host.
