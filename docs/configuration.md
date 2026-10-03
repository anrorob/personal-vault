# Configuration reference

Real configuration is intentionally untracked. Start with `.env.example`, `config/auth.env.example`, and `config/database.env.example`; do not place secrets in source files or issue reports.

## Compose interpolation (`.env`)

| Variable | Purpose |
| --- | --- |
| `PV_VAULT_ROOT` | Host root mounted as the logical Vault content tree by the example override. |
| `PV_STORAGE_SLOT_ROOT` | Host root mounted for managed storage-slot resolver paths. |
| `PV_*_MODEL_ROOT` | Host model roots for Florence, RAM++, People, and face detection containers. |
| `PV_FLORENCE_DEVICE`, `PV_RENDER_GID` | Optional Florence hardware settings used by Compose. |
| `PV_MUSICBRAINZ_USER_AGENT` | Identifies optional MusicBrainz requests. |
| `PV_STORAGE_SLOT_ROOTS_JSON` | Optional explicit resolver-root map for managed slots. |
| `PV_ENVIRONMENT` | Build/deployment identity: `development`, `test`, or `production`. |
| `PV_COMMIT` | Build/deployment commit: an immutable SHA in production. |

The release version comes only from the repository-root `VERSION` file; do not
set it through environment configuration.

## Runtime authentication and database files

`config/auth.env` supplies `PV_ADMIN_USERNAME`, `PV_ADMIN_PASSWORD_HASH`, `PV_SESSION_SECRET`, `PV_WEBAUTHN_RP_ID`, `PV_WEBAUTHN_ORIGIN`, `JELLYFIN_URL`, and `JELLYFIN_API_KEY`.

`config/database.env` supplies `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`. The Compose backend uses `pv-database:5432` by default; override `POSTGRES_HOST` and `POSTGRES_PORT` only for an intentionally separate setup.

## Application paths and services

The example Compose override sets media paths for Theatre, Gallery, Home Videos, Documents, Archives, Music, Library, Arrival Hall, and Quarantine. The backend also supports path, worker cadence, upload-limit, intelligence-service URL, and controlled-executor settings used by its current production-shaped stack.

Those operational settings are not a supported public deployment contract yet. Consult the code and Compose files before changing them; do not expose executor signing keys or bind a development instance to production storage.

## Source admission, Supplier LAN, and Jellyfin

| Setting | Required when | Contract |
| --- | --- | --- |
| `PV_REPOSITORY` | Reporting an identified build or using admitted workers | Exact deployed `owner/repository`; Compose defaults to public upstream. Forks must override it. |
| `PV_ALLOWED_SOURCE_REPOSITORIES` | KEN or section mover admission | Explicit case-sensitive exact identities; missing/invalid configuration denies workers. Supply it independently to each worker runtime. |
| `PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS` | Receiving Supplier payloads | One or more comma-separated exact ASCII hostnames; case-insensitive, no URL/port/wildcard. Missing/invalid configuration denies payload routes. Also configure general `PV_ALLOWED_HOSTS`, restricted LAN proxy, and TLS. |
| `PV_JELLYFIN_MEDIA_PATH_MAP_JSON` | PV and Jellyfin use different media path namespaces | Optional JSON mapping; absent preserves literal paths, present must validate fully. |

See [Supplier listener authorization](supplier-lan-configuration.md), [runtime source admission](runtime-source-identity.md), and [Jellyfin mapping](jellyfin-media-paths.md).

Optional KEN needs `PV_KEN_ENABLED=true`, `PV_KEN_URL`, an explicitly admitted source identity, and a writable isolated `PV_KEN_WORK_ROOT` in production. Models belong in read-only application mounts, never catalogue content. Section mover admission also uses the source allowlist and `PV_SECTION_MOVE_ENABLED=true` in production. These workers are not enabled or deployed by the base Compose example.

Cache paths (`PV_MUSIC_PLAYBACK_CACHE_PATH` and thumbnail caches) are writable derivative storage, separate from canonical media. Budget and mount them accordingly. Recoverable Delete retains canonical bytes; no age-based automatic purge is introduced. Permanent deletion is a separate elevated administrative action.
