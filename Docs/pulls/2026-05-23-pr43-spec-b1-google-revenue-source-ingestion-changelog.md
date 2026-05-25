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
- `tests/db/test_google_revenue_source_migration_postgres.py` — 9 PostgreSQL tests (6 migration round-trip + 1 repository upsert on the production `on_conflict_do_update` path + 1 raw_payload object-shape CHECK rejection via direct SQL + 1 amount-finite guard: NaN rejected by the CHECK, +Infinity rejected by the NUMERIC type, both via direct SQL).
- `tests/connectors/google_source_rows/test_currencies_repository.py` — 5 tests.
- `tests/connectors/google_source_rows/test_repository.py` — 39 tests (idempotency, tenant isolation, validation incl. NaN/non-Decimal amount, non-str guards for all required string columns, report_month-format/period-order/report_month↔period-consistency checks, id stability, raw_payload deep-copy alias safety on write + read paths, non-USD visibility; round-2 hardening: ASCII-digit report_month, nullable-text type guard, date-not-datetime period bounds, ≤6-decimal scale accept/reject, JSON-serialisable raw_payload, COALESCE provenance preservation on re-import; round-3 hardening: Numeric(20,6) integer-digit precision accept/reject, non-string raw_payload key rejection, NaN/Infinity float rejection; round-5 hardening: zero amount with a positive exponent accepted by the integer-digit guard).
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

### Review hardening round 10

