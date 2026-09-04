# Vault Supplier LAN protocol v1

Management pairing now uses the self-describing `PVPAIR1` bootstrap specified in
[pairing protocol v1](vault-supplier-pairing-protocol-v1.md). Its pinned identity
must match the pairing response and the LAN proof below.

Personal Vault implements the authoritative VS-003B contract at Vault Supplier commit `f77a3a76d3b79468114d8039d90b5c2d569d6857`.

Successful `POST /api/vault-supplier/pair` responses include `server_identity`:

```json
{"key_algorithm":"ECDSA_P256_SHA256","public_key_spki_der_base64":"<standard-base64-with-padding>","key_id_sha256":"<64-lowercase-hex>"}
```

The public key is P-256 SubjectPublicKeyInfo DER. Its key ID is SHA-256 of those exact DER bytes. An existing pairing without this object is LAN-unverified and must be re-paired; PV does not migrate or silently trust it.

Discovery advertises `_vault-supplier._tcp.local.` as `Personal Vault <first-eight-vault-UUID-hex>` with the exact VS-003B TXT records: `protocol=1`, `vault_id`, `identity_path`, `verify_path`, and `server_key_id`. DNS-SD is discovery metadata only.

`GET /api/vault-supplier/lan/identity` returns unsigned discovery metadata. `POST /api/vault-supplier/lan/verify` accepts only `{"protocol_version":1,"nonce":"<43-char-unpadded-base64url>"}`. The nonce decodes to exactly 32 bytes. The response has the exact signed seven-line LF-terminated UTF-8 canonical payload, standard padded Base64 of that payload, and standard padded Base64 ASN.1 DER ECDSA P-256/SHA-256 signature defined by VS-003B. This pairing foundation advertises `receiver_available=false` and `resumable_upload_supported=false`. File transfer is not included.

LAN errors use `{"detail":{"code":"...","message":"..."}}`: `invalid_nonce` and `protocol_mismatch` are HTTP 400; an unavailable signing identity is HTTP 500 `server_identity_unavailable`.
