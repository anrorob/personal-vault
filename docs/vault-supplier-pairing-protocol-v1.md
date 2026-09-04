# Vault Supplier pairing credential v1

This document defines the PVPAIR1 bootstrap contract for Vault Supplier. The Supplier client must select its management
origin from the credential before contacting a Vault.

## Credential

The user copies one value:

```text
PVPAIR1.<base64url-without-padding(UTF-8-JSON)>
```

The prefix is case-sensitive. The JSON contains exactly these six fields,
serialized in this order with compact separators, without a BOM or whitespace:

```json
{
  "v": 1,
  "vault_id": "<canonical-lowercase-hyphenated-UUID>",
  "origin": "https://vault.example.net",
  "server_key_id": "<64-lowercase-hex-SHA256-of-SPKI-DER>",
  "server_public_key_spki_der_base64": "<standard-padded-Base64-SPKI-DER>",
  "pairing_secret": "<43-character-unpadded-Base64URL-secret>"
}
```

`v` is the JSON integer 1. The public key is ECDSA P-256 SPKI DER; the key ID
is SHA-256 of those exact DER bytes. The credential includes no user identity,
username, private key, session/authentication token, LAN address, or LAN port.
Field order is deterministic for emission; consumers must parse JSON rather
than depend on field order. Current credentials are approximately 600–800
characters depending on origin length. No encryption or signature is added to
the descriptor: it is a short-lived bearer bootstrap value delivered by the
authenticated Vault UI. Keep the complete credential private.

## Authoritative sources

- Vault UUID: the unique `vaults` row where `is_local=TRUE`.
- Authorized user UUID: the authenticated account, bound only in PostgreSQL.
- Management origin: existing `PV_WEBAUTHN_ORIGIN`, validated against the
  established `PV_WEBAUTHN_RP_ID`. No request Host or Origin header supplies it.
- Server identity: the existing persistent
  `PV_VAULT_SUPPLIER_SERVER_IDENTITY_KEY_PATH` key. Issuance never generates or
  rotates it. SPKI, private-key public component, and key hash must agree.

The configured origin must use HTTPS, contain a valid ASCII hostname and
optional valid port, and have no path except an optional root `/`. Embedded
credentials (including empty userinfo), query/fragment delimiters (even empty),
whitespace/control characters, backslashes, invalid ports, mismatched RP IDs,
and ambiguous hostname forms are rejected. Emission lowercases the hostname,
omits port 443, preserves other valid ports, and omits the trailing root slash.
Dev uses its configured Dev origin; reusable implementation contains no Dev or
production hostname default. Unsafe/missing configuration fails closed before
replacing an existing credential.

## Issue

Authenticated `POST /api/vault-supplier/pairing-code` retains its route name,
but now returns **only a full credential**, never a standalone short code:

```json
{
  "pairing_credential": "PVPAIR1.<payload>",
  "expires_at": "<UTC-timestamp>",
  "protocol_version": 1
}
```

No request descriptor or user UUID is accepted as authority. Responses use
`Cache-Control: no-store`. Security offers Generate/Replace pairing credential,
one selectable value, and Copy pairing credential. It clears expired values.

The secret is `secrets.token_urlsafe(32)`: 32 cryptographically random bytes
(256 bits), represented as 43 unpadded Base64URL characters. PostgreSQL stores
only its lowercase SHA-256 hash. Neither the plaintext secret nor the full
credential is persisted or included in audit metadata.

The existing ten-minute lifetime is unchanged. Issuance serializes on the
authorized account row, invalidates previous active credentials for that
Vault/user, and inserts the replacement in one transaction. Concurrent issuance
leaves exactly one active credential. Expired/consumed/replaced records are
retained.

## Pair

After parsing and validating the descriptor, the client submits only the secret
and installation fields to **that descriptor's HTTPS management origin**:

```text
POST /api/vault-supplier/pair
```

```json
{
  "pairing_secret": "<secret-extracted-from-descriptor>",
  "installation_id": "<stable-installation-UUID>",
  "installation_public_key": "<P-256-SPKI-DER-Base64URL-or-PEM>",
  "key_algorithm": "ECDSA_P256",
  "supplier_version": "<client-version>",
  "protocol_version": 1
}
```

The request does not carry user authority or the full descriptor. Unknown fields
are rejected. `protocol_version` is a strict integer. Existing installation
public-key encoding support remains; no installation private key is sent.

