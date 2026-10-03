# Runtime source identity admission

Set `PV_ALLOWED_SOURCE_REPOSITORIES` in each backend and section mover runtime
environment before upgrading. Set `PV_REPOSITORY` to the exact source identity
of that deployment. For example:

```dotenv
PV_REPOSITORY=example-owner/personal-vault
PV_ALLOWED_SOURCE_REPOSITORIES=example-owner/personal-vault
```

The public upstream identity `anrorob/personal-vault` can also be explicitly
authorized. Multiple identities are comma-separated. Matching is case-sensitive
and exact; there is no wildcard, prefix, substring or suffix trust. Spaces and
tabs around configured entries are ignored, and duplicate entries are harmless.
The live `PV_REPOSITORY` value is not trimmed or case-normalized.

Identities use ASCII `owner/repository` form, with a 1–39 character alphanumeric
owner (internal single hyphens allowed) and a 1–100 character repository name
using letters, digits, dots, underscores or hyphens. URLs, `.git` suffixes,
empty entries, dot-only names and control characters are invalid. At most 64
entries and 16,384 configuration characters are accepted. Missing, empty or
invalid configuration denies all worker admission, even if another entry is valid.

This is an operator-owned deployment safety gate. Repository/build metadata
alone is not permission and this setting is not credential verification. Keep
each environment's intended source identity, endpoints, work roots, storage,
database and secrets isolated. It does not authorize access to user assets or
change immutable ownership, signed requests or managed-storage protections.

KEN still requires `PV_KEN_ENABLED=true`, a Development or Production environment
and an explicit `PV_KEN_URL`; Production also requires `PV_KEN_WORK_ROOT`.
Section mover workers still require a Development or Production environment.
Production section moves require `PV_SECTION_MOVE_ENABLED=true`; Development
retains its existing default enabled flag after source admission succeeds.

Supply the new allowlist to both backend and section mover. The independent
analyser service does not compare repository names and needs no image change
for this contract. No schema change or separate migration command is required.
Rollback may retain the new environment setting; old code ignores it.
