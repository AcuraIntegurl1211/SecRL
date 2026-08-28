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

Before queueing, the UI calls `/api/v1/preflight`. A missing database,
read-only SecRL Incident credential, model secret, or agent revision is
reported as a named check with a next step; secret values are never returned.
For SecRL, scope can be one or more Cases, one or more complete Incidents, or
the full 589-case Benchmark. The API resolves and freezes the actual Case ID
list, Dataset revision, Dataset SHA-256 and RunSpec before execution.

SecRL tasks may optionally score with a separate frozen evaluator model
config: pass `evaluator_model_config_revision_id` at task creation (or pick
"Evaluator model" in the UI). The evaluator config must have a saved
credential, an output token limit and frozen pricing; it is bound into the
frozen evaluator profile by its own SHA-256. Omitting the field keeps the
historical behavior where the agent model config serves both roles.

OpenAI-compatible provider errors use stable codes. Connection failures and
HTTP 429 responses may be retried because no provider usage is known. Malformed
success JSON, empty choices/content, invalid usage, timeouts after dispatch,
redirects, and HTTP 5xx responses are not transparently replayed because usage
may already have occurred. The failure summary exposes only the code, retry
decision, HTTP status, content type, response shape and request/correlation
IDs; prompts, answers, credentials and raw responses are excluded.

Each model revision may carry an optional `timeout_seconds` parameter
(1-600 seconds; default 30) alongside `max_output_tokens` and frozen pricing.
It bounds every provider call the agent and the evaluator make for runs that
use that revision; slow, high-context reasoning calls that exceed it fail as
`TIMEOUT` instead of hanging the attempt.

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

PowerShell operators can invoke the same container-native commands without the
POSIX wrapper scripts:

```powershell
docker compose exec api secrl-lite backup /data/backups/manual
docker compose exec api secrl-lite restore /data/backups/manual /data/restored-manual
```

Restore requires an empty destination and an exactly compatible Lite platform
and backup schema version. The copied staging directory is hash-verified again
immediately before its atomic rename, closing the verification/copy race.

## Disk and logs

Use `docker compose logs --tail=200 api runner` for operational diagnostics.
Never paste environment values or Authorization headers into an issue. Stop
the stack before moving the named `platform_data` volume. Incident volumes
are independent and can be cleaned only after the corresponding profile is
retired and a verified backup exists.
