# SecRL Lite operations

## Queue and recovery

Create tasks from **New evaluation**. The four steps freeze scope, runtime,
reliability limits and budget/review before the task enters the queue. **Runs**
shows state, lease checkpoint and RunSpec hash. Pause, resume and cancel are
state-machine operations; a retry creates a new attempt and never overwrites a
prior attempt.

The platform uses one Runner process. Its lease and fencing token protect the
active Run across a restart. Do not run a second worker against the same data
directory.

## Artifacts and analysis

Artifacts are content-addressed under `/data/artifacts/sha256/...`. The Run
detail page loads case lists first and requests full trajectory content only
when explicitly selected. Failure analysis materializes immutable inputs,
verifies every hash, registers restricted outputs, and appends HumanReview
revisions with audit events. Automatic Attribution remains unchanged.

## Backup and restore

```sh
./scripts/lite-backup.sh ./backups/$(date -u +%Y%m%dT%H%M%SZ)
./scripts/lite-restore.sh ./backups/20260823T000000Z ./restored-data
```

The backup contains an online SQLite copy, all content-addressed artifacts,
an artifact manifest and a versioned root manifest. Restore verifies schema,
paths, database hash, artifact list and every artifact hash before replacing
an empty target. Tampered, incomplete, newer-version and path-traversal
backups are rejected without modifying the target.

## Disk and logs

Use `docker compose logs --tail=200 api runner` for operational diagnostics.
Never paste environment values or Authorization headers into an issue. Stop
the stack before moving the named `platform_data` volume. Incident volumes
are independent and can be cleaned only after the corresponding profile is
retired and a verified backup exists.
