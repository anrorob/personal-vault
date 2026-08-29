# Personal Vault

Personal Vault is a self-hosted archive for privately owned media and documents. It combines a React front end, a FastAPI service, PostgreSQL, and optional media and machine-learning services.

This repository is the canonical development source. It is an early public-development baseline: it contains the application and generic configuration, not a production deployment, private archive data, or operational credentials.

## What is here

- Gallery, theatre, music, personal video, document, and archive experiences
- Vault Master catalogue and metadata workflows
- Authentication, passkeys, sharing, federation, and administration code
- Docker Compose service definitions and automated tests

## Development status

The project is not yet an end-user installer or a supported public service. Set up a separate development environment with fresh credentials, storage, database, and media data. Never point a development instance at a production database, session store, storage mount, or API key.

Copy `.env.example` to `.env`, create the ignored `config/auth.env` and `config/database.env` files required by the Compose override, and supply your own values. Model and storage paths default to local ignored directories and may be overridden with environment variables. The example override uses `PV_VAULT_ROOT` rather than a host-specific media path.

See [CONTRIBUTING.md](CONTRIBUTING.md), [SECURITY.md](SECURITY.md), and [docs/development-environment-plan.md](docs/development-environment-plan.md). No licence has been selected for this repository yet; do not redistribute it until one is added.
