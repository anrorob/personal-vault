# Vault Supplier transfer protocol v1

This is the authoritative Personal Vault receiver contract for the Vault
Supplier client. It applies only after the Supplier has reached
`ConnectedVerified` using the pinned server signing key protocol. File payload
traffic is LAN-only: use `https://pv-srv-001.local:8443`; never send transfer
payloads to `dev-vault.pv-hq.com` or through Cloudflare.

Protocol version is the JSON integer `1`. A different value receives HTTP 400
with `{"detail":{"code":"protocol_mismatch","message":"..."}}`.

## Authorization

Use the existing installation-key challenge flow first:

1. `POST /api/vault-supplier/installations/{installation_id}/challenge` with
   `{"requested_user_id":"<PV user UUID>"}`.
2. Sign the returned decoded challenge bytes with the paired ECDSA P-256
   installation private key using SHA-256 and ASN.1 DER signature encoding.
3. `POST /api/vault-supplier/installations/{installation_id}/authenticate`
   with `{"challenge_id":"<UUID>","signature":"<unpadded Base64URL DER>"}`.

The successful response includes `authorization_token` and
`authorization_expires_at`. The token is a short-lived, opaque bearer token;
it is valid for fifteen minutes and is never logged or persisted by the client
beyond its expiry. Every transfer request must include:

```http
Authorization: Bearer <authorization_token>
X-PV-Supplier-Installation-ID: <installation UUID>
X-PV-Supplier-User-ID: <PV user UUID>
```

PV checks the token, installation, immutable user UUID, local Vault UUID, and
current non-revoked authorization on every request. A revoked or expired
authorization receives `transfer_not_authorized`; authenticate again after an
ordinary token expiry. Display names and usernames are not authority.

## Intake state and duplicates

`GET /api/vault-supplier/intake/state` returns:

```json
{"protocol_version":1,"state":"READY","reason":null}
```

`state` is exactly `READY`, `BUSY`, or `PAUSED`. `PAUSED` reflects the actual
PV intake gate. `BUSY` means the v1 serial receiver already has an active
session. Intelligence services never determine this state.

`POST /api/vault-supplier/intake/check-hashes` accepts at most 128 unique
lowercase SHA-256 values:

```json
{"protocol_version":1,"sha256":["<64 lowercase hex>"]}
```

It returns:

```json
{"protocol_version":1,"hashes":[{"sha256":"<digest>","duplicate":false}]}
```

`duplicate` is scoped to the authorized immutable user and means active
duplicate authority exists. It is content-based: filenames do not affect it.
The endpoint never exposes a Vault-wide checksum inventory.

## Session lifecycle

Create a session with `POST /api/vault-supplier/transfers`:

```json
{
  "protocol_version": 1,
  "filename": "clip.mp4",
  "total_size": 123456,
  "sha256": "<64 lowercase hex>",
  "media_type": "video/mp4",
  "source_context": {"source_path_components":["Camera","DCIM"]}
}
```

`filename` is one presentation filename, not a path. It must contain no path
separators, control characters, `.`/`..`, or leading/trailing whitespace. An
invalid filename receives HTTP 400 with stable code `invalid_filename`.
`source_context` is advisory JSON only, has a 16 KiB UTF-8 limit, never names
a server path, and never controls routing. `total_size` is `0` through 10 TiB.
PV rejects a current duplicate with HTTP 409 `duplicate_content` and rejects
new work when the receiver is `intake_busy` or `intake_paused`.

The HTTP 201 response contains the opaque UUID `transfer_id`, zero initial
`bytes_received`, and `chunk_size_min` 1, `chunk_size_recommended` 8388608,
and `chunk_size_max` 67108864. Sessions expire seven days after creation.

Get server-authoritative resume status using
`GET /api/vault-supplier/transfers/{transfer_id}`. The response contains:

```json
{
  "protocol_version":1,
  "transfer_id":"<UUID>",
  "state":"created|receiving|paused|verifying|finalized|failed|aborted",
  "total_size":123456,
  "bytes_received":65536,
  "sha256":"<64 lowercase hex>",
  "upload_may_resume":true,
  "expires_at":"<RFC3339 timestamp>",
  "arrival_hall_receipt_id":null,
  "arrival_hall_filename":null
}
```

After a process or network interruption, ask this endpoint and resume from
exactly its `bytes_received`; do not trust a remembered client offset.

## Sequential chunk upload

Use `PUT /api/vault-supplier/transfers/{transfer_id}/data` with raw bytes.

```http
Content-Type: application/octet-stream
Content-Length: <exact chunk byte count>
X-PV-Upload-Offset: <decimal server-authoritative offset>
```

v1 supports only sequential append. `X-PV-Upload-Offset` must exactly equal
the returned `bytes_received`; sparse, overlapping, and out-of-order writes
are rejected as HTTP 409 `invalid_offset`. Each non-empty chunk is 1 through
67108864 bytes and must not exceed `total_size`. PV streams bytes into
`<transfer-id>.part`, flushes and fsyncs that file before committing the new
`bytes_received`, so it never claims bytes not durably written. On status or
next upload PV reconciles the durable file length with persisted progress.

Only one active receiver session is accepted in v1. This is the large-file
serial foundation; future small-file concurrency must be explicitly added.

## Finalize and abort

`POST /api/vault-supplier/transfers/{transfer_id}/finalize` has no body. It
requires the complete exact length and streams SHA-256 over the staged file.
On mismatch it returns HTTP 422 `checksum_mismatch`, marks the session failed,
and does not create an Arrival Hall item. On success PV records immutable
owner UUID metadata, atomically hard-links the verified same-filesystem
staging file into Arrival Hall, removes the `.part`, and returns `state`:
`finalized` plus `arrival_hall_receipt_id` (the transfer UUID) and the final
Arrival Hall filename. This is the handoff point: Vault Master later processes
the normal Arrival Hall item; Florence and other intelligence do not gate it.

`DELETE /api/vault-supplier/transfers/{transfer_id}` aborts only a non-final
session, removes its `.part`, and returns state `aborted`. Finalized transfers
refuse abort with `invalid_transfer_state` and cannot affect canonical content.

## Error envelope and ingress boundary

Every domain failure uses exactly:

```json
{"detail":{"code":"<stable-code>","message":"<human message>"}}
```

Codes include `intake_paused`, `intake_busy`, `invalid_checksum`,
`invalid_filename`, `invalid_request`, `duplicate_content`, `invalid_offset`, `invalid_transfer_state`,
`transfer_not_found`, `transfer_not_authorized`, `transfer_expired`,
`size_mismatch`, `checksum_mismatch`, `receiver_unavailable`, and
`protocol_mismatch`.

Malformed JSON, missing required fields, invalid field types, and invalid UUID
path parameters for these receiver routes receive HTTP 422 with
`invalid_request`. This normalization applies only to Vault Supplier receiver
routes; it does not alter validation responses elsewhere in Personal Vault.

`source_context` is retained only in the Vault Supplier transfer-session
record. It is advisory evidence, not ownership, filesystem-placement, or
routing authority, and v1 does not copy it into the Arrival Hall owner
manifest.

## Contract status

Remaining wire-level ambiguities: **NONE**

Implementation/document disagreements found: **NONE**
