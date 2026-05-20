# PR #27 — Full Repo Ruff Cleanup — Changelog

**Date:** 2026-05-20
**PR:** https://github.com/XGenerationy/Youtube/pull/27
**Branch:** `pr/s2-4b-repo-ruff-cleanup`
**Base:** `pr/s2-4a-tenant-id-on-operational-tables`

## Added

- `docs/pulls/2026-05-20-pr27-repo-ruff-cleanup-report.md` (this PR's report artifact).
- `docs/pulls/2026-05-20-pr27-repo-ruff-cleanup-changelog.md` (this file).
- `docs/pulls/2026-05-20-pr27-repo-ruff-cleanup-handoff.md` (handoff artifact).

## Changed

### Source semantics — none

No business logic, finance calculation, tenant scoping, authorization rule, audit behavior, migration semantics, API contract, or Neo4j read-only projection changed.

### Lint / format — yes, broadly

- **101 files reformatted** by `ruff format` (line breaks, import grouping, trailing commas, etc.).
- **35 files** normalized from CRLF → LF line endings.
- **4 files** that my manual line breaks didn't match `ruff format`'s style had their final shape applied by `ruff format` rerun.

### Symbol renames — 3 exceptions

- `backend/ums_smart_revenue/auth/api_guards.py`: `class AccessDenied` → `class AccessDeniedError`. All call sites within the file updated. No external imports.
- `backend/ums_smart_revenue/graph/readonly_service.py`: `class GraphAccessDenied` → `class GraphAccessDeniedError`. Test importer updated (`tests/graph/test_readonly_service.py`).
- `tests/api/test_connectors_api.py`: local `class DuplicateOrig` → `class DuplicateOrigError`. Only used inside the same test function.

### Enum migration — 3 classes

- `backend/ums_smart_revenue/auth/permissions.py`: `class Permission(str, Enum)` → `class Permission(StrEnum)`. Import updated to `from enum import StrEnum`.
- `backend/ums_smart_revenue/auth/roles.py`: `class RoleKey(str, Enum)` → `class RoleKey(StrEnum)`. Import updated.
- `backend/ums_smart_revenue/auth/scopes.py`: `class ScopeType(str, Enum)` → `class ScopeType(StrEnum)`. Import updated.

### Generic syntax — 1 function

- `backend/ums_smart_revenue/auth/api_guards.py`: `guarded_call` converted to PEP 695 `def guarded_call[T](...)`. Removed `from typing import TypeVar` and `T = TypeVar("T")`.

### Cypher whitespace — 2 query templates

- `backend/ums_smart_revenue/graph/cypher.py`: the `hierarchy` and `revenue_flow` Cypher templates had their long `MATCH path =` lines broken across 3 lines each. Cypher is whitespace-tolerant; the read-only-guard test still passes; functional output is identical.

### Alembic downgrade docstrings — 8 files

The line `"""Fully reverse upgrade(): drop all tables and indexes created in this migration in reverse dependency order."""` (117 chars) was split into a two-line docstring across these migrations:

- `20260510_0001_security_foundation.py`
- `20260510_0002_org_registry.py`
- `20260510_0003_finance_close.py`
- `20260510_0004_revenue_facts.py`
- `20260510_0005_manual_overrides.py`
- `20260510_0006_raw_report_files.py`
- `20260510_0007_number_explanations.py`
- `20260510_0008_export_jobs.py`

And the docstring `"""Fully reverse upgrade(): drop the revoke_reason column added in this migration."""` (89 chars) was split in:

- `20260511_0001_permission_grant_revoke_reason.py`

These are docstring whitespace changes only — the function bodies are unchanged.

### SQL CHECK constraint string concatenations — 6 alembic files

Long SQL CHECK constraint strings (>88 chars) were split into multiple concatenated Python string literals. Compile-time concatenation produces the identical literal SQL the database sees. Files:

- `security_foundation.py` (3 CHECK constraints: `scope_type IN (...)`, scope_id required logic, active+revoked_at logic)
- `revenue_facts.py` (`source_kind IN (...)`)
- `manual_overrides.py` (approval-fields conjunction)
- `export_jobs.py` (`export_type IN (...)`, scope_id required logic)

### Long error-message strings — 4 backend files

Concatenated string breaks applied for >88 char user-visible error messages in:

- `backend/ums_smart_revenue/api/connectors.py` (external secret reference error)
- `backend/ums_smart_revenue/api/finance_close.py` (allocation rule conflict)
- `backend/ums_smart_revenue/api/revenue.py` (connector_key/source_kind validation)
- `backend/ums_smart_revenue/finance/reconciliation.py` (3 messages: no-facts, insufficient-sources, gross-variance)
- `backend/ums_smart_revenue/finance/revenue_summary.py` (no-facts)
- `backend/ums_smart_revenue/db/tenant_models.py` (currency CHECK constraint)

The user-facing error message TEXT is unchanged byte-for-byte. Only the source-code layout changed.

### Test fixtures — 8 files

Concatenated string breaks applied for >88 char strings in:

- `tests/db/test_alembic_scaffold.py` (2 sites; long migration path → `Path(...)` segmented)
- `tests/db/test_explanation_migration.py` (1)
- `tests/db/test_manual_override_migration.py` (1)
- `tests/db/test_raw_report_file_migration.py` (1)
- `tests/db/test_tenants_migration.py` (2; long path + long SQL INSERT)
- `tests/db/test_explanation_models.py` (formula literal)
- `tests/auth/test_user_roles_repository.py` (comment)
- `tests/api/test_connectors_api.py` (function name shortened: `test_connector_credential_integrity_error_classifier_uses_duplicate_constraint_only` → `test_credential_integrity_classifier_uses_duplicate_constraint_only`)
- `tests/api/test_groups_api.py` (SQL constraint message in IntegrityError fixture)
- `tests/api/test_raw_report_files_api.py` (response detail assertion)
- `tests/api/test_revenue_explanations_api.py` (warnings message)
- `tests/api/test_user_roles_api.py` (multi-line comment)
- `tests/finance/test_revenue_reconciliation.py` (2 instances of the same gross-variance message)

## Removed

- `backend/ums_smart_revenue/auth/api_guards.py`: `from typing import TypeVar` and `T = TypeVar("T")` (replaced by PEP 695 inline syntax).

## Behavior changes

- **Source semantics: none.** Pytest count unchanged: 490 → 490. Same 7 SQLAlchemy reflection warnings.
- **`str(enum)` representation: changed** for `Permission`, `RoleKey`, `ScopeType`. Before: `"Permission.VIEW_ANALYTICS"`. After: `"analytics.view"`. Equality and `.value` and JSON serialization behave identically. No tests rely on the old form. If a downstream log statement uses raw `str()` on an enum without `.value`, the log line will switch to the value-only form (which is more useful for parsing).
- **Exception class spelling: changed** for 3 symbols. Old names are removed (no backwards-compat shim). Downstream importers MUST use the new `*Error` names.

## Test surface change

- Pytest total: 490 → 490 (unchanged).
- 1 test function renamed: `test_connector_credential_integrity_error_classifier_uses_duplicate_constraint_only` → `test_credential_integrity_classifier_uses_duplicate_constraint_only`. Pytest discovery is by name pattern (`test_*`), and the function isn't referenced by string anywhere — safe.

## Documentation changes

- 3 new artifacts under `docs/pulls/` (report + changelog + handoff). No edits to existing `Docs/*.md` architecture or API specs.

## Schema / data

- **No** Prisma/Alembic migration. **No** DB column, index, constraint, enum, status, or JSON-shape change. Alembic CHECK constraint strings are reformatted in source code only — the SQL they emit to the database is identical (Python string concat is a compile-time operation).

## Configuration / runtime

- No `.env`, `pyproject.toml`, `alembic.ini`, Docker, CI, or operational contract change.
