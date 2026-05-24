# PR #43 — Changelog

## Added

### Reference data + ORM

- `backend/ums_smart_revenue/db/iso_4217_2026_05.py` — immutable ISO 4217 snapshot (177 currencies); entries wrapped in `MappingProxyType`.
- `backend/ums_smart_revenue/db/source_models.py` — `CurrencyORM`, `GoogleRevenueSourceRowORM` on `FinanceBase`. Cross-metadata FK technique documented inline.
- `backend/ums_smart_revenue/db/alembic/versions/20260523_0001_google_revenue_source_foundation.py` — Alembic migration: creates `currencies` + `google_revenue_source_rows`, seeds ISO 4217, flips v1 supported set. Drops nothing.

### Repository layer

- `backend/ums_smart_revenue/connectors/google_source_rows/__init__.py` — public surface.
- `backend/ums_smart_revenue/connectors/google_source_rows/dataclasses.py` — `IsoCurrency`, `ParsedSourceRow`, `GoogleRevenueSourceRowEntry`, `GoogleRevenueSourceRowError`, `GoogleRevenueSourceRowValidationError`, `CurrencyValidationError`, `ALLOWED_SOURCE_SYSTEMS`, `ALLOWED_VALUE_KINDS`, `SOURCE_ROW_KEY_LENGTH=64`.
- `backend/ums_smart_revenue/connectors/google_source_rows/repository.py` — `SqlAlchemyCurrenciesRepository` (read-only); `SqlAlchemyGoogleRevenueSourceRowRepository` with `upsert_many` (idempotent + defensive `raw_payload` copy), `list`, `list_for_channel`, `get_exact`. Dialect-insert helper.

### Parsers + fixtures

- `backend/ums_smart_revenue/connectors/google_source_parsers/__init__.py` — re-exports.
- `backend/ums_smart_revenue/connectors/google_source_parsers/base.py` — `SourceRowParser` protocol, `ParserError`, shared `require_dict`/`require_str`/`require_int`/`parse_decimal_amount` helpers.
- `backend/ums_smart_revenue/connectors/google_source_parsers/source_row_keys.py` — `build_source_row_key` (full 64-char SHA-256 hex).
- `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_reporting.py` — `YouTubeReportingParser`.
- `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py` — `YouTubeAnalyticsParser` (per-account scoping via `ids` in query_signature).
- `backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py` — `AdSenseManagementParser`.
- `tests/connectors/_fixtures/README.md` — synthetic-data provenance.
- `tests/connectors/_fixtures/youtube_reporting/sample_estimated_revenue_2026_04.json` + `_rerun.json`.
- `tests/connectors/_fixtures/youtube_analytics/sample_query_response_2026_04.json` + `_rerun.json`.
- `tests/connectors/_fixtures/adsense_management/sample_earnings_report_2026_04.json` + `_rerun.json`.
- `tests/connectors/_fixtures/adsense_management/sample_payment_report_2026_04.json` + `_rerun.json`.
- Package markers (`__init__.py`) for all fixture subdirectories.

### Tests

_Per-file counts reflect the final PR state, including the review-hardening regression tests added during Codex/CodeRabbit review (see the Pytest delta below for the original vs. hardening split)._

