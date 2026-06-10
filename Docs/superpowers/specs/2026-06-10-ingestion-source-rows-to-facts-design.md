# Ingestion → revenue facts: wire the C1 normalizer into production

**Date:** 2026-06-10
**Branch:** `feat/ingestion-source-rows-to-facts` (off main `e92efd2`, #88)
**Status:** Approved direction (Mahmoud: "proceed", recommendations pre-approved)

## Problem

A live connector ingest persists `google_revenue_source_rows` but never produces
`MonthlyChannelRevenueFactORM`. `GoogleSourceNormalizer.normalize_month`
(`finance/google_source_normalizer.py:189`) is the only code that collapses source rows into
revenue facts, and it has **no production caller** — it is invoked only by tests. So the dashboard,
allocation engine, reconciliation (Track F), net-revenue, and exports — all of which read
`MonthlyChannelRevenueFactORM` — see nothing from a real connector run. The ingest pipeline is
complete up to source rows, then the finance-fact projection step is orphaned.

## Goal

Trigger `normalize_month` automatically at the end of a successful connector run, so a real ingest
produces revenue facts, with rock-solid fail-closed behavior around month locks and zero collision
with other fact writers. No schema change, no new permission, no migration.

## Non-goals (explicitly deferred)

- **`POST /connectors/jobs` executing path.** `run_one` is synchronous and network-bound (live
  Google API + OAuth + blob I/O); an API-triggered pull needs background/async execution
  infrastructure and a `report_month` field on `ConnectorJobRequest`. That is a materially larger,
  independent change → its own follow-up spec. This PR keeps ingestion triggered only by the
  existing out-of-band CLI/orchestrator path.
- Stale-fact cleanup (a channel/source_kind that disappears from source rows). Out of scope; C1's
  key isolation means it never collides with other writers, and deleting facts is a separate
  reconciliation concern.
- ADSENSE-kind facts (AdSense rows carry no `youtube_channel_id` today → skipped as
  MISSING_CHANNEL_ID; channel allocation for AdSense is a future spec).

## Design

### Integration point
Inside `connectors/runs/orchestrator.py`, after the live run finishes and commits, gated on the
run having reached a terminal **SUCCEEDED** or **PARTIAL** status. The normalize stage runs **once
per `(tenant, report_month)`** — not per report — because `normalize_month` reads ALL source rows
for the month (`google_source_normalizer.py:233-235`) and must run after every per-report commit
(`:846`) and after the analytics deferred-cleanup flush (`:718-722`).

`run_one(session, *, tenant_id, connector_key, account_id, report_month, dry_run, triggered_by_user_id)`
already holds everything the normalize call needs: `session`, `tenant_id` (UUID), `report_month`
(`YYYY-MM`), and the actor.

### Locked-month policy (fail-closed, never overwrite locked facts)
1. **Prefilter:** before normalizing, call `get_month_close_status(session, report_month,
   tenant_id=tenant_id)` (`month_close.py:206`, a pure SELECT — no row creation, no lock
   contention). If `== "LOCKED"`, **skip** the normalize stage, log it, leave the run status
   unchanged. A connector legitimately ingesting late data for a locked month must not fail the run
   or mutate locked facts.
2. **Hard guarantee (defense-in-depth, already present):** `normalize_month` raises
   `RevenueFactLockedMonthError` upfront (`:214-231`) and `record_fact._require_month_open`
   re-checks per write (`revenue_facts.py:144,328-341`). The wiring catches
   `RevenueFactLockedMonthError` and treats it as a skip — a lock acquired between the prefilter and
   the write can never produce a written locked-month fact, and must never flip a SUCCEEDED run to
   FAILED.

### Idempotency
Keep C1's existing behavior unchanged. It upserts keyed on
`uq_monthly_channel_revenue_source (tenant_id, month, youtube_channel_id, source_kind)` and
classifies CREATED / UPDATED / UNCHANGED via actor-insensitive payload comparison
(`:95-117, :373-415`). A re-ingest is harmless: identical rows → UNCHANGED (no `updated_at`
churn), changed amounts → UPDATED. No fact-level delete is introduced.

### Actor
Use `triggered_by_user_id` when present; otherwise fall back to the connector **service principal**
(the same actor used for the run's audit rows, env `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID`,
already resolved in the live path). This keeps fact `imported_by` attributable to the same actor as
the run.

### Failure handling
- `dry_run` → no normalize (no committed source rows; `run_one` returns no run).
- run status FAILED → no normalize (no new committed source rows worth projecting; prior month
  state intact).
- normalize raising a non-lock error (e.g. an unknown/inactive channel) must be surfaced
  (logged + propagated or attached to the run outcome) — it indicates a real data problem, not
  swallowed. A locked-month error is the only caught-and-skipped case.

## Blast-radius review (finance / DB)

- **Tables/models:** `MonthlyChannelRevenueFactORM` — WRITE only, via the existing `record_fact`
  upsert. **No schema change, no migration.** Reads `google_revenue_source_rows`,
  `youtube_channels`, `finance_month_close` (all existing).
- **PostgreSQL remains the source of truth.** Yes.
- **Could migrations/tests/seed/docs break?** No migration. Existing normalizer + ingestion-gate
  tests preserved; new wiring test added. Seed script (`seed_demo_month.py`) writes facts directly
  and is demo-only — unaffected.
- **Neo4j:** `No graph projection impact detected.` (facts are operational PostgreSQL state; the
  normalize step uses existing repositories only.)
- **Authorization more permissive?** No. No auth change. The connector run path already gates
  `RUN_CONNECTOR_JOBS` at its entry; normalize is an internal post-run step.
- **Finance results / locks / overrides change?** Facts are now produced from ingested source rows
  (the intended effect). Locked months are never overwritten (prefilter skip + existing
  `record_fact` fail-closed guard). ALLOCATION and MANUAL_UPLOAD facts use disjoint `source_kind`
  and are untouched.
- **Backward compatible?** Yes — purely additive. A real ingest now also produces facts; it
  previously produced none.
- **Rollback:** code-only revert; no migration, no data reset.

## Tests

- End-to-end (extend the `test_ingestion_gate.py` pattern): run the connectors via `run_one`, then
  assert revenue facts exist **after `run_one` returns** (no separate manual `normalize_month`
  call) — the YOUTUBE_CMS + YOUTUBE_ANALYTICS facts the gate already expects.
- Wiring unit tests: SUCCEEDED/PARTIAL → normalize invoked; dry-run → not invoked; FAILED → not
  invoked; LOCKED month → skipped (no facts written, run stays SUCCEEDED); actor fallback to the
  service principal when `triggered_by_user_id` is None.
- Preserve all existing `test_google_source_normalizer_*` and orchestrator guarantees.
