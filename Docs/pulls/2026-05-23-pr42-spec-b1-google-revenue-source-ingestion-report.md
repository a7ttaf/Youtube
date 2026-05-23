# PR #42 — Spec B1 Pivot: Google Source-Reported Revenue Ingestion (docs-only)

**Date:** 2026-05-23
**Branch:** `docs/spec-b1-google-source-ingestion`
**Base:** `main` at `a9d8889` (PR #41 merge)
**Status:** Docs-only. No backend code, no tests added or removed, no migration files. PR #43 implements against this locked contract after merge.

## Summary

Replace the FX-rate-led Spec B1 approach with **Google source-reported revenue ingestion**. YouTube/Google/AdSense source-reported amounts become the official finance source; market FX rates are display-only and out of B1 scope.

## What was requested

After the earlier brainstorming sessions reached Section 4 of the FX-storage-foundation design, the operator pivoted B1 to a different cut: in markets with unstable currency rates (notably Egypt-facing workflows), deriving official monthly revenue from a public FX feed or a manual rate table creates numbers that can disagree with Google settlement and bank evidence. The fix is to make Google-reported money authoritative at storage time and preserve native currency exactly.

PR #42 lands the design pivot in docs (9 active doc edits, 1 new design spec, 1 expanded TDD implementation plan). PR #43 will implement against the locked contract.

## What was actually done

### Active doc edits (9)

| File | Change |
|---|---|
| `Docs/18_MULTI_CURRENCY_ENGINE.md` | Rewritten as "Source-Reported Currency Policy". Pins: Google-reported money is authoritative; FX rates are not an official finance source; month close freezes source evidence; no manual rate workflow in B1. |
| `Docs/01_IMPLEMENTATION_PLAN.md` | Replaced the "Multi-currency engine" bullet with "Source-reported currency foundation" pointing at the revised Docs/18. Notes that current `currency_exchange_rates` scaffolding is legacy/inert. |
| `Docs/05_CONNECTORS_YOUTUBE_ADSENSE.md` | Adds Google source-row preservation contract: connectors must preserve reported currency and source evidence; `google_revenue_source_rows` listed as the next ingestion foundation table. |
| `Docs/12_BACKEND_API_SPEC.md` | `/exchange-rates` re-labeled as "legacy scaffold; not official finance source". Money-API contract updated: every endpoint returns source currency, confidence, and source metadata; currency parameters allowed only where implemented; display-only conversion must label the conversion as non-official. |
| `Docs/13_SQL_DATA_MODEL.md` | Adds `google_revenue_source_rows` schema inline next to existing tables. Updates SQL comments to mark `currency_exchange_rates` as legacy. |
| `Docs/15_DELIVERY_BACKLOG.md` | Repositions automated currency rate integration as display-only foundation. Reframes bank/transfer/FX data source as "bank/transfer/local-currency variance evidence". |
| `Docs/16_OPEN_DECISIONS.md` | Replaces FX-provider authority questions with Google-source authority questions (channel/account/payment/metric currency precedence; YouTube-vs-AdSense disagreement; bank-side variance). |
| `Docs/17_MULTI_TENANT_ARCHITECTURE.md` | Drops `tenants.fx_provider_settings` from the tenant schema. `primary_currency` typed as `TEXT` with explicit format check. |
| `Docs/10_EXPORTS_BRAND_REPORTS.md` | Exports must not derive money from market FX rates; non-USD exports must be backed by source-reported currency evidence or clearly labeled as display conversion. |

### New design spec (260 lines)

`Docs/superpowers/specs/2026-05-23-spec-b1-google-revenue-source-ingestion-design.md`

Sections: problem statement, goals, non-goals, data model (`currencies` + `google_revenue_source_rows`), connector flow (parser-only, no live OAuth/API client), legacy exchange-rate scaffold preservation, authorization (reuse existing connector/finance boundaries — no new permission), blast radius, testing inventory.

### New implementation plan (4,414 lines, 45 tasks, 10 phases)

`Docs/superpowers/plans/2026-05-23-spec-b1-google-revenue-source-ingestion.md`

Plan structure mapped to operator-locked subagent dispatch boundaries:

| Slice | Phases |
|---|---|
| DB/migration | 1 (ISO 4217 + `currencies`), 2 (`google_revenue_source_rows` ORM + migration), 8 (Postgres round-trip) |
| Repository | 3 (`SqlAlchemyCurrenciesRepository` read-only + `SqlAlchemyGoogleRevenueSourceRowRepository` storage primitives) |
| Parsers/fixtures | 4 (protocol + `source_row_keys` + 3 parsers + synthetic fixtures), 5 (end-to-end ingestion flow test) |
| Finance guardrails | 6 (finance modules outside legacy do not consume `CurrencyExchangeRateORM`; repository-boundary non-USD visibility) |
| Auth/docs/validation | 7 (no `MANAGE_FX_RATES`; parser-skeleton typed-error failure-state contract; permission-set snapshot), 9 (PR #43 docs/pulls + Docs/01/15 marks), 10 (validation gate + push prep) |

Key design decisions baked into the plan:
- ORM home: `backend/ums_smart_revenue/db/source_models.py` on `FinanceBase`; `alembic/env.py` adds `from ... import source_models  # noqa: F401`.
- Migration filename: `20260523_0001_google_revenue_source_foundation.py`, `down_revision = "20260521_0001"`.
- Repository package: `backend/ums_smart_revenue/connectors/google_source_rows/`.
- Parser package: `backend/ums_smart_revenue/connectors/google_source_parsers/`.
- Fixture location: `tests/connectors/_fixtures/{youtube_reporting,youtube_analytics,adsense_management}/` — **fully synthetic, no real Google account IDs / channel private data / revenue figures / OAuth payloads / credentials**.
- `source_row_key` derivation: full 64-char SHA-256 hex digest of a source-system-specific canonical string. Repository validates length == 64 at write boundary.
- Repository signature: `upsert_many(tenant_id, rows: Iterable[ParsedSourceRow], *, raw_file_id: UUID | None, imported_by: UUID | None)` with dialect-insert helper for SQLite + PostgreSQL `ON CONFLICT DO UPDATE`.

## What this PR does NOT change

- **No backend code.** No `.py` file in `backend/` is touched.
- **No tests added or removed.** `tests/` tree is unchanged.
- **No migration files.** `backend/ums_smart_revenue/db/alembic/versions/` is unchanged.
- **No frontend code.** `frontend/` is unchanged.
- **No CI workflow change.**
- **No legacy scaffolding deletion.** `currency_exchange_rates` table, `CurrencyExchangeRateORM`, `finance/exchange_rates.py`, `api/exchange_rates.py`, the `EXCHANGE_RATE_SYNCED` audit event, and the four legacy test files are all preserved per spec §6 — they are inert scaffolding, not the official finance source.

## Quality checks performed

- `git diff --check --cached` — clean (stripped trailing blank lines from the new spec and plan files before commit).
- `git diff --check` — clean.
- `rg -n "MANAGE_FX_RATES|fx_rates|fx_locked_month_rates|fx_provider_settings" Docs` — 30 hits audited:
  - 28 in PR #42 files: all explicitly framed as non-goals, deferred work, or guardrails. Examples: Docs/18's "No manual rate workflow in B1" section, Docs/12's "must not be expanded into MANAGE_FX_RATES" legacy-scaffold guardrail, spec §3 non-goals list, plan non-goals list + Phase 7 guard tests (`test_manage_fx_rates_permission_does_not_exist`).
  - 2 in pre-existing main content (PR #41 handoff/report) — not touched by PR #42.
- Diff confirmed against `origin/main`: exactly 11 files changed (`git diff origin/main --name-only`), matching the staged set.

## Blast radius

*No graph projection impact detected.* Neo4j was retired in PR #12.

- Authorization: unchanged. No permission added, removed, or modified. Existing `connectors.run_jobs` covers ingestion auth per plan Phase 7.
- Finance: unchanged. No `_usd` column touched. No finance service modified. PR #43's guard test (`tests/finance/test_finance_no_fx_dependency.py`) will pin the contract that finance modules outside `finance/exchange_rates.py` do not consume `CurrencyExchangeRateORM`.
- Audit: unchanged. No new audit event type. `EXCHANGE_RATE_SYNCED` preserved as legacy scaffold.
- Reports/exports: unchanged. Docs/10 documents the future constraint that exports must not derive money from market FX rates, but no exports code changes here.
- Database schema: unchanged at runtime. Schema changes are described in the spec/plan but not yet executed in any migration file.
- Frontend: unchanged. No `frontend/src/` files touched.

## Remaining risks

- **Pre-merge:** This PR depends on the operator merging it before PR #43 opens. PR #43's Phase 0 includes a `gh pr view 42 --json state` check that fails if PR #42 is not yet merged.
- **Post-merge:** If a downstream reader misreads the spec's "fixtures must be synthetic" clause and commits a real Google payload to `tests/connectors/_fixtures/` during PR #43 implementation, that would be a leak risk. The plan's Phase 4 carries an explicit hard-non-goal block reminding implementers; reviewer must enforce.

## Rollback notes

Pure docs revert: `git revert <merge-commit>` restores the prior state of the 9 modified docs and removes the 2 new spec/plan files. No code, schema, or runtime state to roll back. No data migration to reverse.

## Follow-up recommendation

After PR #42 merges:
1. **PR #43** — Implement Spec B1 against the merged spec + plan. Tracked as Phase 0-10 in the plan file. Branch name from plan Phase 0: `pr/spec-b1-google-revenue-source-ingestion`. Estimated PR size: ~30+ commits across 10 phases, all TDD-shaped per the subagent-driven-development skill.
2. Future specs (post-B1): live Google connector (OAuth + API client + download path) building on B1's parsers; display-only currency conversion as a separate spec; paired-column migration of existing `_usd` finance tables as another separate spec.
