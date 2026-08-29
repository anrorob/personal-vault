# Public architecture overview

Personal Vault is a modular monolith: the user-facing frontend and FastAPI backend coordinate one canonical catalogue while optional playback and intelligence services remain adapters around it.

## Authorities and boundaries

- **Permanent content:** owned storage files remain the meaningful representation of originals. The application does not make a database blob store the sole copy of permanent content.
- **PostgreSQL:** stores identities, catalogue records, relationships, permissions, lifecycle state, and audit-relevant application state.
- **Identity:** immutable local user UUIDs are authoritative. Usernames, display names, and email addresses are labels, never authorization keys.
- **Storage:** applications use logical `/vault` areas. A canonical file has one recorded commissioned storage slot and relative path; physical devices and mount paths are not user-facing authority.
- **Authorization:** the backend enforces access to APIs, files, thumbnails, and streams. Frontend visibility is never an authorization mechanism.

## Content flow

Incoming material is staged before permanent publication. Vault Master is the catalogue, ingestion, routing, normalization, and publication authority. Post-publication intelligence may add descriptions, tags, OCR, people, and other evidence, but must not gate ingestion or silently reroute established content.

Jellyfin and model services such as Florence, RAM++, People, and face detection are supporting integrations. Their data or availability must not replace Vault-owned metadata, storage identity, or authorization.

## Growth model

The current architecture supports multi-user ownership, logical sharing, independently understandable storage slots, and future expansion without making a hostname, provider, physical disk, or administrator identity foundational.
