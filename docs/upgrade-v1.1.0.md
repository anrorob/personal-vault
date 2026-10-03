# Upgrading to v1.1.0

Back up PostgreSQL and canonical media together before upgrading. Stop intake publishers and section movers, drain or preserve signed queued work, and stop the old backend before starting the new backend. Upgrade host executors and their coordination modules as a set before resuming publication. Deploy the matching frontend after the backend schema bootstrap succeeds. Do not run mixed old/new workers against shared queues.

## Startup schema bootstrap

The backend initializes schema at normal startup using the existing PostgreSQL stores. It adds missing tables, columns, indexes, and constraints; there is no separate migration command required for a normal public upgrade. It fails startup when an identity cannot be safely reconciled.

- Movie progress is keyed by immutable `(user_id, movie_id)` instead of `(username, movie_id)`. Existing usernames are reconciled to account UUIDs. Unknown accounts stop migration; repair account ownership from your own authoritative records rather than inventing it.
- Episode progress has separate immutable episode identity and watched state.
- Manual movie franchises add per-user franchise/member tables, release/timeline ordering, and metadata-only grouping. They do not relocate movie files.
- Recoverable Delete/Restore adds catalogue lifecycle/history state. Deleted content is excluded from normal listings and retains bytes for owner-authorized restoration. Restore does not reinstate sharing. Permanent deletion remains a separate administrative workflow.
- Gallery navigation/tag/date indexes and metadata, Home Video controls, Music grouping, intake recovery authorization, and optional KEN result binding are initialized by their owning stores. Bootstrap is designed to be repeatable.

## Configuration and independent host executors

Configure Supplier listener host authorization before enabling transfer routes. Set the deployed `PV_REPOSITORY` and explicitly authorize it with `PV_ALLOWED_SOURCE_REPOSITORIES` for the backend and any section mover using worker admission. Existing public pairing credentials remain separate from these authorization settings.

Jellyfin mapping is optional: keep `PV_JELLYFIN_MEDIA_PATH_MAP_JSON` absent for identical paths. Different namespaces require explicit mappings. Empty/invalid configured maps fail closed. Existing explicit maps preserve their behavior. See [configuration](configuration.md).

The generic signed host publisher in `ops/storage/arrival-managed-publisher.py` needs the matching application coordination modules importable via `PYTHONPATH` pointing to `backend`, the same protected HMAC key and queues used by the backend, and its commissioned active-slot manifest. Set host `PV_STORAGE_SLOT_ROOT` to the exact commissioned resolver-root parent; its generic default is `/var/lib/personal-vault-storage/slots`. Preserve restrictive key/queue permissions. Install `backend/app/arrival_publication_coordination.py` and `backend/app/arrival_publisher_loop.py` from the same release with it. Coordinate publication shutdown with backend shutdown; do not copy example roots over existing operator configuration.

## Rollback

An older application is not a safe rollback against upgraded data: it may not understand new lifecycle states, UUID progress keys, or signed routing/coordination contracts. Stop all writers and restore the matching pre-upgrade database, application/executor artifacts, configuration, and filesystem state from your backup. Do not delete new catalogue rows or reverse constraints manually. Retaining extra environment settings alone does not reverse schema changes.

Historical operator-only TV relocation recovery envelopes are not supported by the current executor. Preserve any such exceptional queued work for explicit operator review before upgrading; do not replay it as a normal intake request. This does not remove disc resolver approval, extras publication, retries or owner-scoped projection recovery.
