# PR #43 — Handoff

## Scope

Spec B1 implementation: storage + synthetic-fixture parser foundation for Google source-reported revenue. PR #42 landed the design pivot in docs; PR #43 lands the code against that locked contract. Per the plan at `Docs/superpowers/plans/2026-05-23-spec-b1-google-revenue-source-ingestion.md`, the implementation was dispatched as bounded slices (DB/migration → repository → parsers/fixtures → finance guardrails → auth guardrails → Postgres round-trip → docs) per the operator's locked execution model.

## Non-goals (preserved verbatim from spec §3)

- No `fx_rates` table.
- No `fx_locked_month_rates` table.
- No `tenants.fx_provider_settings` column.
- No `Permission.MANAGE_FX_RATES`.
- No `/fx/sync`, `/fx/rates/manual-upload`, ECB provider, exchangerate.host provider, manual CSV provider.
- No official finance calculation from public/provider FX rates.
- No locked-month FX freeze behavior.
- No frontend currency switcher.
- No paired-column migration of existing `_usd` tables.
- No live Google OAuth flow.
- No live YouTube Reporting / YouTube Analytics / AdSense Management API client.
- No live HTTP download path.
- No credential, secret, or OAuth-token handling beyond existing `connectors/credentials.py`.

## Behavior changes at runtime

- **None for existing endpoints.** No API route added or modified. The new repository + parser modules are not wired to any HTTP surface in B1 — they are consumed only by tests. B2's live connector wires them to a job runner.
- **One new Alembic head:** `20260523_0001`. Migration adds two tables and seeds reference data. Migration drops nothing.

## Tests run locally

- Full suite (with `UMS_TEST_DATABASE_URL=postgresql+psycopg://postgres:ums@localhost:55432/postgres`): **928 passed in ~100s** (910 original + 18 review-hardening regression tests added during Codex/CodeRabbit review).
- Ruff: clean across `backend` and `tests`.
- `git diff --check` (worktree + staged): clean.
- AST policy gate: passes (no `pytest.skip`/`xfail` introduced).
- PostgreSQL migration round-trip: 6/6 pass on disposable `postgres:18-alpine`.
- Pre-existing finance/api tests (`tests/api/` + `tests/finance/`): 420/420 still pass.

## Failures / skipped gates

None. Migration test fails fast (RuntimeError at collection) if `UMS_TEST_DATABASE_URL` is missing — by design, per AST policy.

## Risks

- **Fixture provenance:** fixtures must remain synthetic. Future maintainers must NOT replace any payload with real Google data without operator approval. The `tests/connectors/_fixtures/README.md` documents the discipline; the spec §3 hard non-goal is reinforced in Phase 4 of the plan.
- **`YouTubeAnalyticsParser` per-account scoping:** the `query_signature` now includes `ids` (the YouTube Analytics request-scope, e.g., `contentOwner==cms-X`). Without this, multi-CMS tenants would collapse cross-account data via the repository's `(tenant_id, source_system, source_row_key)` PK. Future parsers consuming new Google API shapes must follow the same per-scope key pattern.
- **Migration test infrastructure:** the round-trip test requires Docker + Postgres 18-alpine + an open port (55432 by default). CI environments must provision this.
- **Cross-metadata FK technique:** `GoogleRevenueSourceRowORM` imports `TenantORM` and `RawReportFileORM` to use direct `Column` references for FKs. This works because the canonical tables live in other Bases. If those models ever import from `source_models`, a circular import would surface. Today neither does. Documented inline.

## Rollback / operational notes

- `git revert <merge-commit>` then `alembic downgrade -1` restores the exact pre-merge schema. Legacy `currency_exchange_rates` is untouched, so no data is lost.
- No new permission to revoke. No new role assignment to undo. No new audit event type to clean up. No frontend to revert.
- Disposable PostgreSQL test resources: `docker stop ums-mig-pg-test` (or whichever container name the test runner used). The migration round-trip test always recreates a fresh schema; teardown is just stopping the container.

## Next-PR recommendations

1. **B2 — Live Google connector.** OAuth 2.0 flow with stored credentials, YouTube Reporting jobs listing + scheduled report download, YouTube Analytics query client, AdSense report generation polling, raw-file storage backend, Celery beat schedule. Wires the new `SourceRowParser` protocol to live payloads. Adds `connectors.run_jobs` enforcement at the job entry point.
2. **B3 — Display-only currency conversion.** Only after operators decide on the display target currency. Add an FX-rate substrate that is clearly labeled as display-only and never overwrites `amount_native`. Possibly introduces a `Permission.MANAGE_FX_RATES` at that point; the absence guard in `tests/auth/test_no_fx_permission.py` must be removed in that PR as part of introducing the permission.
3. **Paired-column migration on `_usd` tables.** Separate spec. Migrates `monthly_channel_revenue_facts`, `bank_reconciliation_entries`, `adsense_payments`, `revenue_manual_overrides` to paired `(amount_native, currency_iso4217)`. Big blast radius — 24+ files per the original PR-#42-era audit.
4. **Deferred Phase 4 NITs** — review committee noted four NITs deferred for follow-up: `build_source_row_key(**fields)` cryptic KeyError on missing field, `_canonical_dimensions` separator collision for free-text dimensions, AdSense `value_kind` dispatch at report level (per-metric would be cleaner), redundant `_require_dict` calls in AdSense `_parse_iso_date`.

## Open questions / decisions deferred

- Whether `YouTubeAnalyticsParser.parse(...)` should accept a `query_signature` override parameter to support future Google API shapes that don't include `ids` in the request envelope. Currently `ids` is required.
- Whether the future B2 connector should run the parser+upsert as a single atomic transaction or as separate per-batch transactions. Repository signature supports both — caller owns the session.
- Whether display-only conversion should be opt-in per-tenant or per-user. Deferred to B3 design.
