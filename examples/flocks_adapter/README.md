# Flocks <-> SecRL Lite Agent Service adapter

A self-contained FastAPI service that bridges the SecRL Lite **Agent Service
Protocol v1** (what the platform's runner calls) onto **Flocks' native session
API**. It is an example integration, not part of the platform package: the
platform never imports it, and it only depends on the frozen protocol models in
`secrl_platform.agents.service` / `secrl_platform.benchmarks.protocol`.

```
SecRL runner ──Agent Service v1──▶ this adapter ──Flocks native──▶ Flocks
   /v1/manifest                      POST /api/session
   POST /v1/sessions                 POST /api/session/{id}/prompt_async
   POST /v1/sessions/{id}:act        GET  /api/session/{id}/status   (poll -> idle)
   POST /v1/sessions/{id}:close      GET  /api/session/{id}/message  (last assistant)
```

## What one turn looks like

1. The platform sends an `ActRequest` with an `Observation`.
2. The adapter renders the observation as text (`episode_start` becomes the
   incident context + question; `tool_result` becomes a JSON dump) and posts it
   to Flocks with the **dedicated** `FLOCKS_EVAL_AGENT` and the **explicit**
   `MODEL_PROVIDER_ID`/`MODEL_ID`.
3. It polls `/status` until idle, accepting both shipped Flocks shapes (`{"status": "idle"}` and `{"isProcessing": false}`), then reads the last assistant message.
4. The message must match the strict grammar:
   - `SQL: <one read-only statement>` -> `tool_call` on the episode's query tool
   - `SUBMIT: <answer>` -> `submit`
   The whole message must be exactly that one line. On a mismatch the adapter
   posts **one** correction prompt into the same Flocks session and retries; a
   second mismatch returns HTTP 422, which the platform maps to
   `INVALID_ACTION` / `AGENT_RUNTIME_ERROR`.
5. `request_id` and `sequence` are echoed verbatim; `usage` carries whatever
   token stats Flocks attached to the message (or zeros, see below).

## Design decisions (already settled -- do not re-litigate in review)

1. **Turn purity.** A dedicated Flocks agent (`FLOCKS_EVAL_AGENT`, default
   `secl-eval`) whose system prompt forbids tool use and demands one action
   line. The adapter never asks Flocks to run tools; all environment access
   stays with the SecRL adapter.
2. **Action grammar.** Strict two-form, full-match regex, one correction retry.
3. **Usage honesty.** If Flocks exposes per-message token counts, they are
   reported in `ActResponse.usage`. If not, the adapter reports zeros and the
   manifest version is suffixed `+no-usage`. No estimation is ever injected.
   Toggle with `FLOCKS_USAGE_REPORTED=false` once confirmed with the Flocks
   side.
4. **Model pinning.** `MODEL_PROVIDER_ID`/`MODEL_ID` are sent on every request
   (no Flocks-side default) and embedded in the manifest version as
   `flocks-hunter-v1+<provider>/<model>`. Changing the model changes the
   manifest SHA-256, which the platform treats as a **new agent revision**.

## Security properties

- The platform's capability token is checked for **presence only** (a non-empty
  `Bearer` value). It is never verified against the platform signer and never
  forwarded to Flocks. Flocks gets its own `FLOCKS_API_TOKEN`, injected via the
  environment; no secret is hardcoded or logged.
- The manifest is static per configuration, so its SHA-256 is stable and can be
  pinned at registration.
- Flocks calls use `follow_redirects=False`; any redirect or non-2xx is a hard
  upstream error (502 to the platform), never a silent retry.

## Run it

From the **repository root** (the adapter imports `secrl_platform`), with the
environment loaded from a local, git-ignored `.env`:

```bash
cp examples/flocks_adapter/.env.example examples/flocks_adapter/.env
# edit examples/flocks_adapter/.env with real values, then:
set -a; source examples/flocks_adapter/.env; set +a
python -m examples.flocks_adapter.app
```

The startup line prints the manifest SHA-256 you must register:

```
flocks adapter manifest sha256 = <64 hex>
```