- `tests/db/test_iso_4217_snapshot.py` — 8 tests (smoke + uniqueness + immutability guard).
- `tests/db/test_source_models.py` — 11 tests (ORM column shape + uniqueness + FKs + indexes + insert/select round-trip + report_month digit-CHECK).
- `tests/db/test_google_revenue_source_migration.py` — 2 metadata tests.
- `tests/db/_postgres_helpers.py` — fail-fast env guard.
- `tests/db/test_google_revenue_source_migration_postgres.py` — 8 PostgreSQL tests (6 migration round-trip + 1 repository upsert on the production `on_conflict_do_update` path + 1 raw_payload object-shape CHECK rejection via direct SQL).
- `tests/connectors/google_source_rows/test_currencies_repository.py` — 5 tests.
- `tests/connectors/google_source_rows/test_repository.py` — 34 tests (idempotency, tenant isolation, validation incl. NaN/non-Decimal amount, non-str guards for all required string columns, report_month-format/period-order/report_month↔period-consistency checks, id stability, raw_payload deep-copy alias safety on write + read paths, non-USD visibility; round-2 hardening: ASCII-digit report_month, nullable-text type guard, date-not-datetime period bounds, ≤6-decimal scale accept/reject, JSON-serialisable raw_payload, COALESCE provenance preservation on re-import).
- `tests/connectors/google_source_parsers/test_source_row_keys.py` — 11 tests (incl. AdSense key excludes run-specific report_id, includes currency).
- `tests/connectors/google_source_parsers/test_youtube_reporting_parser.py` — 12 tests.
- `tests/connectors/google_source_parsers/test_youtube_analytics_parser.py` — 16 tests.
- `tests/connectors/google_source_parsers/test_adsense_management_parser.py` — 10 tests (incl. source_row_key ignores run-specific report_id; round-2: missing `rows` treated as empty no-activity report).
- `tests/connectors/google_source_parsers/test_parser_failure_states.py` — 28 tests (incl. AdSense METRIC_CURRENCY header currency mismatch + missing-currency fail-closed; round-2: AdSense accountId fail-closed without `accounts/` prefix + empty id, strict YYYY-MM-DD rejecting compact `YYYYMMDD` + ISO week dates).
- `tests/connectors/test_google_source_ingestion_flow.py` — 3 tests (end-to-end + rerun + malformed-payload safety).
- `tests/finance/test_finance_no_fx_dependency.py` — 1 AST guard.
- `tests/auth/test_no_fx_permission.py` — 2 tests.
- `tests/auth/test_no_new_ingestion_permission.py` — 1 permission-set snapshot.

### Docs

- `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md` (this PR's report).
- `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-changelog.md` (this file).
- `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-handoff.md` (handoff).

## Changed

### Review hardening round 2 (Codex + CodeRabbit)

- `backend/ums_smart_revenue/connectors/google_source_rows/repository.py` — `_REPORT_MONTH_RE` switched from `\d` to `[0-9]` (ASCII, mirrors the DB CHECK; rejects Unicode digits); `_validate` extended with nullable-text type guards, `date`-not-`datetime` period rejection, `Numeric(20, 6)` ≤6-fractional-digit scale guard, and a JSON-serialisable `raw_payload` check; `upsert_many` conflict path now `COALESCE`s `raw_file_id`/`imported_by` so a provenance-less replay cannot erase audit lineage.
- `backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py` — fail closed on a `request.accountId` missing the `accounts/` prefix or with an empty id; missing/null `rows` now treated as a clean empty report (mirrors `YouTubeAnalyticsParser`).
- `backend/ums_smart_revenue/connectors/google_source_parsers/base.py` — `parse_iso_date` enforces canonical `YYYY-MM-DD` via an `isoformat()` round-trip (rejects Python 3.11+ compact `YYYYMMDD` and ISO week-date forms).
- `tests/db/_postgres_helpers.py` — `require_postgres_url` rejects whitespace-only `UMS_TEST_DATABASE_URL` and returns the trimmed value; added the standard contract block.
- `Docs/pulls/2026-05-23-pr43-…report.md` — corrected the stale `query_signature` description (excludes `metrics`; embedded as a structured JSON field, not a raw delimited string).
- `backend/ums_smart_revenue/db/alembic/env.py` — added `from ums_smart_revenue.db import source_models  # noqa: F401  # registers tables on FinanceBase`.
- `Docs/01_IMPLEMENTATION_PLAN.md` — added ✅ PR #43 sub-bullet under "Source-reported currency foundation" section.
- `Docs/15_DELIVERY_BACKLOG.md` — added ✅ PR #43 bullet under "Cross-cutting shipped".

## Removed

Nothing. Legacy `currency_exchange_rates` table, `CurrencyExchangeRateORM`, `finance/exchange_rates.py`, `api/exchange_rates.py`, `EXCHANGE_RATE_SYNCED` audit event, and the four legacy test files are all preserved per spec §6 as inert scaffolding.

## Pytest delta

- Pre-PR #43 baseline (post-PR #42 docs-only merge): 821 tests
- Post-PR #43 (original submission): 910 tests
- Post-PR #43 (review hardening round 1, Codex/CodeRabbit): 961 tests
- Post-PR #43 (review hardening round 2, Codex/CodeRabbit): 973 tests
- **Net new: +152 tests** (+89 in the original 17 new test files; +63 review-hardening regressions across existing test files).
