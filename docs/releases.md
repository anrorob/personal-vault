# Releases and version identity

## Version authority

[`VERSION`](../VERSION) is the single canonical source for the public release
version. It contains only a Semantic Versioning value such as `1.0.0`.

The backend loads that file for its OpenAPI version and returns it from
`/api/health`. Each frontend build writes the same version, along with safe
build identity, to `/build-info.json`. Neither surface includes secrets,
configuration values, or deployment credentials.

| Variable | Purpose |
| --- | --- |
| `PV_ENVIRONMENT` | Exactly `development`, `test`, or `production`. |
| `PV_COMMIT` | Immutable Git SHA for a release build. Non-production builds may use `development`, `test`, or `unknown`. Production rejects a non-SHA value. |

## Branch roles

```text
feature/* -> development -> development Vault test -> explicit approval
                                                    -> main -> vX.Y.Z -> production
```

- `feature/*` is for isolated work and is integrated by pull request.
- `development` is the integration branch and the only branch eligible for a development-Vault update after explicit approval.
- `main` contains approved release candidates only. Moving `development` to `main` requires explicit approval; it does not deploy or tag automatically.
- A production release is an annotated, immutable `vX.Y.Z` tag created from the approved `main` commit. Production deploys the exact tag/commit only.

The CI workflow runs backend tests, PostgreSQL-backed tests, frontend linting,
and a frontend build for pushes to `feature/*`, `development`, and `main`, and
for pull requests targeting `development` or `main`. It never deploys, tags,
promotes branches, or changes repository settings.

## Required repository safeguards

These GitHub settings are a deliberate manual follow-up; this repository does
not configure them automatically.

- Protect `main`: require pull requests and the `Backend / Python 3.13` and `Frontend / Bun` checks; require the branch to be current; block force pushes and deletion.
- Protect `development`: block force pushes and deletion, and require the same checks before protected integration changes.
- Limit tag creation to trusted maintainers and protect the `v*` tag pattern if the GitHub plan supports tag protection rules.

## Development Vault contract

The development Vault is a separate environment, not a production alias. Its deployment must use its own host, secrets, PostgreSQL database and role, storage roots, session material, and WebAuthn relying-party identity:

```dotenv
PV_ENVIRONMENT=development
PV_WEBAUTHN_RP_ID=dev-vault.pv-hq.com
PV_WEBAUTHN_ORIGIN=https://dev-vault.pv-hq.com
```

Do not point a development environment at production data, Vault storage, configuration files, or credential material. A production RP ID/origin must not be reused in development. This repository intentionally does not create or deploy the development Vault.

## Approved release procedure

1. Complete and validate the change on `development`.
2. With explicit approval, update the development Vault from the exact `development` commit and perform the required authenticated acceptance.
3. With separate explicit approval, merge the tested commit into `main`.
4. Confirm CI succeeds on that exact `main` SHA, update `VERSION` if needed, and create an annotated `vX.Y.Z` tag matching the file value.
5. With production-deployment approval, fetch and verify the tag on the production host, then deploy that detached exact tag. Set `PV_ENVIRONMENT=production` and `PV_COMMIT` to the resolved tag SHA.
6. Verify `/api/health`, `/build-info.json`, service logs, and the expected authenticated product behaviour. Record the tag and SHA in the release notes.

No command in this repository performs these external state changes by itself. The existing production deployment remains independent until a future, explicitly approved migration.

## Migration and rollback safety

Before a production release, take and verify a database backup, record the current tag/SHA and deployment configuration revision, and review the change's migration plan. Database changes must be additive and backward-compatible until a separately approved cleanup release; a failed migration stops the deployment and is investigated before retrying.

To roll back application code, redeploy the prior known-good immutable tag only after confirming schema compatibility. Do not attempt a blind database schema downgrade. If data recovery is required, restore the verified backup and its matching configuration only under a specific recovery approval, then validate service health and data integrity.