The hash lookup resolves the authoritative record. Pairing checks expiry,
replacement, consumption, protocol, Vault UUID, canonical origin, server key ID,
and exact public SPKI. Missing legacy binding fields cannot authorize pairing.
If the current origin or server identity changed after issuance, the client
must obtain a new credential. Client-echoed fields cannot repair stale bindings.

The record is locked and rechecked in the same transaction that registers the
installation, grants its user authorization, and consumes the secret. Concurrent
use succeeds once. Registration failure rolls back consumption. An existing
installation UUID cannot be rebound to a different key or Vault; revocation
remains enforced. User identity is resolved by immutable UUID and must be active.

## Success response and client verification

```json
{
  "protocol_version": 1,
  "vault_id": "<credential-vault-UUID>",
  "vault_display_name": "<display-only-label>",
  "user_id": "<server-bound-immutable-authorized-user-UUID>",
  "user_display_name": "<display-only-label>",
  "installation_id": "<submitted-installation-UUID>",
  "server_identity": {
    "key_algorithm": "ECDSA_P256_SHA256",
    "public_key_spki_der_base64": "<exact-credential-SPKI>",
    "key_id_sha256": "<exact-credential-key-ID>"
  },
  "lan_connection_metadata": {"available": false, "mode": "unavailable"}
}
```

The client must verify the protocol, Vault UUID, installation UUID, server key
ID and exact public SPKI against its request/credential before persisting the
pairing. The immutable authorized user UUID is learned only from successful
pairing. Display labels never authorize access. Existing LAN discovery and
signature proof remain defined in [LAN protocol v1](vault-supplier-lan-protocol-v1.md).
No LAN URL/port is added to the bootstrap credential.

## Errors

Pairing domain errors use:

```json
{"detail":{"code":"pairing_identity_mismatch","message":"Pairing identity mismatch."}}
```

| Code | Meaning |
| --- | --- |
| `invalid_pairing_code` | Unknown/malformed secret or unbound legacy record |
| `pairing_code_expired` | Ten-minute lifetime elapsed |
| `pairing_code_used` | Secret already consumed |
| `pairing_code_replaced` | Credential superseded |
| `protocol_mismatch` | Unsupported request or bound protocol |
| `pairing_identity_mismatch` | Bound Vault/origin/key/SPKI differs from current authority |
| `invalid_pairing_origin` | Missing, unsafe, ambiguous, or RP-inconsistent configured origin |
| `invalid_pairing_descriptor` | Unavailable/malformed configured signing identity or malformed request |
| `invalid_installation_key` | Invalid installation public key or key algorithm |
| `invalid_installation_identity` | Existing installation UUID belongs to another identity |
| `installation_revoked` | Installation is revoked |
| `user_not_allowed` | Bound account is unavailable/inactive |

Domain failures are HTTP 400, except `user_not_allowed` (403). Request shape/type
errors are HTTP 422 using `invalid_installation_key` for key fields and otherwise
`invalid_pairing_descriptor`. Validation responses never echo input secrets.
Session authentication and browser host/CSRF errors retain the application-wide
authentication contract. Supplier challenge/authentication domain errors also
use the same code/message envelope.

## Persistence, upgrade, and audit

`vault_supplier_pairing_codes` retains `id`, `vault_id`, `user_id`, `code_hash`,
`created_at` (issued time), `expires_at`, `consumed_at`, `invalidated_at`, and
`protocol_version`. Three additive, idempotent nullable TEXT columns are added:
`management_origin`, `server_key_id`, `server_public_key_spki_der_base64`.
New issuance always supplies all three. Existing rows are not deleted or
backfilled with invented authority; NULL bindings fail closed. Existing paired
installations and challenge/authentication state remain usable.

Legacy standalone short-code issuance and acceptance are removed from the
normal product path. There is no developer/test fallback. The old `pairing_code`
response/request field is removed; clients must implement this contract.

Existing audit identifiers `vault_supplier_pairing_code_generated`,
`vault_supplier_pairing_code_replaced`, `vault_supplier_paired`, and
`vault_supplier_user_authorized` are retained. Successful pairing also records
`vault_supplier_installation_registered`. The prior flow did not audit failed
pair attempts; challenge failure auditing is preserved. Events contain UUIDs
and ordinary request context only, never credentials, secrets, or private keys.
