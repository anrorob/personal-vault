# Supplier LAN receiver host authorization

Set `PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS` in the backend runtime environment to
the exact hostname(s) of your Supplier LAN listener:

```dotenv
PV_VAULT_SUPPLIER_LAN_ALLOWED_HOSTS=vault-server.local
```

Multiple hostnames are comma-separated, for example
`vault-server.local,backup-vault.local`. Matching is case-insensitive and exact;
there is no suffix, substring or wildcard matching. Entries must be ASCII
hostnames without URLs, ports, paths or empty elements. Missing, empty or invalid
configuration disables all Supplier payload routes with HTTP 404.

The request's HTTP `Host` may include a valid numeric port. Malformed, missing
or duplicate Host headers are denied. Keep the existing restricted LAN proxy,
TLS validation, pinned server identity and Supplier challenge/bearer-token
authorization: this hostname allowlist does not replace them. Add the listener
hostnames to `PV_ALLOWED_HOSTS` too; that general API allowlist does not itself
authorize Supplier payload routes. Certificate-derived pairing hints provide
discovery only and do not populate this authorization allowlist.

For the Compose definition, supply this variable in its backend
runtime authentication environment file. For other deployments, supply it through the
backend's existing runtime environment/configuration layer. Configure each
environment independently using its actual listener hostname before activating
the application update. No schema migration or Supplier protocol change is
required. Application rollback may retain this extra environment variable.
