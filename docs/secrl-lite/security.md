# SecRL Lite security boundaries

## Secrets

Model API keys enter through a password field, travel to the authenticated API
over the local connection, and are encrypted by SecretStore before SQLite
persistence. The API exposes only `credential_configured` and never returns
the key. The Runner decrypts only during provider construction/call. Master
key, session secret, admin password and MySQL credentials come from the
runtime environment and are not copied into images, repository files or logs.

## Browser and API

Non-health routes require the local session cookie. Mutations require the
strict CSRF token returned by login. Cookies are HttpOnly and SameSite strict.
The frontend does not use localStorage, does not log request bodies, and
clears the model password field after submission. Gold, evaluator-private
inputs, capability tokens and database credentials are absent from Agent
payloads, public response schemas and public artifacts.

The deployment-created administrator must change its initial password before
other authenticated API routes are enabled. When an HTTPS reverse proxy fronts
the localhost-only Web service, it must set `X-Forwarded-Proto: https`; Nginx
preserves that value and the internal API trusts proxy headers only because the
API port is not published outside the private Compose network. The resulting
session cookie is marked `Secure`.

## Network

Agent Service registration and model provider validation enforce allowlists,
DNS checks for all resolved addresses and redirect refusal. Public/global
addresses are rejected for internal plaintext Agent Service endpoints. Compose
publishes Web only on `127.0.0.1`; API, Runner, MySQL and reference Agent
Service have no host ports. No service mounts `/var/run/docker.sock`.

## Integrity

TaskSpec, RunSpec, Benchmark, DatasetVersion and Agent manifests are hashed.
Artifacts are content-addressed and verified before registration and download.
Compare requires matching Benchmark and Dataset revisions. HumanReview is
append-only and auditable; it cannot overwrite automatic Attribution.

## Scope

This release intentionally excludes public mode, browser code upload, Redis,
PostgreSQL, multi-worker orchestration and runtime container lifecycle control.
