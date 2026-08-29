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

## Runtime authentication and database files

`config/auth.env` supplies `PV_ADMIN_USERNAME`, `PV_ADMIN_PASSWORD_HASH`, `PV_SESSION_SECRET`, `PV_WEBAUTHN_RP_ID`, `PV_WEBAUTHN_ORIGIN`, `JELLYFIN_URL`, and `JELLYFIN_API_KEY`.

`config/database.env` supplies `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`. The Compose backend uses `pv-database:5432` by default; override `POSTGRES_HOST` and `POSTGRES_PORT` only for an intentionally separate setup.

## Application paths and services

The example Compose override sets media paths for Theatre, Gallery, Home Videos, Documents, Archives, Music, Library, Arrival Hall, and Quarantine. The backend also supports path, worker cadence, upload-limit, intelligence-service URL, and controlled-executor settings used by its current production-shaped stack.

Those operational settings are not a supported public deployment contract yet. Consult the code and Compose files before changing them; do not expose executor signing keys or bind a development instance to production storage.
