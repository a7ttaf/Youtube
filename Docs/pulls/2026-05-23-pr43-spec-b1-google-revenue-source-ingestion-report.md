# PR #43 — Spec B1 Google Revenue Source Ingestion Foundation — Report

**Date:** 2026-05-23
**Branch:** `pr/spec-b1-google-revenue-source-ingestion`
**Base:** `main` at `b19507f` (PR #42 merge — docs-only pivot)
**Status:** Implementation against the locked PR #42 spec + plan.

## What was requested

PR #42 landed the design pivot: B1 is Google source-reported revenue ingestion foundation, not FX storage. PR #43 implements that foundation across 10 phases per the plan at `Docs/superpowers/plans/2026-05-23-spec-b1-google-revenue-source-ingestion.md`. Hard non-goals from spec §3 preserved verbatim: no `fx_rates`/`fx_locked_month_rates`/`tenants.fx_provider_settings`, no `Permission.MANAGE_FX_RATES`, no live Google OAuth/API client, no paired-column migration of existing `_usd` tables, no frontend currency switcher.

## What was actually done

### Schema + reference data (Phases 1-2)

- `backend/ums_smart_revenue/db/iso_4217_2026_05.py` — immutable ISO 4217 snapshot (177 currencies) wrapped in `MappingProxyType` to prevent in-process mutation.
- `CurrencyORM` (platform-wide reference) and `GoogleRevenueSourceRowORM` (tenant-scoped) on `FinanceBase`.
- Alembic migration `20260523_0001_google_revenue_source_foundation`: creates `currencies`, seeds full ISO 4217, flips the v1 supported set (`AED`, `USD`, `EUR`, `GBP`, `SAR`, `EGP`), creates `google_revenue_source_rows` with composite FK and partial channel/month index. Adds nothing else; removes nothing. Legacy `currency_exchange_rates` table preserved per spec §6.

### Repository (Phase 3)

- `backend/ums_smart_revenue/connectors/google_source_rows/` package with `dataclasses.py` (`IsoCurrency`, `ParsedSourceRow`, `GoogleRevenueSourceRowEntry`, error classes, constants) and `repository.py`.
- `SqlAlchemyCurrenciesRepository` — read-only (`list_all`, `list_supported`, `get`); no mutation methods.
- `SqlAlchemyGoogleRevenueSourceRowRepository` — storage primitives: `upsert_many` (idempotent on `(tenant_id, source_system, source_row_key)`, dialect-aware via `_dialect_insert`), `list`, `list_for_channel`, `get_exact`. Pre-write validation (source_system membership, value_kind membership, `source_row_key` length=64, `amount_native >= 0`, `raw_payload` is dict, currency exists). Defensive copy of `raw_payload` to prevent caller-mutation aliasing.

### Parsers + synthetic fixtures (Phases 4-5)

- `backend/ums_smart_revenue/connectors/google_source_parsers/` package: `base.py` (`SourceRowParser` protocol, `ParserError`, shared `require_dict`/`require_str`/`require_int`/`parse_decimal_amount` helpers), `source_row_keys.py` (`build_source_row_key` returns full 64-char SHA-256 hex), and 3 parsers — `YouTubeReportingParser`, `YouTubeAnalyticsParser`, `AdSenseManagementParser`.
- `YouTubeAnalyticsParser` `query_signature` includes the request `ids` field, scoping `source_row_key` per-account (caught during review — without it, multi-CMS tenants would silently collide cross-account data).
- `parse_decimal_amount` rejects non-finite values (`NaN`, `Infinity`) with labeled `ParserError`.
- `YouTubeReportingParser` fails closed when `content_owner` is missing — no `"unknown"` sentinel.
- Synthetic fixtures under `tests/connectors/_fixtures/` (YouTube Reporting, YouTube Analytics, AdSense earnings + payment, plus byte-identical `_rerun.json` pairs for idempotency proofs). All values invented: channel IDs `UC_test_*`, AdSense `pub-test-*`, content owner `cms-test-*`. No real Google data.
- End-to-end test at `tests/connectors/test_google_source_ingestion_flow.py` parses all 4 fixtures, upserts via repository, asserts row count and zero-new-row idempotency on rerun.

### Guardrails (Phases 6-7)

- `tests/finance/test_finance_no_fx_dependency.py` — AST-based guard: every `.py` under `backend/ums_smart_revenue/finance/` (except `exchange_rates.py`) does NOT import or attribute-access `CurrencyExchangeRateORM`.
- `tests/connectors/google_source_rows/test_repository.py` — repository surfaces non-USD source rows at the storage layer (no API expansion in B1).
- `tests/auth/test_no_fx_permission.py` — `Permission.MANAGE_FX_RATES` does not exist (neither enum name nor value).
- `tests/auth/test_no_new_ingestion_permission.py` — permission-set snapshot pinned to PR #41 baseline (26 permissions); no additions or removals.
- `tests/connectors/google_source_parsers/test_parser_failure_states.py` — each parser raises typed `ParserError` on malformed payloads (parser-skeleton failure contract).

### Migration round-trip (Phase 8)

- `tests/db/_postgres_helpers.py` — fail-fast guard: raises `RuntimeError` at import time if `UMS_TEST_DATABASE_URL` is missing. No silent skip per the AST policy gate.
- `tests/db/test_google_revenue_source_migration_postgres.py` — 6 tests against disposable `postgres:18-alpine` on host port 55432: pre-state at prior head, upgrade creates expected tables/seeds, both indexes present, partial channel/month index has WHERE clause, downgrade drops only B1 tables, full round-trip is idempotent. `alembic_config` fixture constructs `Config()` without file path to avoid `logging.fileConfig()` side-effects polluting other suites.

### Docs (Phase 9, this commit)

- `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-{report,changelog,handoff}.md` (this triple).
- `Docs/01_IMPLEMENTATION_PLAN.md` — added ✅ PR #43 sub-bullet under "Source-reported currency foundation".
- `Docs/15_DELIVERY_BACKLOG.md` — added ✅ PR #43 bullet under "Cross-cutting shipped".

## Validation

- `python scripts/run_validation_gate.py` (with `UMS_TEST_DATABASE_URL` set): **6/6 steps green** (ruff → AST policy → pytest 954 passed → vitest 34 passed → git diff --check worktree → git diff --check staged).
- Pytest delta from PR #41 baseline (821): **+133 tests** (+89 in 17 new test files at submission; +44 review-hardening regressions added during Codex/CodeRabbit review).
- PostgreSQL: 8/8 passed on disposable `postgres:18-alpine` (6 migration round-trip + 1 repository upsert covering the production `on_conflict_do_update` path + 1 raw_payload object-shape CHECK rejection via direct SQL).
- Frontend tests: 34 passed (unchanged from PR #41; no frontend code touched).
- AST policy gate: still passes (no skip/xfail introduced).
- `python -m pytest tests/api/ tests/finance/ -q`: 420 pre-existing tests pass (no regressions).

**Phase 10 late-fix:** the validation gate's first run surfaced two import-path issues that the targeted-pytest runs in earlier phases didn't catch:
1. `tests/db/test_google_revenue_source_migration_postgres.py` used `from tests.db._postgres_helpers import POSTGRES_URL`. Full-suite pytest collection can't resolve `tests.db.X` because `tests/db/` is the test file's home (pytest's `prepend` mode puts `tests/db/` on sys.path, not the rootdir). Fixed by switching to sibling import `from _postgres_helpers import POSTGRES_URL`.
2. Parser tests use `importlib.resources.files("tests.connectors._fixtures.X")` which requires `tests/` and `tests/connectors/` to be regular packages (not PEP 420 namespace packages). Fixed by adding empty `__init__.py` to both directories.

Both fixes landed in commit `3a4da12` (post Phase 9 docs commit). After applying that commit, the full validation gate ran fully green (the earlier failures were only in the gate's first run, before `3a4da12`).

## Blast radius

*No graph projection impact detected.* Neo4j was retired in PR #12.

- Authorization: unchanged. No permission added or removed. Existing `connectors.run_jobs` covers Google source ingestion auth (no new permission needed).
- Finance: unchanged. No `_usd` column touched. No finance service modified. Guard test pins the contract that finance modules outside `finance/exchange_rates.py` do not consume `CurrencyExchangeRateORM`.
- Audit: unchanged. No new audit event type. `EXCHANGE_RATE_SYNCED` preserved as legacy scaffolding.
- Reports/exports: unchanged.
- Frontend: unchanged. Zero touches to `frontend/`.
- Database schema: two new tables on `FinanceBase` (`currencies`, `google_revenue_source_rows`). Migration `20260523_0001` advances Alembic head from `20260521_0001`. Legacy `currency_exchange_rates` (migration `20260513_0004`) preserved untouched.

## Remaining risks

- **Pre-merge**: PR #43 depends on a disposable Postgres for the migration round-trip test. CI must spin one up or the test fails fast at collection (by design — no silent skip).
- **Fixture drift risk**: synthetic fixture payloads mirror Google API shapes from public documentation. If Google changes the upstream response shape, parsers may need follow-up. The synthetic-data discipline (`tests/connectors/_fixtures/README.md`) must be enforced on every future fixture addition.
- **`YouTubeAnalyticsParser` `query_signature` review**: post-fix the canonical string is `f"{ids}|{metrics_csv}|{dimensions_csv}|{metric_name}"`. Per-account scoping is now correct, but future parsers consuming new Google API shapes should follow the same pattern.

## Follow-up recommendations

After PR #43 merges:

1. **B2: Live Google connector** — build OAuth flow, YouTube Reporting jobs listing/download, YouTube Analytics query client, AdSense report generation polling, raw-file storage backend, scheduled run. Plug into B1's parsers via `SourceRowParser` protocol.
2. **B3: Display-only currency conversion** — only after operators decide which display currency they want for non-USD source rows. Convert at the response edge, never in storage. Labeled non-official.
3. **Paired-column migration** — separate spec, migrates `revenue_facts*` and `bank_reconciliation_entries` from `_usd` to paired `(amount_native, currency_iso4217)`. Big blast radius — touches 24+ files per the original spec audit.

## Rollback notes

`git revert <merge>` followed by `alembic downgrade -1` restores the exact pre-merge schema. Legacy `currency_exchange_rates` was never touched, so no data is lost. No frontend, no API, no audit, no permission changes to roll back.
