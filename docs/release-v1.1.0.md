# Personal Vault v1.1.0

## Highlights

- Theatre: Original, Auto, FHD and 720p quality modes; quality-first negotiation, more responsive seeking and buffering; persistent watched/progress state; Continue Series and Play Next Episode using episode identity.
- Movies: search, responsive browsing, manual franchises with Release/Timeline ordering, rotating artwork and grouped top-level presentation.
- Gallery: paginated browsing, restored navigation position, Year/Month date rail, and recoverable Delete/Restore.
- Arrival Hall: Vault-wide existing-asset recovery search, restoration of deleted assets, and safer recheck/retry/cancellation of unfinished intake.
- Responsive layouts across Gallery, Movies, Music and Home Videos.

## Upgrade notes

Read [the upgrade guide](upgrade-v1.1.0.md) before upgrading. Normal backend startup bootstraps the schema, including UUID-keyed movie progress, franchise tables and recoverable lifecycle/history. Back up first, stop writers, coordinate host publisher updates, then bring up the matching backend and frontend. Restoring an older version requires matching database/filesystem backups.

Supplier payload listeners now require explicit `PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS`. Optional KEN and section mover admission require the deployed `PV_REPOSITORY` in `PV_ALLOWED_SOURCE_REPOSITORIES`. Both allowlists match exact configured values and fail closed when missing or invalid.

`PV_JELLYFIN_MEDIA_PATH_MAP_JSON` supports different PV/Jellyfin media namespaces. It remains optional for identical paths: an absent key preserves literal lookup and does not guess translations. Invalid configured maps fail closed; existing explicit mappings retain their behavior.

## Other improvements

- Supplier recovery/provenance and safe filename remediation contracts.
- Music grouping, playback caching/queue behavior, and Home Video metadata, privacy, thumbnails and playback controls.
- Optional KEN analysis with shared immutable model assets, bound results, corrections and title/style workflows.
- Catalogue authorization, storage placement, signed publication coordination and source privacy hardening.

This release retains the TV disc resolver, extras lifecycle and self-describing Supplier pairing already present on public main. Those features are not new in this synchronization.