- `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py` + `…/adsense_management.py` — each yielded `ParsedSourceRow` now stores an **owned** copy of its dimensions (`dict(dim_values)`) in `raw_payload`. All monetary metrics emitted from one source/data row shared the same `dim_values` object, so a later mutation of one row's `raw_payload["dimensions"]` would have silently corrupted sibling metric rows' audit payloads (CLAUDE.md rule 4 — audit-payload isolation). Mirrors the existing per-row `deepcopy` in `youtube_reporting.py`. Cells are scalars, so a shallow `dict()` copy fully isolates each row. Tests: +2 (sibling-isolation regressions for the analytics and AdSense parsers). Deferred (not changed): a selector-vs-row-channel cross-check (conflicts with the round-7 decision to decouple the `ids` selector from the row's `channel` so `channel==MINE`/explicit selectors dedup identically) and accepting non-currency AdSense metric headers (would weaken the deliberate fail-closed header guard — rule 7).

### Review hardening round 9

- `backend/ums_smart_revenue/connectors/google_source_parsers/source_row_keys.py` + `…/youtube_reporting.py` — `currency` is now a **required** key axis for the `youtube_reporting` `source_row_key`, matching the `youtube_analytics` and `adsense_management` branches. The parser stamps a per-row `metrics.currencyCode`, but the key omitted it, so two rows identical in `report_type`/period/`dimensions` but differing in currency hashed to the **same** upsert key and silently overwrote each other (financial data loss — CLAUDE.md rule 4). `source_report_id`/`line_index` stay excluded, so backfill idempotency is unchanged (a corrected rerun carries the same currency). **Blast radius:** `source_row_key` derivation only; `google_revenue_source_rows` is new in this PR with no persisted keys (disposable pre-alpha), so no migration/backfill. Tests: +2 (a `youtube_reporting` includes-currency key case and a parser-level distinct-currency no-collision regression); the currency-required guard now covers all three source systems.

### Review hardening round 8

- `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-{changelog,report}.md` — the doc-summary bullets described the `Docs/01_IMPLEMENTATION_PLAN.md` and `Docs/15_DELIVERY_BACKLOG.md` entries with a ✅ (done) marker, but both authoritative docs deliberately record those entries as ⏳ (foundation-only — the scaffolding-only honesty rule, no live ingestion path yet). Both summaries now read ⏳ to match the docs they actually describe. Doc-only marker consistency; no code, test, or behavior change.

### Review hardening round 7

- `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py` — three follow-on guards hardening the round-6 changes:
  - **Whitespace-only `currency`** (e.g. `"   "`) now fails closed — the malformed-currency check trims before testing, and an accepted currency is stored trimmed (`"  USD  "` → `"USD"`), keeping it idempotent in the row key.
  - **Whitespace-only `ids` selector id** (e.g. `channel==   `) now fails closed — `selector_id` is trimmed before the non-empty check, and `source_account_id` stores the normalised `kind==id` selector.
  - **`source_row_key` is canonicalised against the account identity, not the raw `ids` selector.** YouTube Analytics accepts equivalent channel selectors (`channel==MINE` and `channel==<CHANNEL_ID>`) for the same channel; keying `query_signature` on the raw selector let a representation switch between runs hash identical revenue rows to different keys and insert duplicates on rerun. `query_signature` now keys on `content_owner_id` (multi-CMS-account distinctness) while the channel identity is carried by the required `dim_values["channel"]` already folded into the key. **Blast radius:** `source_row_key` derivation only — no DB/model/migration change, no live persisted data at this phase (disposable pre-alpha), and the cross-account distinctness the raw selector protected is preserved by `content_owner_id`. Added a `channel==MINE`-vs-explicit-id key-equality regression test.
- Tests: +5 cases (whitespace `currency`/`ids` rejections, a trimmed-currency acceptance, and the selector-canonicalisation regression).

### Review hardening round 6

- `backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py` — `_SETTLED_METRICS` no longer includes `UNPAID_AMOUNT`; only `PAID_AMOUNT` is settled, matching the module contract. `UNPAID_AMOUNT` is outstanding (not-yet-paid) earnings, so it now correctly emits `value_kind='estimated'`/`report_type='earnings_report'` instead of being mislabeled as settled cash, which would have corrupted finalized-vs-estimated and payment-reconciliation totals (CLAUDE.md rule 4). Added `test_unpaid_amount_is_classified_as_estimated`.
- `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py` — the parser now fails closed on a malformed `query_request.ids` selector. The YouTube Analytics contract requires a structured `channel==<id>` or `contentOwner==<id>` value; arbitrary/unprefixed text or a bare `contentOwner==`/`channel==` with an empty id previously persisted a revenue row whose `source_account_id`/`content_owner_id` could not be tied back to a real owner or channel. This mirrors the AdSense parser's `accountId` prefix/suffix guard. Added 6 parametrized failure-state cases and a positive `channel==`-scoped acceptance test.
- `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py` — `currency` is now treated as an **optional** `reports.query` request parameter. The YouTube Analytics API defaults financial metrics to USD when `currency` is omitted, so an omitted `currency` now defaults to `USD` and ingests instead of raising `ParserError`, fixing avoidable failures for otherwise-valid replay payloads that drop the optional parameter. The default applies **only on true omission**: a present-but-malformed value (null / empty / non-string) still fails closed as a typed `ParserError`. `build_source_row_key` continues to receive a concrete currency (now defaulted), so distinct-currency key separation is preserved. Added `test_missing_currency_defaults_to_usd` plus 3 parametrized malformed-currency cases.

### Review hardening round 5 (Codex P1 + P2)

- `tests/db/_postgres_helpers.py` + `tests/db/test_google_revenue_source_migration_postgres.py` — moved the `UMS_TEST_DATABASE_URL` resolution out of module import time into a `postgres_url` pytest fixture (Codex P1). Computing it at import made the whole `pytest -q` suite abort during *collection* (`Interrupted: 1 error during collection`, 0 tests run) on any machine/CI without Postgres, because pytest imports every test module up front. It now raises the same `RuntimeError` — never a skip, so the AST policy gate still holds — only when the Postgres suite's fixtures run, so the failure is localised to that suite and every unrelated test still collects and runs.
- `backend/ums_smart_revenue/connectors/google_source_rows/repository.py` — `_validate` now exempts exact zero from the ≤14 integer-digit guard (Codex P2). A zero with a positive exponent (e.g. `Decimal('0E+20')`, which `parse_decimal_amount` can yield) has `adjusted()==20` and was wrongly rejected even though it is exactly 0 and fits `Numeric(20,6)`; `is_zero()` is True for any zero regardless of exponent. Added `test_validate_accepts_zero_amount_with_positive_exponent`.

### Review hardening round 4 (Codex P1 + CodeRabbit)

- `backend/ums_smart_revenue/connectors/google_source_parsers/youtube_analytics.py` + `…/base.py` — accept numeric `reports.query` metric cells (Codex P1). `reports.query` columns are typed `FLOAT`/`INTEGER` per `columnHeaders[].dataType` and arrive as JSON numbers, so the prior str-only guard raised `ParserError` on valid YouTube Analytics revenue reports. `parse_decimal_amount` now normalises `str | int | float` → `Decimal` (floats routed through `str()` to avoid binary-float artifacts; `bool` and non-finite `NaN`/`Infinity` rejected) and the str-only guard in `youtube_analytics.py` is removed. AdSense (string report cells) and YouTube Reporting (CSV) parsers keep their str guards by design — only `reports.query` emits typed JSON numbers.
- `backend/ums_smart_revenue/connectors/google_source_parsers/source_row_keys.py` — `currency` is now a **required** key (bracket access) in both currency-keyed branches (`youtube_analytics`, `adsense_management`) instead of `fields.get("currency")` (CodeRabbit). A caller that omits currency now fails closed with `KeyError` rather than silently hashing `None` and collapsing distinct-currency rows onto one upsert key (CLAUDE.md rule 4). `filters` stays optional. Both production parsers already pass `currency`; five `test_source_row_keys.py` cases were updated and a `test_currency_is_required_for_currency_keyed_branches` regression added.

### Review hardening round 3 (Codex P2)

- `backend/ums_smart_revenue/connectors/google_source_rows/repository.py` — `_validate` now also bounds the **integer** part of `amount_native` (≤14 integer digits, completing the `Numeric(20,6)` envelope alongside the round-2 ≤6-fractional scale guard) so an oversized value fails as a typed error, not a raw PG numeric overflow; `raw_payload` validation now rejects **non-string keys** (recursive `_require_str_keys`, since `json.dumps` silently coerces them) and **NaN/Infinity** floats (`json.dumps(allow_nan=False)`, which PG JSONB rejects).
- `backend/ums_smart_revenue/db/source_models.py` + `…/alembic/versions/20260523_0001_…py` — added Postgres-only CHECK `ck_google_revenue_source_rows_amount_finite` (`amount_native < 'Infinity'::numeric`) so a direct-SQL/backfill writer cannot persist a NaN amount (which `>= 0` admits); ±Infinity is already blocked by the `NUMERIC(20,6)` type. ORM constraint is `ddl_if(dialect="postgresql")` so the SQLite test metadata is unaffected.
- Deferred (scope, not bugs): Codex suggested the YouTube Analytics parser accept dimensionless / non-`channel` queries. B1 is intentionally channel-scoped and **fails closed** (loud `ParserError`, no silent loss) on other shapes; supporting CMS-aggregate analytics is a deliberate B2 (live-connector) decision. Recorded under Remaining risks in the report.

### Review hardening round 2 (Codex + CodeRabbit)

- `backend/ums_smart_revenue/connectors/google_source_rows/repository.py` — `_REPORT_MONTH_RE` switched from `\d` to `[0-9]` (ASCII, mirrors the DB CHECK; rejects Unicode digits); `_validate` extended with nullable-text type guards, `date`-not-`datetime` period rejection, `Numeric(20, 6)` ≤6-fractional-digit scale guard, and a JSON-serialisable `raw_payload` check; `upsert_many` conflict path now `COALESCE`s `raw_file_id`/`imported_by` so a provenance-less replay cannot erase audit lineage.
- `backend/ums_smart_revenue/connectors/google_source_parsers/adsense_management.py` — fail closed on a `request.accountId` missing the `accounts/` prefix or with an empty id; missing/null `rows` now treated as a clean empty report (mirrors `YouTubeAnalyticsParser`).
- `backend/ums_smart_revenue/connectors/google_source_parsers/base.py` — `parse_iso_date` enforces canonical `YYYY-MM-DD` via an `isoformat()` round-trip (rejects Python 3.11+ compact `YYYYMMDD` and ISO week-date forms).
- `tests/db/_postgres_helpers.py` — `require_postgres_url` rejects whitespace-only `UMS_TEST_DATABASE_URL` and returns the trimmed value; added the standard contract block.
- `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md` — corrected the stale `query_signature` description (excludes `metrics`; embedded as a structured JSON field, not a raw delimited string).
- `backend/ums_smart_revenue/db/alembic/env.py` — added `from ums_smart_revenue.db import source_models  # noqa: F401  # registers tables on FinanceBase`.
- `Docs/01_IMPLEMENTATION_PLAN.md` — added ⏳ PR #43 sub-bullet under "Source-reported currency foundation" section.
- `Docs/15_DELIVERY_BACKLOG.md` — added ⏳ PR #43 bullet under "Cross-cutting shipped".

## Removed

Nothing. Legacy `currency_exchange_rates` table, `CurrencyExchangeRateORM`, `finance/exchange_rates.py`, `api/exchange_rates.py`, `EXCHANGE_RATE_SYNCED` audit event, and the four legacy test files are all preserved per spec §6 as inert scaffolding.

## Pytest delta

- Pre-PR #43 baseline (post-PR #42 docs-only merge): 821 tests
- Post-PR #43 (original submission): 910 tests
- Post-PR #43 (review hardening round 1, Codex/CodeRabbit): 961 tests
- Post-PR #43 (review hardening round 2, Codex/CodeRabbit): 973 tests
- Post-PR #43 (review hardening round 3 — Numeric precision + raw_payload key/finite guards + DB finite CHECK): 978 tests
- Post-PR #43 (review hardening round 4 — numeric `reports.query` metric-cell acceptance + `currency` required in source_row_key): 983 tests
- Post-PR #43 (review hardening round 5 — zero-amount integer-digit guard + Postgres-helper fixture relocation): 984 tests
- Post-PR #43 (review hardening round 6 — UNPAID_AMOUNT classified estimated + fail-closed on malformed Analytics `ids` selector + optional Analytics `currency` defaults to USD): 996 tests
- Post-PR #43 (review hardening round 7 — whitespace-only `currency`/`ids` rejection + `source_row_key` canonicalised against account identity for channel-selector idempotency): 1001 tests
- **Net new: +180 tests** (+89 in the original 17 new test files; +91 review-hardening regressions across existing test files).
