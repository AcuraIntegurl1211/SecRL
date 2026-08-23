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
for a create request and are cleared from the form after submission.

## Optional profiles

Protocol-Smoke does not require MySQL:

```sh
docker compose --profile smoke up -d
```

Incident MySQL is opt-in and has no host port:

```sh
docker compose --profile incident_34 up -d
docker compose --profile secrl-all up -d
```

Use a separate, non-checked-in value for `SECRL_MYSQL_ROOT_PASSWORD` and
`SECRL_MYSQL_PASSWORD` when an Incident profile is enabled.

## Stop and upgrade

```sh
docker compose down
docker compose up -d
```

Named volumes preserve `/data` and Incident databases. Back up before an
upgrade; migrations run automatically on API startup. Windows is supported as
Docker Desktop documentation and operational guidance only in this milestone;
no Windows host smoke test is claimed.
