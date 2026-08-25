# SecRL Lite v0.1.1

SecRL Lite v0.1.1 is a compatibility and operations release for the Lite
runtime. It hardens OpenAI-compatible provider handling, makes benchmark scope
selection explicit, packages the built-in Agent runtime assets, and keeps
low-memory deployments from starting Incident databases unintentionally.

This release does not publish public Docker images. Operators build the audited
source revision locally with Docker Compose.

## Highlights

### DeepSeek/OpenAI-compatible providers and ReAct

- DeepSeek and OpenAI-compatible endpoints accept the platform-recognized URL
  forms while retaining SSRF validation, DNS pinning, redirect restrictions,
  and SecretStore boundaries.
- ReAct runtime compatibility was fixed for provider responses that contain
  `reasoning_content`, normal `content`, or both.
- Provider parsing now safely handles malformed JSON, empty `choices`, empty
  content, invalid or unusual `usage`, and unexpected `finish_reason` values.
- Errors expose stable, actionable codes instead of collapsing to `FAILED`.
  The failure summary contains only safe response structure, HTTP status,
  content type, and request/correlation identifiers; it never records API keys,
  prompts, benchmark questions, model answers, or raw provider responses.
- A request ID is persisted when a provider supplies one. If no request ID is
  supplied, the stored value remains empty.
- Only failures known to occur before dispatch, such as connection failures or
  HTTP 429 responses with no known provider usage, may be retried. Malformed
  success responses, empty response fields, timeouts after dispatch, HTTP 5xx
  responses, and other ambiguous outcomes are not transparently replayed
  because the provider may already have charged the request.

### Built-in Agent wheel assets

Python wheels now include the built-in Agent runtime assets required after
installation, including:

- `secgym/agents/react_examples/*.txt`
- `secgym/agents/expel_train/*.json`
- `secgym/agents/expel_train/*.jsonl`

The wheel verification path imports the Agent from the installed wheel rather
than only from the source tree.

### Explicit benchmark scope

Task creation now has one explicit scope mode:

- `CASES`: accepts only `case_ids`.
- `INCIDENTS`: accepts only `incident_ids` and expands each selected Incident
  into its complete Case list.
- `ALL_BENCHMARK`: selects the full frozen 589-Case Benchmark.

`case_ids` and `incident_ids` may not be combined. Mixed scope fields return
`AMBIGUOUS_SCOPE`; the API never silently takes their union. The UI clears
fields that are incompatible with the newly selected mode and displays the
frozen Case and Incident counts before queueing.

The backend freezes the resolved Case IDs, Incident IDs, Dataset revision,
Dataset SHA-256, and RunSpec. A single Case selection freezes exactly one Case;
the `incident_5` selection freezes 98 Cases; `ALL_BENCHMARK` freezes 589 Cases.
Duplicate Cases are rejected during resolution.

### Budget and completion semantics

The budget state machine distinguishes incomplete work from exhausted limits.
When every Case frozen in the RunSpec has completed, the final task state is
`SUCCEEDED`, even when `max_cases`, `max_tokens`, or `max_cost` was reached by
the completed work. `BUDGET_EXHAUSTED` is used only when at least one frozen
Case remains unprocessed and the applicable budget is exhausted. Reservation
settlement and completion-first precedence are covered for the max-cases,
max-tokens, and max-cost combinations.

### Compose Incident profiles and low-memory operation

The default Compose deployment starts no Incident MySQL service. Each Incident
has an explicit profile, so operators start only the selected services, for
example:

```sh
docker compose up -d --wait api runner web
docker compose --profile incident_5 up -d --wait incident-5
```

For multiple selected Incidents, pass exactly the corresponding profiles:

```sh
docker compose --profile incident_5 --profile incident_34 up -d --wait
```

There is no aggregate profile that starts all eight Incident databases. When a
selected Incident is not running, preflight reports it as unavailable and
provides the required profile command. The platform does not use Docker Socket
access and cannot control the host Docker daemon at runtime.

On a low-memory host, start the platform and one required Incident first, wait
for health, and add other profiles only when the task explicitly needs them.
Do not start all Incident profiles together. Incident services use independent
named volumes; preserve a verified backup before retiring a profile or its
volume.

## Backward compatibility

Runs created by v0.1.0 may have RunSpecs without `scope_mode` or frozen scope
counts. They remain readable, recoverable, and displayable. The API and UI
project the legacy scope for presentation without rewriting the historical
RunSpec, its hash, or its persisted task data. New tasks always use the
explicit scope model and freeze the resolved selection before queueing.

## Platform boundaries

- Ubuntu uses Docker Engine and Compose v2. Incident profiles are explicit and
  should be started only for selected Incidents.
- macOS uses Docker Desktop and the same Compose commands from Terminal. This
  release has configuration and frontend validation on macOS, but no macOS host
  container smoke claim.
- Windows uses Docker Desktop with the WSL2 engine and Compose from PowerShell.
  Native Windows Python execution is outside Lite scope; Linux-only runtime
  primitives execute inside the containers. Windows validation is documentation
  and configuration validation, not a Windows host smoke claim.
- The default Web publish remains bound to `127.0.0.1`. The platform is not a
  public Internet deployment and no component receives Docker Socket access.

## Clash Verge Fake-IP note

If Clash Verge is enabled with TUN or Fake-IP mode, provider DNS resolution may
return a synthetic address or route traffic through a proxy policy. This can
make a provider preflight fail even when the endpoint is valid. For a provider
preflight or paid task, use a network path where `api.deepseek.com` (or the
configured provider hostname) resolves to its normal public address and verify
the result from both the host and the relevant container. Do not add a
hard-coded `extra_hosts` entry or provider IP, and do not weaken DNS pinning,
SSRF, redirect, or SecretStore checks to work around Fake-IP behavior.

## Upgrade

Back up the platform data and selected Incident volumes before upgrading. Keep
the previous image or source revision and its matching verified backup until
post-upgrade health and artifact checks have completed.

```sh
./scripts/lite-backup.sh ./backups/pre-v0.1.1
docker compose build
docker compose up -d --wait api runner web
curl --fail http://127.0.0.1:8080/api/v1/health
```

Start only the Incident profiles required by the selected tasks, then run the
preflight checks before queueing work. Alembic migrations run at API startup;
do not copy or edit the live SQLite volume in place.

## Rollback

Stop the v0.1.1 stack, select the previous v0.1.0 image or source revision,
and restore its matching verified backup into an empty target. Do not roll back
by mutating the live volume in place. Re-run health and preflight checks before
resuming work, and preserve the v0.1.1 backup for later investigation.

```sh
docker compose down
# Select the previous v0.1.0 image or source revision.
docker compose up -d --wait api runner web
./scripts/lite-restore.sh ./backups/pre-v0.1.0 ./restored-data
```

The restore target must be empty and the backup schema/platform version must be
exactly compatible with the selected runtime.

## Security and distribution

- Secrets are displayed only as configured or missing; plaintext values are
  never returned by the API or rendered in the UI.
- Agent Services return structured Actions only. Benchmark tools are executed
  by the platform.
- No API keys, databases, caches, experiment results, trajectories, raw
  provider responses, or no-truncation output belong in this release notes PR.
- CI does not publish public Docker images. Build images locally from the
  reviewed source revision.
