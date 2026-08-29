# Contributing

Use `development` for integration work and keep `main` releasable. Create focused branches, keep pull requests small, and include relevant automated tests.

Do not commit credentials, production URLs, user identities, media files, database exports, private diagnostics, or host-specific deployment material. Use placeholders in examples and `.env.example`; keep real configuration in ignored files.

Before proposing a change, run the relevant backend tests and frontend checks. Changes that affect database-backed behaviour require the relevant PostgreSQL-backed test coverage in a separately provisioned development database.

The project is currently preparing for public development. If a proposed contribution needs product, architecture, storage, or security decisions, open that discussion before implementation.
