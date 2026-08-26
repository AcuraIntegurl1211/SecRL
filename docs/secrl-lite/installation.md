# SecRL Lite installation

SecRL Lite is a local-first benchmark operations console. The default Compose
publish binds only to `127.0.0.1`; it is not a public service.

## Prerequisites

- Docker Desktop or Docker Engine with Compose v2.
- Linux `amd64` or `arm64`, or Docker Desktop on macOS.
- At least 4 GB of memory for the platform image. Incident profiles require
  additional storage for their named MySQL volumes.

## First start

```sh
cp .env.example .env
openssl rand -hex 32                 # put the output in SECRL_MASTER_KEY
openssl rand -base64 32              # put the output in SECRL_SESSION_SECRET
openssl rand -hex 32                 # use for the reference Agent Service only
docker compose build
docker compose up -d
curl --fail http://127.0.0.1:8080/api/v1/health
```

Set `SECRL_INITIAL_ADMIN_PASSWORD` in `.env` before the first start. The
entrypoint applies Alembic migrations and creates the local `admin` user only
when that username does not exist. It never prints the password.

Open <http://127.0.0.1:8080> and sign in. The browser receives a strict,
HttpOnly session cookie and a CSRF token; model credentials are entered only
for a create request and are cleared from the form after submission. A newly
created administrator is restricted to the password-change endpoint until the
initial deployment password has been replaced with a new value of at least 12
characters.

## Local development auto-authentication

On a private development machine the login step can be skipped by starting
the API with both:

```sh
SECRL_DEV_AUTOAUTH=true
SECRL_DEV_AUTOAUTH_CONFIRM=yes
```

Every request is then served as the local `admin` account without a session
cookie or CSRF token, and the web console opens directly into the workspace.
The API refuses to start when the flag is set without the confirmation value,
and it fails closed if no active admin account exists. The initial-password
rotation gate still applies, so a fresh deployment completes its one-time
password change first. Never enable these variables in production, shared
hosts, or any deployment whose port is reachable beyond the machine itself;
the Compose files intentionally do not pass them through.

## macOS and Windows setup boundary

On macOS, run the same Compose commands from Terminal after installing Docker
Desktop. The repository scripts use POSIX `sh`, relative Compose paths and named
volumes; they do not require Linux host paths. This release was reviewed and its
non-Docker frontend/Python configuration was tested on macOS, but Docker was not
available on that host, so no macOS container smoke is claimed.

On Windows, use Docker Desktop with the WSL2 engine and run Compose from
PowerShell in the repository directory:

```powershell
Copy-Item .env.example .env
# Edit .env and set unique random values; do not paste them into shell history.
docker compose config --quiet
docker compose --profile smoke up -d --build --wait
Invoke-RestMethod http://127.0.0.1:8080/api/v1/health
docker compose --profile smoke down
```

Native Windows Python execution is not supported. Linux-only primitives such as
`fcntl` execute inside the Linux containers supplied by Docker Desktop/WSL2.
These Windows commands and paths were documentation/configuration reviewed;
there is no Windows host smoke evidence for this release.

## Optional profiles

Protocol-Smoke does not require MySQL:

```sh
docker compose --profile smoke up -d
```

Incident MySQL is opt-in and has no host port:

```sh
docker compose --profile incident_34 up -d
docker compose --profile incident_5 --profile incident_34 up -d
```

Each Incident has its own explicit Compose profile. The default
`docker compose up -d` starts no Incident database, and this release does not
provide an aggregate profile that can accidentally start all eight databases.
When a task selects multiple Incidents, pass exactly those profiles to Compose.
The preflight response reports unavailable Incidents and gives the corresponding
profile command when a selected service is not running.

Use a separate, non-checked-in value for `SECRL_MYSQL_ROOT_PASSWORD` and
`SECRL_MYSQL_PASSWORD` when an Incident profile is enabled.

To make SecRL Incident selection and preflight available to the API and
Runner, also set `SECRL_SECRL_RUNTIME_ENABLED=true` in `.env`. The API only
reports the credential as configured or missing; it never returns the value.

### Low-memory hosts

On a host with limited memory, run the platform services and only the Incident
needed by the current task. Start additional profiles one at a time when a
task explicitly selects them; do not start an all-Incident profile. The pinned
MySQL service has a 2 GiB container limit, so reserve at least 4 GiB for the
platform plus one Incident and verify health before queuing a run:

```sh
docker compose up -d --wait api runner web
docker compose --profile incident_34 up -d --wait incident-34
docker compose ps
```

If an Incident is not running, preflight returns `SECRL_ENV_UNAVAILABLE` with
the exact `--profile incident_*` command required to start it. This keeps
resource selection explicit and does not require Docker Socket access.

## Stop and upgrade

```sh
docker compose down
docker compose up -d
```

Named volumes preserve `/data` and Incident databases. Back up before an
upgrade; migrations run automatically on API startup. Windows is supported as
Docker Desktop documentation and operational guidance only in this milestone;
no Windows host smoke test is claimed.
