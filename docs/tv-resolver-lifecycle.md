# TV resolver publication lifecycle

Episode approval and extras publication are separate owner-scoped actions.
After episodes publish, approval disappears. The extras action queues only
tracks already classified `likely_extra`, using their reviewed season and
original basename in `Theatre/TV Shows/<show>/Season NN/Extras/`.

The immutable owner, approved audience, checksum and existing show/season
must agree. Unsafe names and duplicate catalogue destinations are rejected.
The existing signed managed executor checks actual source existence, size and
SHA-256, chooses managed storage, and prevents destination overwrites. No
generic Home Videos mover may publish resolver tracks.

Root receipts create canonical assets, files and placements atomically. Extras
have a `vault_tv_extras` relationship to their season and `tv_extra` metadata
with `approved_tv_resolver` provenance, included in normal owned sidecars.
They never acquire episode numbers. Each completed publication phase requests
one bounded Jellyfin handoff; a handoff failure does not reverse publication.

Rejected signed requests are associated with the current attempt ID, so old
rejections cannot fail a retry. Failed-only retry preserves successful files
and catalogue identities. A batch becomes `complete` only when every track is
published and none is unresolved. Complete batches leave the active queue,
remain retrievable by their owner, and remain protected from rediscovery.

## Contract audit and compatibility

Audited resolver discovery/list/detail/approval/retry, worker claim, rescan,
direct mover guard, managed request/receipt/failure handling, catalogue,
placements, sidecars, TV show/season/episode APIs and metadata import, and
Arrival Hall action/count rendering. Former episode-only authority and
published-as-terminal assumptions now distinguish extra publication and batch
completion. Episode APIs, numbering and metadata import intentionally remain
episode-only; no extras browsing or playback UI is introduced here.

Durable TV writes remain PostgreSQL-only. Memory implementations retain request
validation and attempt metadata but intentionally cannot fabricate TV review
or canonical TV receipts. Local PostgreSQL tests exercise the production model;
physical executor tests use synthetic files and temporary slots only.

Schema bootstrap adds the extra relationship and extends batch status with
`complete`, preserving existing records. Deployment does not approve or move
extras. Existing staged tracks require the user's explicit Publish extras action.
