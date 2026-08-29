# Personal Vault

Personal Vault is a self-hosted digital archive for privately owned media and documents. It is designed around data ownership, long-lived filesystems, replaceable supporting services, and a catalogue that remains under the Vault owner's control.

> **Project status:** active, early open-source development. Personal Vault is for technically capable developers and self-hosting experimenters. It is not yet a polished installer or finished consumer appliance; do not treat the current Compose configuration as a supported production recipe.

## Current capabilities

- Theatre for Movies and TV Shows, backed by a replaceable playback service
- Gallery, Home Videos, Music, Documents, Archives, Library, and People areas
- Vault Master ingestion, catalogue, routing, and metadata workflows
- Florence and gallery/people intelligence integration where configured
- Multi-user accounts, passkey-first authentication, and administrative Vault Control
- Local sharing foundations and shared collections

Email, Ledger, Vault Supplier, federation, recovery tooling, and installer work are not public-release capabilities. See [project status](docs/project-status.md) for the deliberately short current/planned split.

## Principles

- Permanent content remains ordinary files on storage the Vault owner controls.
- PostgreSQL holds application state, relationships, identities, and permissions; immutable user UUIDs—not names—are the authorization authority.
- `/vault` is a logical content namespace. Physical disks, mounts, and device identities stay behind that boundary.
- Supporting services such as Jellyfin and analysis models are replaceable integrations, not the authority for canonical Vault metadata or content.

## Architecture

```text
Browser
  ↓
Personal Vault frontend + FastAPI backend
  ↓
PostgreSQL catalogue/state + logical /vault content
  ↓
Optional playback and intelligence services
```

The backend is the authorization boundary. Read the public [architecture overview](docs/architecture.md) for the model and constraints.

## Repository layout

| Path | Purpose |
| --- | --- |
| `src/` | React/TanStack frontend |
| `backend/` | FastAPI application, PostgreSQL stores, and tests |
| `ai/`, `rampp/`, `people/`, `face_detector/` | Optional model-service containers |
| `public/` | Public application assets and fonts |
| `docs/` | Public project, development, and release documentation |
| `.github/` | Continuous-integration workflow |

## Getting started

Read [development setup](docs/development.md) before attempting to run the project. It documents the current host expectations, required manual configuration, database bootstrap behaviour, core versus optional services, and test commands.

Configuration is split deliberately:

- `.env` supplies Docker Compose interpolation values; start from [`.env.example`](.env.example).
- `config/auth.env` and `config/database.env` are runtime environment files for the backend/database override; start from the two templates in `config/` and keep the real files untracked.

## Development and testing

The frontend uses Bun (`bun install --frozen-lockfile`, then `bun run dev` or `bun run build`). Backend test instructions, including PostgreSQL-backed testing, are in [backend/TESTING.md](backend/TESTING.md). The public [configuration reference](docs/configuration.md) explains the variables used by the available development paths.

## Contributing and security

See [CONTRIBUTING.md](CONTRIBUTING.md) for the branch and pull-request model. Security issues must follow [SECURITY.md](SECURITY.md), not public issue discussion.

## Branch and release policy

`development` is the integration branch and `main` is kept releasable. The repository is owner-controlled: external contributors may propose pull requests but do not receive automatic merge or write authority. The remaining public-release gates are tracked in [the release checklist](docs/public-release-checklist.md).

The public version lives in [`VERSION`](VERSION). CI validates but never deploys,
promotes, or tags. See the [release workflow and version identity](docs/releases.md)
for exact-tag deployment, development-Vault separation, and rollback rules.

## Licence status

**Licence selection is pending before public release.** This repository remains private and the code is not yet offered for public reuse under an open-source licence.
