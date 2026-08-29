# Development environment plan

Run development as a separate deployment plane from production:

- a distinct hostname, reverse-proxy site, Cloudflare tunnel or DNS route, and TLS configuration;
- separate Compose project name, PostgreSQL database and role, session secret, passkey credentials, service accounts, job state, model cache, and storage roots;
- development-only media copies or synthetic fixtures, never production mounts or catalogue data.

For passkeys, the WebAuthn relying-party ID must match the development hostname and the configured origin must exactly match its HTTPS origin. Browser cookies must be scoped to the development host; do not share cookie names, domains, or signing keys with production. The reverse proxy must forward the public HTTPS scheme and host so origin checks remain correct.

Use independent service networks where practical. A development Jellyfin service, if used, needs its own API key and data directory. Keep database migrations additive and test them in development before any production change.
