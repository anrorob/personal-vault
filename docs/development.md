# Development setup

Personal Vault currently exposes application source and a production-shaped Compose stack; it is not a one-command installer. Use an isolated development host, database, credentials, storage tree, and external-service accounts. Never connect a development environment to production data, mounts, session secrets, or API keys.

## Host expectations

- The full Compose stack is Linux-host oriented: it uses Linux bind mounts, optional `/dev/dri` access, and external Docker networks.
- Docker Engine with the Compose plugin is required for the container stack.
- Bun is used by the frontend Docker build and lockfile. Python 3.13 is used by the backend Docker image and CI.
- PostgreSQL 17 is used by the Compose stack and CI.
- Native Windows backend tests are supported as documented in [backend/TESTING.md](../backend/TESTING.md); that does not make the full Compose stack a Windows deployment path.

## Configuration

1. Copy `.env.example` to `.env` for Compose interpolation.
2. Copy `config/auth.env.example` to `config/auth.env` and `config/database.env.example` to `config/database.env`.
3. Replace every placeholder. Passkeys require a real HTTPS hostname: `PV_WEBAUTHN_RP_ID` and the hostname in `PV_WEBAUTHN_ORIGIN` must match exactly.
4. Copy `docker-compose.override.yml.example` to the ignored `docker-compose.override.yml` and adjust the logical Vault-root mapping for an isolated development tree.

Set `PV_ENVIRONMENT=development` and a non-production `PV_COMMIT` marker in
the root `.env`. The release version is read from [`VERSION`](../VERSION), not
from an environment variable. See [releases](releases.md) before configuring a
development Vault or any deployment target.

The checked-in override expects external `pv-public` and `jellyfin_default` Docker networks. Create/use isolated equivalents deliberately, or adapt a local override; the repository does not yet supply a portable network/bootstrap installer.

## Core application requirements

The backend requires PostgreSQL plus the runtime values in `config/auth.env` and `config/database.env`. On backend startup, its controlled bootstrap creates or updates the additive PostgreSQL schema; there is currently no separate public migration-command interface. Use a new development database when testing schema changes.

For frontend-only work:

```bash
bun install --frozen-lockfile
bun run dev
```

For a direct backend process, install `backend/requirements-dev.txt`, load the runtime environment files into the process, then run `uvicorn app.main:app --reload` from `backend/`. This still requires reachable PostgreSQL and the required configuration values.

## Optional/full-stack services

The Compose stack defines services for Jellyfin, Florence OCR, RAM++, People, and face detection. They enable playback/import and intelligence paths but are not a promise that every feature works without manual model acquisition, service configuration, and development-only API keys.

Jellyfin is required by the current backend configuration and should use a separate development service and API key. Model containers expect model files beneath the configured ignored `models/` roots. Storage-executor queues, signing-key mounts, and production-style service networking are not a supported public installer path yet.

## Tests and checks

Run backend tests as documented in [backend/TESTING.md](../backend/TESTING.md). PostgreSQL-backed tests require an explicitly configured disposable `PV_TEST_DATABASE_URL`; never use production PostgreSQL.

For the frontend:

```bash
bun run lint
bun run build
```

The GitHub Actions workflow runs backend tests with a disposable PostgreSQL service, plus frontend lint and build checks, on feature, development, and main integration paths. It does not deploy or promote any environment.
