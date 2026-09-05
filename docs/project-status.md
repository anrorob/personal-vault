# Project status

## Implemented/current development

- Core archive experiences for Theatre, Gallery, Home Videos, Music, Documents, Archives, Library, and People
- Vault Master catalogue, ingestion, routing, and metadata foundations
- Passkey-first authentication, multi-user identity, and administrative controls
- Local sharing and shared-collection foundations
- Supporting playback and intelligence integrations

## Planned or exploratory

- A supported installer and recovery/operational tooling
- A ready-made Vault Supplier client or deployment, Email, and Ledger areas
- Federation beyond its current foundations
- Public release/versioning workflow and final licensing

No dates or compatibility promises are attached to these items. The project remains private until the publication gates in [the release checklist](public-release-checklist.md) are intentionally completed.

Vault Supplier supports PVPAIR1 credential issuance, installation pairing/revocation, signed identity verification, and a generic server-side resumable-transfer contract. Transfer finalization can retain the advisory `source_kind`, `source_id`, `source_label`, and `relative_path` evidence required by post-ingestion features such as the TV resolver; it is never ownership, routing, or filesystem authority. The public foundation does not include the private Windows Supplier application, a ready-made LAN listener, TLS material, firewall or Avahi configuration, deployment artifacts, or machine-specific state. See [pairing configuration](vault-supplier-setup.md) and [the transfer protocol](vault-supplier-transfer-protocol-v1.md).
