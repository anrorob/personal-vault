# Contributing

Personal Vault is currently owner-controlled. External contributors are welcome to propose changes through pull requests; they do not receive automatic merge or write authority.

## Workflow

- Fork the repository and use a focused branch.
- Target `development` unless a maintainer asks for `main`.
- Keep `main` releasable and avoid unrelated formatting or generated-file churn.
- Explain the problem, scope, tests, and any compatibility implications in the pull request.

## Expectations

- Follow the public [architecture overview](docs/architecture.md), especially the immutable-identity, authorization, and logical-storage boundaries.
- Add or update relevant tests. PostgreSQL-backed behaviour requires PostgreSQL-backed tests; see [backend/TESTING.md](backend/TESTING.md).
- Use the established frontend lint/build and backend test commands where relevant.
- Discuss significant architecture, storage, identity, security, or product-direction changes before implementation.

Never include credentials, private media, real user identities, production URLs, database exports, host-specific operations material, or private diagnostics in commits, pull requests, or issues.

Use normal GitHub issues for reproducible bugs and feature discussions. Use [SECURITY.md](SECURITY.md) for vulnerabilities.
