# Agent Service Protocol v1

An Agent Service is an HTTP process that returns structured `Action` objects.
It never executes benchmark tools itself. The platform owns the environment,
tool allowlist, budgets, observation truncation and artifact registration.

## Registration and health

Register a service from **Agents → Register agent** with an internal Compose
service URL, an `agent_revision_id`, and the SHA-256 of its manifest.

The API fetches `/v1/manifest` with redirects disabled, rejects public/global
addresses, validates every DNS result, and stores the manifest hash. A later
check repeats the same validation. The reference service is available under
the `agent-service-reference` Compose profile.

## Requests

- `GET /health` — liveness only;
- `GET /v1/manifest` — immutable protocol and revision metadata;
- `POST /v1/sessions` — create a run-scoped session;
- `POST /v1/sessions/{id}:act` — return exactly one structured action;
- `POST /v1/sessions/{id}:close` — close a session idempotently.

Every request carries a short-lived capability token scoped to run, agent
revision and allowed operation. Tokens are never sent to a public endpoint,
logged, persisted in SQLite JSON, or returned by the public API.

## Failure and retry behavior

Request IDs and sequence numbers make create/act/close idempotent. Ambiguous
timeouts are surfaced to the Runner as interrupted attempts; the Runner does
not blindly replay a request that may have reached the service. The service
must not mount Docker Socket or call Docker SDK.
