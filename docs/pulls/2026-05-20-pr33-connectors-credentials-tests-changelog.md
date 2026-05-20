# PR #33 — Connector Credential Repository Tests — Changelog

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/33
**Branch:** `pr/s2-4b-connectors-credentials-tests`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`

## Added

- `tests/connectors/test_credentials.py` (448 lines, 38 tests).
- `tests/connectors/` directory (new subdirectory parallel to `tests/finance/`, `tests/reports/`, `tests/org/`).
- `docs/pulls/2026-05-20-pr33-connectors-credentials-tests-report.md`.
- `docs/pulls/2026-05-20-pr33-connectors-credentials-tests-changelog.md` (this file).
- `docs/pulls/2026-05-20-pr33-connectors-credentials-tests-handoff.md`.

## Changed

### Source semantics — none

No `backend/ums_smart_revenue/**` file is touched. `connectors/credentials.py` is unchanged.

### Lint / format — none

No Python source file is modified outside of the new test file.

### Symbol renames — none

### Enum migration — none

### Generic syntax — none

### Cypher whitespace — none

### Alembic — none

### SQL — none

### Tests — added 38

Direct module-level tests for `connectors/credentials.py`:

| Surface | Tests | What is asserted |
|---|---|---|
| `is_external_secret_ref` | 4 (2 parametrized × ~16 cases) | Every allowed prefix accepted; blank/whitespace/empty-suffix/unknown rejected; whitespace stripped; `SECRET_REF_PREFIXES` const matches documented allowlist |
| `ConnectorCredentialEntry.to_api` | 2 | Every field; secret material never in serialized output |
| `create_credential` happy path | 3 | tenant_id stamp + every field; non-blank ref → `has_secret_ref=True`; blank ref → `has_secret_ref=False`; `to_api()` never leaks the secret ref string |
| `create_credential` duplicate detection | 3 | Pre-check raises `Conflict`; distinct keys succeed; cross-tenant same key succeeds |
| `create_credential` input validation | 2 | Malformed/empty actor UUID rejection |
| `list_credentials` | 6 | Default-page `(connector_key, account_id)` ordering; pagination across 3 pages with `has_more`; bad limit/offset; cross-tenant isolation; empty page; no secret leakage in serialized output |
| ORM-level integrity (race-safety net) | 1 | Direct duplicate insert raises `IntegrityError` |
| Repository defaults & constants | 3 | `_tenant_id`, `MAX_CREDENTIAL_PAGE_SIZE`, `CONNECTOR_CREDENTIAL_UNIQUE_CONSTRAINT` |

## Removed

- Nothing removed.

## Behavior changes

- **Source semantics: none.** Pytest count: 538 → 576 (+38).
- **Regression guards added:** see report.md for the 5 explicit security vectors guarded.

## Test surface change

- Pytest total: 538 → 576 (+38).
- 1 new test file: `tests/connectors/test_credentials.py`.
- 1 new test subdirectory: `tests/connectors/`.
- 38 new test functions (some `@pytest.mark.parametrize`), all `test_*` discoverable.
- No existing test file, fixture, or conftest is modified.

## Documentation changes

- 3 new artifacts under `docs/pulls/`.

## Schema / data

- No Prisma/Alembic migration. No DB column, index, constraint, enum, status, or JSON-shape change.

## Configuration / runtime

- No `.env`, `pyproject.toml`, `alembic.ini`, Docker, CI, or operational contract change.

## Pattern compatibility

- Mirrors `tests/api/test_connectors_api.py` for the secret-ref allowlist behavior at the API layer (this PR adds the missing repository-layer counterpart).
- Uses fresh canonical UUIDs for this surface:
  - `DEFAULT_TENANT_UUID = UUID(UMS_TENANT_ID)`.
  - `OTHER_TENANT_UUID = UUID("00000000-0000-0000-0000-000000081999")` (`8xxxx` range to avoid collision with AdSense `3xxxx`, bank reconciliation `4xxxx`, channel groups `5xxxx`, explanations `6xxxx`, raw report files `7xxxx`).
  - `ACTOR_USER_ID = "00000000-0000-0000-0000-000000081001"`.

## Compatibility with origin/main

- Purely additive. When the stack rebases onto / merges with `origin/main`, the new test file ships unchanged.