The adapter binds `127.0.0.1:$ADAPTER_PORT` by default. To make it reachable
from the SecRL **runner container**, publish it on a network the runner can
resolve -- e.g. run it as a sidecar on the Ubuntu host and reach it via the
Docker bridge gateway IP, or add a compose service. It must be plain
`http://<host>:<port>` with no path/query, because Agent Service v1 endpoints
are pinned-internal-HTTP only.

## Register with the platform (manual checklist -- no production commands here)

1. **Allowlist the host.** Add the adapter's hostname to
   `SECRL_AGENT_SERVICE_ALLOWLIST` in `compose.override.yaml` for **both** the
   `api` and `runner` services (the env is not auto-passed through
   `x-platform-env`), then recreate those services.
2. **Register the revision.** In the web UI: Agents -> New -> **SERVICE**, with
   - `revision_id`: `flocks-hunter-v1`
   - `endpoint`: `http://<adapter-host>:<port>`
   - `manifest_sha256`: the value printed by the adapter (must match the
     `GET /v1/manifest` hash computed as
     `sha256(json.dumps(manifest, ensure_ascii=False, separators=(",",":"), sort_keys=True))`)
3. **`:check`** the agent. The platform fetches `/v1/manifest`, validates the
   five `ServiceManifest` fields, and compares the SHA-256 to the registration.
   A mismatch is a `PROTOCOL_MISMATCH` -- re-check the model/provider env.
4. **Single-case smoke.** Queue one CASES task (one incident case,
   `max_steps` 15-32) against the new revision. Watch the run to
   SUCCEEDED/FAILED and inspect the trajectory artifact for the action grammar.
   Real LLM calls require separate authorization and a declared cost cap.

## Two open points to confirm with the Flocks side before live use

1. **Tool bypass.** Confirm `FLOCKS_EVAL_AGENT` is configured Flocks-side to
   refuse all tool execution (decision 1). Verification: send a prompt that
   tempts a tool call and assert the `/message` reply is still a single
   `SQL:`/`SUBMIT:` line and that no Flocks tool span appears in the session.
2. **Usage exposure.** Confirm whether `/api/session/{id}/message` carries
   per-message token stats in the shape this adapter reads
   (`tokens.input`/`tokens.output`/...). Verification: run one turn and check
   `ActResponse.usage` is non-zero; if Flocks does not expose them, set
   `FLOCKS_USAGE_REPORTED=false` so the manifest advertises `+no-usage` rather
   than silently reporting zeros.

### Both points verified on a live deployment (2026-09-12)

- **Usage: PASS.** `info.tokens.{input,output,reasoning,cache}` is present on
  every assistant message and the adapter forwards it as `ActResponse.usage`.
- **Tool bypass: PASS with the dedicated `secl-eval` agent, FAIL with rex.**
  The primary `rex` agent silently expands an empty `tools: []` into *all*
  builtin tools (Flocks `resolve_agent_initial_tools` special-cases rex), so
  it will run real `bash` tool calls when tempted — do not use it. The fix is
  a storage-based custom agent created via Flocks' own API:

```sh
curl -s -X POST -H "Authorization: Bearer $FLOCKS_API_TOKEN" \
  -H "Content-Type: application/json" "$FLOCKS_BASE_URL/api/agent" -d '{
  "name": "secl-eval",
  "prompt": "You are a read-only evaluation responder ... (see examples/flocks_adapter for the full prompt)",
  "mode": "primary",
  "temperature": 0,
  "delegatable": false,
  "tools": [],
  "skills": [],
  "permission": [{"permission": "*", "pattern": "*", "action": "deny"}]
}'
```

  A non-`rex` custom agent with empty `tools` resolves to zero loaded tool
  schemas at session time, and the `*: deny` permission ruleset is a second,
  independent gate. Re-verified after creation: a tool-tempting prompt
  produced grammar-clean `SQL:`/`SUBMIT:` replies across a three-turn session
  with **zero tool spans** in the Flocks session record.

## Tests

Offline only -- a fake Flocks ASGI app is injected via `httpx` transport, so no
real Flocks or LLM traffic occurs:

```bash
python -m pytest tests/platform/test_flocks_adapter.py -q
```
