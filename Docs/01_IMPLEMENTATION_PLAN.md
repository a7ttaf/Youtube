# Implementation Plan

## Status (2026-06-13)

Mainline merge status is reconciled through PR #97 (executor Bucket-A audit RLS
fix). Mainline integration was reconciled through PR #36 (S2 multi-tenant
integration merged onto `main` at commit `96dbe73`), and the roadmap notes
now include the stacked PR #47 Google live connector foundation state. The
original Phase 0–7 product cut below is the durable roadmap; status markers
(`✅ / ⏳ / 🗑️`) show what has shipped end-to-end, what is partial, and what
has been removed from scope. The new section "Cross-cutting infrastructure
(Sx)" tracks the platform-level work delivered outside the product phase
numbering (backend foundation, governance/CI, multi-tenant stack).

**Marker conventions** (also applied across `15_DELIVERY_BACKLOG.md`):

- `✅ PR #N` — item is shipped end-to-end at the layer being marked.
- `⏳ PR #N — remaining: <note>` — partial; concrete remaining work is
  named.
- `🗑️ removed in PR #N — <reason>` — explicitly dropped from scope.

Honesty rule: scaffolding-only items (ORM + repo + tests but no real
ingestion / UI / user-facing path) are marked `⏳`, not `✅`. The
remaining-note explains what would lift them to `✅`.

## Goal

Deliver a smart internal revenue engine for 300+ YouTube channels, focused
first on monthly channel/company/sector numbers.

## Build rule

Do not start with a normal dashboard. Start with the **data and calculation
engine**, then expose it through a dashboard.

---

## Cross-cutting infrastructure (Sx — 2026-05-10 → 2026-05-21)

Platform-level tracks delivered outside the Phase 0–7 product numbering. All
of this is required for any phase below to work end-to-end.

### S0 — Backend foundation (2026-05-10 → 2026-05-15)

- ✅ PR #1: Backend skeleton (FastAPI + SQLAlchemy + Alembic + pytest baseline).
- ✅ PR #2: Revenue facts foundation APIs.
- ✅ PR #3: User role assignment APIs.
- ✅ PR #4: Direct permission grant APIs.
- ✅ PR #5: Database-backed principal authorization mode.
- ✅ PR #6: User account lifecycle APIs.
- ✅ PR #7: User access read APIs.
- ✅ PR #8: Block month-close on missing required revenue facts.
- ✅ PR #9: Guarded finance export artifacts (Excel scaffolding).
- ✅ PR #10: Frontend preview shell — follow-through shipped: API client +
  `X-UMS-Tenant` header wiring (PR #41); real dashboard pages wired to live
  APIs (PR #69: Command Center + smart alerts, Close, Trace/Explain, Exports,
  Connectors; the Audit page is wired to `GET /audit/events` (PR #71,
  31a7641) and Registry to `GET /channels` (PR #73/#78), so all dashboard
  pages are wired to live APIs).
- ✅ PR #95 — Connector-jobs executor: `POST /connectors/jobs` now EXECUTES
  (submits a real ingest pull to a bounded in-process `ConnectorJobExecutor`,
  returns 202 `submitted`; the old `recorded_not_executed` no-op is retired).
  Fail-closed `connector_job_executor_enabled` setting (default OFF -> 503),
  in-process + DB duplicate guard with stale-orphan supersede, one route-owned
  audit row, a worker Bucket-A `job_failed_before_start` audit, and a frontend
  "Run pull" control. Part 2 adds four `api_connector_credentials`
  refresh-telemetry columns + CHECK (migration `20260612_0001`) stamped at the
  single `resolve_connector_credentials` chokepoint. Prerequisite PR #94
  (ingestion RLS lane fix) made the merged ingest->normalize pipeline executable
  on RLS-enforced Postgres (the `db.lane.platform_lane` elevation). See
  `Docs/15_DELIVERY_BACKLOG.md` for the full scope + deferrals.
- ✅ PR #96 — Alembic env.py URL-precedence hardening: an ambient
  `UMS_DATABASE_URL` can no longer silently override an in-code-injected
  `sqlalchemy.url`, closing the wrong-DB footgun for migration round-trip tests.
  The precedence logic is extracted to `db/migration_url.py::resolve_database_url`
  (re-reads the ini on-disk url; differing configured value wins; ini-placeholder
  preserves the prod env-var-wins contract). No schema change.
- ✅ PR #97 — Executor Bucket-A audit RLS fix: post-#95 reverts dropped the
  `TENANT_CTX` minimal-tenant set in `executor.py::_audit_failed_before_start`,
  so `app_current_tenant_id()` was NULL and the `audit_logs`
  `WITH CHECK (tenant_id = app_current_tenant_id())` RLS policy denied the
  Bucket-A failure-audit INSERT on Postgres (red gate
  `test_bucket_a_audit_persists_under_platform_lane_on_postgres`). Restored the
  fabricated-`Tenant` contextvar bridge (id-only; reset via `finally`) so the
  `after_begin` hook writes the trusted context row and the INSERT satisfies the
  policy. SQLite tier unaffected (RLS not enforced there).

### S1 — Governance, infra, scope freeze (2026-05-16)

- ✅ PR #11: Governance & quickstart docs.
- 🗑️ removed in PR #12 — Neo4j graph component retired entirely;
  PostgreSQL is now the sole source of truth.
- ✅ PR #13: Trusted-gateway token hardening in conftest.
- ✅ PR #14: CI gate (Elite-CI vendored) + Dependabot config.
- ✅ PR #15: Dockerfile + docker-compose stack.
- ✅ PR #16: Multi-tenant + multi-currency architecture docs
  (`Docs/17_MULTI_TENANT_ARCHITECTURE.md`,
  `Docs/18_MULTI_CURRENCY_ENGINE.md`).
- ✅ PR #17: uvicorn 0.46 → 0.47 (Dependabot).

### S2 — Multi-tenant stack (2026-05-16 → 2026-05-21)

- ✅ PR #18: `tenants` + `platform_admins` tables (S2.1).
- ✅ PR #19: Header resolver + `TENANT_CTX` contextvar + tenant repository
  (S2.2).
- ✅ PR #20: `UserPrincipal.tenant_id` + `PLATFORM_ADMIN` principal (S2.3).
- ✅ PR #21: `tenant_id` column on 18 operational tables, default UMS
  (S2.4a).
- ✅ S2.4b tenant-scope stack (PRs #22 – #34): audit log, user accounts,
  roles & grants, org channel registry, AdSense payment repo, bank
  reconciliation repo, channel group registry, number explanations, raw
  report files, credentials repo — all tenant-scoped with direct repository
  tests; plus full ruff cleanup (PR #27, 652 → 0) and gitignore backport
  (PR #28).
- ✅ PR #35: S2 integration plan + recovery spec + execution plan documents.
- ✅ PR #36: S2 stack merged onto `main` (commit `96dbe73`) with resolver
  admission control, trusted-gateway tenant middleware, bootstrap UMS
  tenant, cross-tenant principal binding hardened, and tenant-scoped
  export jobs. The remaining frontend `X-UMS-Tenant` header wiring +
  tenant-aware API client shipped in PR #41 and is consumed by the wired
  dashboard pages in PR #69.

### S0/S1 catch-up (2026-05-22)

Operational artifacts that were used through S0/S1/S2 but never previously
committed to git. PR #38 brings the repository in sync with what was actually
running on the operator's workstation.

- ✅ PR #38: Local validation gate (`scripts/run_validation_gate.py` invokes
  `backend/ums_smart_revenue/devtools/quality_gate.py` which runs ruff
  on `backend` + `tests` + `scripts`, the AST-based no-skip/xfail policy
  gate, the full pytest suite with `--strict-config --strict-markers
  --basetemp .pytest-tmp`, and `git diff --check` on both working tree
  and staged).
- ✅ PR #38: Developer agent rules — `AGENTS.md` (Codex-facing, committed)
  and `.agents/skills/` (postgresql-table-design + vitest) with
  `skills-lock.json` pinning. The Claude-Code-local `CLAUDE.md` stays
  gitignored as the per-machine copy.
- ✅ PR #38: Pre-integration state log
  (`Docs/superpowers/runlog/2026-05-21-phase-0.md`) and completion of
  the S2 integration runlog `§3.7/§3.8/§3.9`
  (`Docs/superpowers/runlog/2026-05-21-phase-4.md`).
- ✅ PR #38: `.gitignore` adds `Docs/Youtube Project/` (local Obsidian
  vault) and `.vite/` (Vite dev cache).
- ✅ PR #39: Soft-dark mockup variant + OFL fonts —
  `mockups/ums-smart-revenue-command-center-soft-dark.html`,
  `mockups/qa/ums-command-center-soft-dark-*.png` (9 QA screenshots),
  `mockups/qa/generate-screenshots-soft-dark.py`, and `mockups/FontsGH/`
  (Mona Sans + Monaspace Neon + Newsreader OFL-1.1 fonts with license
  notices and README). Redistributable sibling to the canonical
  Anthropic-licensed mockup; referenced from
  `Docs/09_SMART_DASHBOARD_UI.md` as a visual target for Phase 5.
- ✅ PR #41 — Spec A frontend `X-UMS-Tenant` header foundation. Backend `GET /tenants/me` proof endpoint depending on the existing `TenantResolverMiddleware` + `current_principal_from_headers`; React `TenantContext` seeded with bootstrap slug `"ums"`; `useApiClient()` thin fetch wrapper that sets `X-UMS-Tenant` last (cannot be overridden); AppShell dev-only proof tag; Vite dev proxy injecting trusted-gateway headers from Node env; Vitest framework wired into the local validation gate. Closes S2 spec Phase 5.

### Sx — Specced but not yet started

- **S3 — Tenant hardening at storage layer.**
  `Docs/17_MULTI_TENANT_ARCHITECTURE.md` specifies row-level security
  (RLS) + Postgres GUC + composite foreign keys + tenant-scoped unique
  keys + `app_tenant` / `app_platform` Postgres roles.
    - ✅ Track E (2026-06-08): **RLS enforcement DONE.** Migration
      `20260608_0001` creates the `app_tenant`/`app_platform` roles and an
      isolation policy on all 25 tenant-scoped tables. The tenant context is
      held in an `app_tenant_context` table keyed by `backend_pid` (NOT a
      Postgres GUC): a SECURITY DEFINER `set_app_current_tenant_id(uuid)` writes
      the row, the RLS policies read it through `app_current_tenant_id()`, and a
      SECURITY DEFINER `clear_app_current_tenant_id()` (migration
      `20260609_0002`) clears it — the app lanes hold only SELECT on the table,
      so a tenant lane cannot forge its own context. A single-pool
      `SET LOCAL ROLE` realization: a Postgres-only, context-gated `after_begin`
      hook in `db/session.py` first switches to `app_platform`, writes/clears the
      trusted context row, then switches a tenant-lane session to the restricted
      `app_tenant` role (fail-closed: missing context => no rows). Plus
      `build_platform_session_factory` and the `assert_tenant_match` write-path
      helper. Composite FKs / tenant-scoped unique keys / `FORCE ROW LEVEL
      SECURITY` remain follow-ups.
- **Source-reported currency foundation.**
  `Docs/18_MULTI_CURRENCY_ENGINE.md` was revised on 2026-05-23 to make
  Google/YouTube/AdSense reported money the official finance source. The next
  B1 storage cut should preserve source amounts, currencies, report identity,
  raw payload references, and idempotent Google source row keys. Existing
  `currency_exchange_rates` scaffolding is legacy/inert and must not become
  the official finance source.
    - ⏳ PR #43 (foundation only): storage + synthetic-fixture parsers for
      Google revenue source ingestion. `currencies` + `google_revenue_source_rows`
      tables on `FinanceBase`, repository with idempotent `upsert_many`, parsers
      for YouTube Reporting / YouTube Analytics / AdSense Management. Legacy
      `currency_exchange_rates` preserved as inert scaffolding. PostgreSQL
      migration round-trip on disposable `postgres:18-alpine`. Remaining: live
      OAuth/API connector (B2), FX/conversion (B3) — scaffolding-only, so marked
      ⏳ not ✅.
    - ⏳ PR #44 - Google source-rows -> revenue facts normalizer (Spec C1).
      Adds `backend/ums_smart_revenue/finance/google_source_normalizer.py`,
      five new test files under `tests/finance/`, no schema delta. Bridge
      between PR #43 substrate and existing `MonthlyChannelRevenueFactORM`
      write path. See spec at
      `Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md`.
    - ✅ Track E (2026-06-08): **B1 source-rows read API DONE.**
      `GET /revenue/source-rows?month=&source_system=` + `/{id}`,
      `finance.view_revenue`-gated, tenant-scoped, keyset-paged; `raw_payload`
      never returned (`raw_payload_redacted` always true); cross-tenant/missing
      id returns 404. The paired-column `*_usd` -> native migration remains
      ⏳ PENDING as a separate future spec (out of scope for Track E).
    - ✅ Track F (2026-06-09): **Smart revenue reconciliation workflow DONE.**
      Month-level engine derives the three reductions (US tax, YouTube->AdSense
      transfer fee, AdSense->bank fee+FX) from actual figures and attributes the
      aggregate ones per channel proportional to CMS gross; persists typed
      `deduction_components` + a `revenue_reconciliation_usd` explanation (only
      TAX feeds `net_revenue_usd`). `POST /revenue/months/{month}/reconcile`
      (`CHANGE_ALLOCATION_RULE@finance_month`) +
      `GET /revenue/channels/{id}/months/{month}/reconciliation`
      (`VIEW_REVENUE@channel`). **Outside-CMS 1:1 ALLOCATION attribution DONE**
      (single verified account->channel link writes the gross fact; many ->
      skip + warn). **Manual report purge DONE** —
      `DELETE /reports/raw-files/{id}` (`MANAGE_CONNECTORS@connector`,
      reason-required, marks PURGED keeping metadata). ⏳ Refine-later: real
      US-view-share feed, withholding-rate calibration, and multi-API-key
      ingestion scaling.
---

## Phase 0 — Foundation decisions

### Scope

- ✅ UMS beta only — locked.
- ✅ YouTube channels only — locked.
- ✅ No title/show mapping in the first release — locked.
- ✅ No Content ID fingerprint/claims module in the first release —
  locked.
- 🗑️ removed in PR #12 — Neo4j graph projection layer dropped entirely
  from the active roadmap.

### Outputs

- ✅ Final stack decision (FastAPI + PostgreSQL + Docker via PRs #1, #11,
  #15).
- ⏳ OAuth/API access plan — remaining: credentials repository exists
  (PRs #33, #34); PR #47 stacks the full Google live connector foundation
  (B2.1-B2.4 in one PR). Public OAuth consent flows, live connector
  ingestion against real Google APIs, and token monitoring are not
  started — they belong to B2.5+ slices that stack on PR #47.
    - ✅ PR #47 — Google live connector foundation (B2.1-B2.4 in one
      stack, merged 2026-05-27 as commit 52734a3): credential foundation (secret resolver dispatch +
      gcp-secret-manager:// + local-secret:// + OAuth refresh wrapper);
      blob storage backends (GCS + file-store) + raw_file lifecycle
      helpers (mark_parsed accepts FAILED -> PARSED retry recovery;
      mark_failed FAILED -> FAILED idempotent); connector_runs +
      connector_run_raw_files ORM, repository, Alembic migration, and
      raw_report_files tenant/id UNIQUE for composite FKs; google-auth +
      httpx base client + retry policy + YouTube Reporting client +
      report_type whitelist + run_one() orchestrator +
      scripts/run_google_connector.py CLI with extensible --connector
      registry. Concern A (deterministic_blob_path emitted gs://
      regardless of backend, so the default LocalFileStoreBackend
      rejected every URI at upload) closed by commit 435aa58 — scheme is
      now threaded through (backend, scheme, bucket) from
      _build_blob_backend(); regression test exercises the real
      LocalFileStoreBackend end-to-end through run_one. B2.5 (YouTube
      Analytics) + B2.6 (operator console) stack on top in follow-up
      PRs.
    - ✅ PR #48 (B2.5, merged 2026-05-28 as commit 68ac62e) — YouTube
      Analytics targeted CMS-channel
      ingestion; registers youtube-analytics (and the youtube_analytics
      alias) in the B2.4 connector registry; queries
      `ids=contentOwner==<account>` with `filters=channel==<id>` and
      wire-level `dimensions=month` only so the revenue metrics stay on
      a supported YouTube Analytics contract (the channel dimension
      requires a multi-value channel filter for content-owner reports);
      the orchestrator runner synthesises the `channel` dimension into
      the parser payload from the filter so `YouTubeAnalyticsParser`
      keeps its `(channel, month)` row-key contract without a parser
      change; scopes channel selection to `active + revenue_required +
      content_owner_id matches account`; real LocalFileStoreBackend
      round-trip + dry-run regression tests included; tenant_id derived
      from run.tenant_id (no cross-tenant credential lookup); locked
      metrics/dimensions constants shared between client and runner so
      the wire shape cannot silently drift.
    - ✅ PR #49 (B2.6) — AdSense Management client + connector audit
      emitters + adsense-management runner + mock end-to-end ingestion
      gate. AdSenseManagementClient issues one GET reports.generate per
      (account, month) with locked params (dateRange=CUSTOM,
      dimensions=MONTH, metrics[]=ESTIMATED_EARNINGS,
      metrics[]=TOTAL_EARNINGS,
      currencyCode=USD); the adapter stamps a deterministic SHA-256
      report_id so AdSenseManagementParser's report_id contract survives
      the API not returning one. Audit emitters reuse
      AuditEventType.CONNECTOR_JOB_RUN (STARTED|FINISHED) +
      REPORT_IMPORTED (DOWNLOADED|PARSED|FAILED) with a `lifecycle`
      payload discriminator — no new enum or permission values. The
      orchestrator threads a SqlAlchemyAuditSink and a tenant-scoped
      connector service principal (Permission.RUN_CONNECTOR_JOBS via a
      new UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID env) through every
      finish_run path including the fail-safe sweep; STARTED commits
      with start_run, FINISHED commits with finish_run, per-raw-file
      edges stage in the main transaction; dry-run emits zero events
      and a missing service-actor env fails closed in Bucket A before
      any RUNNING row is created. DOWNLOADED fires only on fresh
      inserts and FAILED->DOWNLOADED retries, not on idempotent reuse.
      AdSenseManagementRunner registers under both adsense-management
      and adsense_management keys, fetches once per (account, month)
      (account-scoped — no channel loop), and forwards the T35 parser-
      ready payload verbatim. A new mock end-to-end ingestion gate at
      tests/connectors/runs/test_ingestion_gate.py runs all three
      connectors (YT Reporting + YT Analytics + AdSense) through
      run_one + C1 GoogleSourceNormalizer.normalize_month and asserts:
      6 source rows across all three source_systems, exactly 2
      MonthlyChannelRevenueFactORM rows from the YT paths, every
      AdSense row skipped as SkipReason.MISSING_CHANNEL_ID, and the
      12-event STARTED->DOWNLOADED->PARSED->FINISHED audit sequence per
      run with the connector service principal.
    - ✅ PR #50 (merged 2026-05-28 as commit 9c884bd) — Concern C:
      connector_runs.counts_json source-row created/updated/unchanged split
      (B2.6 carry-forward). Replaces the
      placeholder where rows_upserted_created/updated/unchanged were
      always 0 with a real classification:
      SqlAlchemyGoogleRevenueSourceRowRepository.upsert_many now
      pre-fetches existing rows by
      (tenant_id, source_system, source_row_key) and returns a
      SourceRowUpsertResult carrying entries plus per-row classification
      counts. _content_matches_existing compares the parser-owned content
      fields (excludes provenance: raw_file_id / imported_by — a fresh
      raw file carrying identical values is "unchanged content, refreshed
      evidence", not a value update). _process_one_report bubbles the
      counts up via a new _ProcessedReportResult dataclass and
      _handle_live_produced_report sums them into the run-level counts
      dict that finish_run writes to connector_runs.counts_json. Sum
      invariant: created + updated + unchanged == total for live upserts.
      Dry-run reports the parsed would-upsert total while keeping
      created/updated/unchanged at 0 because no source-row write or
      classification runs.
      6 new repo-level tests cover create/unchanged/updated/mixed-rerun,
      provenance-only rerun, and refreshed RETURNING payloads; 1 new
      end-to-end orchestrator test asserts the counts plumb
      into connector_runs.counts_json across 3 consecutive runs (fresh
      insert, identical rerun, mutated rerun). _require_no_duplicate_keys
      fails closed (typed GoogleRevenueSourceRowValidationError, nothing
      persisted) when one batch carries two rows on the same
      (source_system, source_row_key) — ambiguous source evidence that
      would otherwise silently last-write-wins and skew the split; the
      error names the key only (no raw_payload leak), capped at 10 with a
      "(+N more)" suffix. 4 added repo tests: duplicate-in-batch rejected
      (fail closed, nothing persisted); same key under a different
      source_system still allowed (the guard keys on the full tuple);
      error-message cap formatting; guard runs before the FK/currency
      existence pre-checks.
- ⏳ Channel inventory file format — remaining: tenant-scoped channel
  registry exists (PR #25); bulk inventory load format not yet defined.
- ⏳ Finance month-close input format — remaining: close-gate enforced
  server-side (PR #8); manual close UI and bank-input form not yet defined.

### Acceptance gate

- ⏳ At least 300+ channels listed/classified/grouped — remaining:
  registry and group registry exist; bulk inventory ingestion not yet
  driven.

### Status (2026-05-22)

Scope locked; stack decided. The outputs and acceptance gate are blocked
on real ingestion (Phase 2) and the inventory load workflow.

---

## Phase 1 — Channel registry and hierarchy

### Build

- ⏳ Dynamic organization hierarchy — remaining: ORG models exist
  (PR #25); hierarchy assignment workflow / UI not built.
- ✅ Channel registry (tenant-scoped, PR #25).
- ⏳ CMS status: inside / outside / unknown — remaining: status column
  exists in the registry; Track F (PR #87) attributes outside-CMS revenue for
  the single verified account->channel link case (1:1 ALLOCATION), so
  outside-CMS revenue sourcing is partially resolved — many-link and zero-link
  channels stay open (Hard Problem #1 in `15_DELIVERY_BACKLOG.md`).
- ⏳ Revenue-required flag — remaining: column exists in the registry and is
  surfaced in the Registry table source label (PR #73/#78); finance-fact
  coverage of revenue-required channels is now flagged by the
  `CHANNELS_MISSING_REVENUE_FACTS` smart alert (this PR).
- ✅ Group builder (tenant-scoped channel group registry, PR #25 + direct
  tests in PR #30).
- ✅ Role model (PRs #3, #4, #24 — tenant-scoped roles & permission grants).

### Outputs

- ⏳ Channel master table — remaining: schema exists; bulk inventory load
  not yet driven.
- ⏳ Company/sector/group mapping — remaining: registries exist and the
  Registry page now drives live re-parenting via `PATCH /channels/{id}/mapping`
  (Registry Phase 2, PR #78), now month-lock-guarded (this PR rejects a mapping
  change that would rewrite a LOCKED month's attribution); bulk inventory
  assignment workflow still not built.
- ⏳ Outside-CMS monitor — remaining: status column exists and the CommandView
  outside-CMS / channel-issues monitor panel is wired to
  `GET /channels/outside-cms` + `GET /channels/issues` (this PR,
  VIEW_ANALYTICS-gated, no-fetch-when-restricted); proactive alerting beyond the
  panel + the `CHANNELS_MISSING_REVENUE_FACTS` smart alert is not built.

### Acceptance gate

- ⏳ Every active channel assigned or in unmapped list — remaining: bulk
  load + unmapped report not implemented.

### Status (2026-05-22)

Registry + group + role models all tenant-scoped and unit-tested at the
repository layer. Hierarchy assignment workflow, inventory load, and
unmapped-channel report are the remaining gaps.

---

## Phase 2 — YouTube ingestion

### Build

- ⏳ YouTube Reporting API jobs — remaining: client + `run_one` orchestrator +
  CLI built and mock-tested (PR #47); blocked only on real OAuth credentials.
- ⏳ YouTube Analytics API targeted queries — remaining: targeted CMS-channel
  query layer built and mock-tested (PR #48, B2.5); blocked only on real OAuth
  credentials.
- ⏳ YouTube Data API metadata sync — remaining: same credentials block; no
  real sync.
- ⏳ Raw report storage — remaining: ORM + repository (PR #32) + blob backends
  + raw-file lifecycle (PR #47) wired into the ingest pipeline; runs only on
  mock fixtures (no live creds).
- ⏳ Normalized monthly channel facts — remaining: the C1 normalizer
  (`GoogleSourceNormalizer.normalize_month`, PR #44) is now WIRED into `run_one`
  as a post-run step (PR #90, refactored into
  `connectors/runs/normalization.py` by PR #93), so a run projects source rows
  to `MonthlyChannelRevenueFactORM`; blocked only on real OAuth credentials.
- ⏳ Missing report alerts — remaining: per-channel coverage is now flagged by
  the `CHANNELS_MISSING_REVENUE_FACTS` smart alert (this PR); a stored
  expected-connectors/accounts baseline (missing-REPORT detection) is deferred
  (`connector_runs` carries no channel dimension).

### Outputs

- ⏳ Daily/monthly channel metrics — not shipped (no real ingestion).
- ⏳ Monthly revenue facts where available — not shipped (no real
  ingestion).
- ⏳ Revenue source labels and source-reported currency evidence — not
  shipped (no real ingestion).

### Acceptance gate

- ⏳ Dashboard can show gross monthly revenue and performance for
  CMS-linked channels — not yet met.

### Status (2026-06-13)

Engine-complete, credentials-blocked. The live pull engine is fully built and
mock-tested end-to-end (OAuth refresh wrapper + httpx client + YouTube Reporting
/ YouTube Analytics / AdSense Management clients + `run_one` orchestrator +
CLI, PRs #47-#50); the C1 normalizer is wired into `run_one` (PRs #90/#93) so a
run projects source rows to monthly facts; and `POST /connectors/jobs` now
executes a real ingest via the in-process `ConnectorJobExecutor` (PR #95, the
RLS lanes fixed by PRs #94/#97). The single remaining blocker is real Google
OAuth credentials — no live pull has run against the real APIs. The pipeline
stores Google-reported monetary values and currencies as source evidence before
finance facts consume them.

---

## Phase 3 — AdSense payment matching

### Build

- ⏳ AdSense account connector — remaining: automated multi-account
  discovery/onboarding via the credentials repository (PRs #33, #34). OAuth
  refresh and the live `accounts.payments.list` pull already ship; today an
  operator supplies each account id to the sync CLI.
- ✅ Monthly payment pull — shipped: live AdSense `accounts.payments.list` pull
  (GoogleAdSensePaymentClient + pure fail-closed mapping/parse +
  AdSensePaymentSyncService with read-only locked-month skip + audit + operator
  CLI `scripts/run_adsense_payment_sync.py`), re-keyed on `source_account_id`
  (migration 20260529_0001). Follow-up: bare ambiguous-symbol amounts ($, ¥, kr)
  fail closed by design (no $→USD guess), so $-denominated settlements need an
  explicit-currency resolution before they can sync.
- ✅ Paid/unpaid status — shipped (PR #52): per-month, per-account,
  per-currency settlement-status breakdown (`finance/payment_status.py` +
  `GET /adsense/payments/status`); outstanding = PENDING + UNPAID; CANCELLED
  reported for evidence, excluded from outstanding; no FX.
- ✅ Payment month matcher — shipped earlier and verified live:
  `build_monthly_payment_match_summary` + `GET /revenue/months/{month}/payment-match`
  (month-total YouTube↔AdSense match; prior backlog "not started" was stale).
- ✅ Payment-vs-YouTube comparison — shipped via the payment-match endpoint
  (YouTube gross USD vs PAID AdSense USD → gap + PAYMENT_MATCHED/PAYMENT_VARIANCE);
  bank reconciliation (PR #29) remains a separate downstream leg.

### Outputs

- ✅ Monthly AdSense payment table — live pull shipped; real pulls preserve
  Google's reported payment currency, source account identity, and deterministic
  source report identity.
- ✅ Payment match status — driven by the payment-match endpoint plus the
  paid/unpaid status breakdown (`GET /adsense/payments/status`).
- ✅ Payment gap value — computed and tested as `payment_gap_usd` by
  `build_monthly_payment_match_summary`, exposed via
  `GET /revenue/months/{month}/payment-match`.

### Acceptance gate

- ✅ System can show whether YouTube revenue total matches the AdSense
  payment amount — met via `build_monthly_payment_match_summary` +
  `GET /revenue/months/{month}/payment-match` (PAYMENT_MATCHED / PAYMENT_VARIANCE
  with `payment_gap_usd`).

### Status (2026-05-29)

Live AdSense payment pull persists account-scoped settlements into the
PostgreSQL `adsense_payments` source-of-truth. The month-total YouTube↔AdSense
matcher already ships (`GET /revenue/months/{month}/payment-match`); PR #52 adds
the per-account, per-currency paid/unpaid status breakdown
(`GET /adsense/payments/status`). Both read AdSense-reported amounts/currencies
only — no market FX. Remaining Phase 3 depth (per-account *matching* needing a
channel↔account map, and multi-currency FX) stays out per Docs/18.

---

## Phase 4 — Reconciliation and allocation engine

### Build

- ✅ Finance month-close screen — close-gate backend (PR #8); CloseView UI
  shipped in PR #69 (status + readiness + lock/unlock with audited reason).
- ⏳ Manual bank/payment input — remaining: bank recon repo (PR #29);
  input UI not built.
- ⏳ Tax/deduction ingestion — substrate + ingestion shipped (PR #55):
  `deduction_components` table + three adapters (Google value_kind tax/deduction,
  bank transfer-fee/FX, AdSense earnings→payment gap) + operator CLI + sensitive
  `DEDUCTION_COMPONENTS_INGESTED` audit. Consumption shipped (PR #56): net_revenue
  derives a channel-direct `COMPONENT_DERIVED`/`D_ESTIMATED` net from same-month,
  same-source CHANNEL TAX/DEDUCTION components only when source net is missing
  (anti-double-count, anti-cross-source), plus read-only
  `GET /revenue/months/{month}/deduction-components`. This branch now consumes
  verified account→channel allocation for ACCOUNT-grain net-applicable evidence;
  persisted/committed allocation state shipped (PR-5 + PR-6); remaining allocation work is
  PAYMENT-grain evidence — BLOCKED pending remittance/bank data + the operator receipt→account model.
- ⏳ Allocation rules (Spec 2b) — PR-1 SHIPPED (#58) + PR-2 SHIPPED (#59) + PR-3 SHIPPED (#60) + PR-4 SHIPPED (#61) + PR-5 SHIPPED (#62) + PR-6 SHIPPED (#65): PR-2 folds
  account-allocated net-applicable lines into net-revenue (API + finance exports) on the
  missing-net path, read/compute only (no persistence, no migration). PR-3 adds a
  `net_revenue_usd` metric to `POST /revenue/channels/{channel_id}/months/{month}/explain`,
  reusing PR-2's net builder + a shared no-drift account-allocation provenance helper to emit
  channel-direct + account-allocated deduction breakdowns in the existing `number_explanations`
  components JSON (read + persist, no migration, no schema change), gated by
  VIEW_FINALIZED_PAYMENTS@finance_month(month) with dual REVENUE_VIEWED + PAYMENT_VIEWED audit.
  PR-4 SHIPPED (PR #61): export deduction breakdown — XLSX/PDF/PPTX surface the
  channel-direct vs account-allocated split plus two additive month aggregates on
  MonthNetRevenueSummary; read-surface only.
  PR-5 SHIPPED (PR #62): persisted/committed allocation — write-only versioned
  snapshot endpoint (POST /revenue/months/{month}/account-allocations/commit) over the
  gross_revenue_proportional compute + 4 tables + ALLOCATION_COMMITTED audit; readers unchanged.
  Post-review hardening (Codex): finite (non-NaN, non-Infinity) CHECK on
  committed_allocation_unallocated.amount_usd + non-empty (length>=1) CHECKs on the snapshot
  identity columns (lines account/channel/component key; unallocated scope_id/component_key;
  notes channel) mirroring deduction_components; the commit OpenAPI contract now documents both
  201 (new snapshot) and 200 (idempotent replay).
  PR-6 SHIPPED (PR #65, 2026-06-03): read-switch — a central lock-aware
  resolve_month_account_allocation now backs all four readers (allocation GET, net-revenue,
  explain, exports), which prefer the latest committed snapshot for LOCKED months (lossless
  reconstruction; live fallback when no committed run; OPEN/no-close-row stays live), and emit
  full allocation_source/committed_run provenance on every surface plus an export disclosure
  token. No migration / no auth / no write-path change.
  POST-TAX METHOD SHIPPED (PR #67, 2026-06-04): allocation-method status —
  `gross_revenue_proportional` AND `post_tax_revenue_proportional` are now BOTH committable.
  The engine/orchestrator are parameterized on `allocation_method` (gross weights by source
  gross; post_tax weights by source net_revenue_usd, fail-closed omitting any
  (channel, source_kind) key with a null-net fact); the commit path is un-gated to a
  two-method allowlist (service + DB `ck_committed_allocation_runs_method` CHECK + migration
  20260603_0001); the persisted basis field was renamed `basis_gross_usd`→`basis_amount_usd`
  (column + finite CHECK + migration); and `/revenue/recalculate`'s dry-run net check moved
  from channel grain to (channel, source_kind) grain so a dry-run can no longer report READY
  while the commit engine would go UNALLOCATED.
  COMPANY_LEVEL + NO_ALLOCATION METHODS SHIPPED (branch
  `spec/no-allocation-company-level-methods`, 2026-06-06): `company_level` weights by
  company source-aligned gross with a flat split inside each company (consumes the
  org-access channel→company index at the route boundary; fail-closed COMPANY_UNMAPPED /
  COMPANY_BASIS_INCOMPLETE / ZERO_COMPANY_BASIS); `no_allocation` commits a zero-line
  snapshot persisting every component as a typed INTENTIONAL_NO_ALLOCATION unallocated
  row (reject-on-unallocated bypassed for this method only). Service allowlist is now
  four methods; the DB CHECK widened to all five via migration 20260606_0001 ('manual'
  pre-cleared at DB layer only). `/revenue/recalculate` preview parity: no_allocation is
  exempt from NO_REVENUE_FACTS; company_level gains COMPANY_MAPPING_MISSING.
  MANUAL METHOD + RECALCULATE COMMITTED WRITE-PATH SHIPPED (branch
  `spec/manual-allocation-recalc-write`, 2026-06-06): `manual` commits an
  operator-asserted per-channel split posted as `manual_lines` on the commit endpoint
  (pure fail-closed builder in `finance/manual_allocation.py` — exact per-component
  Decimal sums, verified channels only, ≤6dp non-negative amounts, every ACCOUNT
  component covered, out-of-range amounts rejected typed; the engine keeps rejecting
  manual — it is committable via the service-level allowlist only; manual lines fold
  into the idempotency fingerprint with legacy digests unchanged).
  `/revenue/recalculate` `dry_run=false` now performs a real committed allocation via
  the same service path (same advisory lock / idempotency / version chain; preview runs
  as a pre-flight BLOCKED_BY_ISSUES 409 gate; write-only VIEW_FINALIZED_PAYMENTS gate;
  `idempotency_key` required; cross-endpoint idempotent replay with the commit
  endpoint; manual is redirected to the commit endpoint).
  Remaining: PAYMENT-grain allocation BLOCKED — pending live remittance/bank evidence + an
  operator-asserted (tenant_id, month, bank_reference)→account(s) receipt-assertion model
  (verified 2026-06-03, no deterministic bank_reference→account bridge — see
  Docs/superpowers/specs/2026-06-03-spec-payment-account-modeling-design.md). All five
  allocation methods + the recalculate committed write-path shipped 2026-06-06 (see
  above).
  Prerequisite SHIPPED
  (PR #57): canonical channel↔account map — `adsense_content_owner_links`
  (operator-verified account↔owner) + `content_owner_channel_links` (derived from
  source-row co-occurrence) + Alembic migration + audited propose/verify/reject
  API (dual MANAGE_ORG_MAPPING + CHANGE_ALLOCATION_RULE gate, per-account
  advisory-lock overlap invariant) + `list_verified_adsense_account_channels`
  read contract. Allocation consumes only VERIFIED links; unmapped/unverified
  accounts stay UNALLOCATED.
- ✅ Net revenue by channel/company/sector — shipped: GET
  /revenue/months/{month}/net-revenue (build_month_net_revenue_summary;
  per-channel gross/net/deduction roll-up with channel/company/sector/global
  scoping, USD-only). Channel-direct deductions and ACCOUNT-grain allocations
  now apply on the missing-source-net path; PAYMENT-grain allocation remains
  unbuilt.
- ✅ Manual override rules — shipped: create + approve flow (POST
  /revenue/manual-overrides, /manual-overrides/{id}/approve) with locked-month
  guard and APPROVE_MANUAL_OVERRIDE scope. Override-rules dashboard UI remains
  unbuilt (Phase 5).

### Outputs

- ⏳ Gross revenue / Deductions / Net revenue / Deduction percentage /
  Unresolved gap / Confidence rating — gross, net, unresolved payment gap, and
  confidence labels ARE produced (net-revenue + payment-match + explain APIs);
  deduction figures now include channel-direct and account-allocated
  net-applicable components on missing-source-net rows. Committed/persisted
  allocation state shipped (PR-5 write path + PR-6 lock-aware read-switch);
  remaining is PAYMENT-grain allocation (BLOCKED — pending remittance/bank evidence + the
  operator receipt→account assertion model) plus other allocation methods.

### Acceptance gate

- ⏳ Finance can generate a channel-level net revenue table for a selected
  month — partially met: GET /revenue/months/{month}/net-revenue produces the
  per-channel table today and includes channel-direct/account-allocated
  deductions on the missing-source-net path; committed/persisted allocation state
  shipped (PR-5 write path + PR-6 read-switch), so full reconciled net now awaits
  only PAYMENT-grain allocation — BLOCKED pending remittance/bank evidence + the operator
  receipt→account assertion model (see the 2026-06-03 payment-account-modeling design).

### Status (2026-06-01)

Net-revenue roll-up, manual overrides, payment-match, and confidence labels
ship at the API layer. PR-A (PR #55) ingested deduction evidence; PR-B (PR #56)
wired net-revenue to derive `COMPONENT_DERIVED`/`D_ESTIMATED` nets from
channel-scoped TAX/DEDUCTION components on the missing-net path, and added the
read-only `GET /revenue/months/{month}/deduction-components` endpoint. This
branch ships the canonical channel↔account map substrate (two-layer ORM +
Alembic migration + audited propose/verify/reject API + read contract) as the
prerequisite for Spec 2b, plus Spec 2b PR-1: the account-level deduction
allocation compute + read endpoint (`GET /revenue/months/{month}/account-allocations`).
Spec 2b PR-2 now wires source-aligned, net-applicable ACCOUNT allocations into
`GET /revenue/months/{month}/net-revenue` and finance exports with matching
`PAYMENT_VIEWED` audit coverage. Spec 2b PR-3 extends the channel-month explain
endpoint with a `net_revenue_usd` metric that reuses the PR-2 net builder and a
shared no-drift provenance helper to surface channel-direct + account-allocated
deduction breakdowns in the persisted `number_explanations` components JSON (no
migration), gated by `VIEW_FINALIZED_PAYMENTS@finance_month(month)` with dual
`REVENUE_VIEWED` + `PAYMENT_VIEWED` audit. Spec 2b PR-5 then shipped the
persisted/committed allocation WRITE path and PR-6 the lock-aware read-switch
(readers prefer committed snapshots for LOCKED months); the allocation engine's
remaining work is PAYMENT-grain distribution — the largest blocker to a fully
reconciled net figure, itself BLOCKED pending live remittance/bank evidence + an
operator-asserted (tenant_id, month, bank_reference)→account(s) receipt model
(verified 2026-06-03) — plus other allocation methods.

---

## Phase 5 — Smart dashboard

### Build

- ✅ Revenue command center — shipped (PR #69): CommandView wired to the
  live net-revenue API (month selector, status strip, channel table with
  the channel-direct vs account-allocated deduction split).
- ✅ Explain-number screen — shipped (PR #69): TraceView wired to
  POST /revenue/channels/{ch}/months/{m}/explain (metric selector, source +
  formula + confidence provenance). (Planned as a drawer; shipped as the
  Trace/Explain page.)
- ✅ Smart problem panel — shipped (PR #69): smart-alerts problem panel
  wired into the Command Center from GET
  /revenue/months/{month}/smart-alerts.
- ✅ Company/sector/channel ranking — shipped (this PR): finance-gated,
  scope-safe `GET /revenue/months/{month}/rankings` (pure `build_month_rankings`
  rolls the per-channel net-revenue summary up to company/sector, ranks each
  dimension by gross|net|deduction with None-sink + stable id tie-break, top-N)
  + a CommandView rankings panel (own hook, money gated on `canViewFinance`,
  metric toggle, surfaces `allocation_source`).
- ✅ Group/sector rollup scope selector — shipped (branch
  `feat/group-sector-rollup`): new fail-closed `GET /revenue/scopes`
  (`finance.view_revenue`, read-only, no audit; pure
  `finance/revenue_scopes.py` `build_authorized_revenue_scopes`) returns ONLY
  the viewer's authorized rollup scopes (global / their sectors / their
  companies), so the selector can never over-list the org structure or offer a
  dead option that 403s on the rollup read. CommandView now populates its
  `<select aria-label="Scope">` from that endpoint (keyed on a stable
  `{scopeType, scopeId}` pair, global-only fallback on load/403) and threads the
  chosen scope into the already-wired net-revenue + rankings reads. Channel
  GROUP scope is a named follow-up (see acceptance gate below).
- ✅ Outside-CMS issue monitor — shipped (this PR): CommandView monitor panel
  wired to `GET /channels/outside-cms` + `GET /channels/issues`
  (VIEW_ANALYTICS-gated, no-fetch-when-restricted; 403 -> denied copy, never
  "no issues").
- ✅ Month-close status — shipped (PR #69): CloseView wired to
  GET/POST /finance-close (status, readiness checklist, lock/unlock with
  audited reason).

### Outputs

- ⏳ Internal decision dashboard / Smart alerts — shipped as live UI
  (PR #69). Management-ready summaries remain export-driven (Phase 6
  artifacts); the Registry page is wired to `GET /channels` (PR #73/#78) and
  the Audit page to `GET /audit/events`).

### Acceptance gate

- ⏳ A user can select month + group and receive source-backed gross,
  deduction, net, currency, and explanation — **MET for global / sector /
  company rollup** (branch `feat/group-sector-rollup`): the Command Center
  selector now lists the viewer's authorized scopes from the fail-closed
  `GET /revenue/scopes` endpoint and threads the chosen month + scope into the
  source-backed net-revenue + rankings reads (gross/deduction/net per scope);
  channel + month selection with explanations already shipped in PR #69.
  Remaining (named follow-up): **channel-GROUP revenue scope** (TV_BRAND /
  CUSTOM_GROUP etc.) is not a finance `scope_type` today — it needs a
  `ScopeType.GROUP`, a group_id → member channel_ids resolver, and per-channel
  authorization as the AND of `AccessScope.channel(cid)` checks; the exports
  path is the precedent (`api/exports.py` `_require_export_scope_permissions`,
  which loops the resolved channels asserting `AccessScope.channel(cid)`, plus
  `_access_scope_from_export_scope` for the non-group scope→AccessScope mapping).
  Optional display conversion is later work and must be labeled non-official
  unless it is the currency reported by Google/AdSense.

### Status (2026-06-05)

Six screens are wired to live APIs on the June-14 MVP branch (PR #69):
Command Center + smart alerts, Close, Trace/Explain, Exports, Connectors —
plus a demo-month seed (`scripts/seed_demo_month.py`), an end-to-end smoke
(`scripts/smoke_mvp.py`), and the demo runbook (`frontend/README.md`).
Registry is wired to `GET /channels` (PR #73/#78); the Audit page is
now wired to `GET /audit/events` (see below).

- ✅ Production session hydration — merged to main as PR #70 (4d7f154):
  `GET /session/me` (`backend/ums_smart_revenue/api/session.py`) returns the
  authenticated principal's identity + optional tenant + roles/permissions +
  **global-scope** camelCase capability booleans derived (fail-closed) from the
  permission policy; the SPA bootstraps it (`useSessionBootstrap` +
  `SessionContext`) and the AppShell renders the dashboard gated by those
  capabilities (capability-gated AppShell). A production build no longer shows
  the permanent access-denied screen and needs no preview role; the dev preview
  role is now presentation-only. Failed hydration / a `disabled` principal fails
  closed to `AccessDenied`; connector controls require `canRunConnectorJobs`;
  the smoke asserts the contract. Live ingestion needs real connector
  credentials.
- ✅ `canViewAnalytics` session capability — shipped (this PR): `/session/me`
  `SessionCapabilities` gains `can_view_analytics`, derived **scope-aware** (true
  if the principal holds ANY active `VIEW_ANALYTICS` grant — direct or via role
  — at any scope, mirroring `connector_health`; NOT the global-only `_can()`
  helper, so a legitimately company/sector-scoped analytics user still sees the
  panel). Fail-closed (disabled -> false). The FE `SessionCapabilities` gains
  `canViewAnalytics`, mapped through `AppShell` `capabilitiesToPermissions` and
  threaded to CommandView; it gates the outside-CMS / channel-issues monitor
  panel (no-fetch-when-restricted).
- ✅ Production Audit view wiring — merged to main as PR #71 (31a7641): the
  dashboard Audit page reads the real
  `GET /audit/events` feed (tenant-scoped audit log from PR #22) instead of the
  mock `AUDIT_EVENTS`. A memoized `useAuditEvents` hook (one self-auditing fetch
  per mount — no loop), distinct cursor-pagination types
  (`AuditLogEntry`/`AuditEventCursor`/`AuditEventPagination`, never the offset
  `PaginationMeta`), and an extracted `views/AuditView.tsx`. Server-driven
  sensitive-payload redaction (the UI reflects `details_redacted`, never reveals
  withheld payloads or offers a reveal control); fail-closed gate (a non-audit
  viewer sees the restricted placeholder and fires no fetch); 403 → audit-
  appropriate no-permission copy. Frontend-only; backend unchanged. (Registry
  was later wired to `GET /channels` in PR #73/#78, so no page remains
  mock-labelled.) Track C follow-up (branch
  `feat/audit-track-c`) completed the surface: Load More via
  `pagination.next_cursor` (append + id-dedupe + filter-change reset), a live
  event-type filter on the existing `event_type` param (real `AuditEventType`
  values; no severity facet was invented), and "Download Audit View" wired to a
  new synchronous `GET /audit/events/export` CSV route (same gate/filters/
  redaction as the list route, 10,000-row cap + `X-Truncated` header surfaced
  in the UI, snapshot-before-audit `EXPORT_DOWNLOADED` emission, formula-
  injection-guarded deterministic CSV). CommandView confidence badges now show
  human labels via the shared `confidenceDisplay` helper (raw code in
  title/aria).
- ✅ Connector credential test-connection probe — `POST /connectors/credentials/{connector_key}/{account_id}/test`
  (branch `docs/plan-hygiene-post-71`): wraps `resolve_connector_credentials()` (load cred →
  resolve secret URI → OAuth refresh, no live data pull). 404 on missing credential;
  200 with `status` field (`ok` / `inactive_credential` / `auth_failed` / `error`) and
  string `detail` otherwise. `CONNECTOR_TESTED` audit event. 5 TDD tests, `MANAGE_CONNECTORS`
  gate. Backend only; no migration. Merged to main as PR #72 (28da1a6).
- ✅ Connector run history + Test Connection — merged to main as PR #81 (Track D
  buildable chunk): read-only `GET /connectors/runs` (`VIEW_CONNECTOR_HEALTH`
  gate, tenant-scoped, connector_key/account_id filters, newest-first cursor
  pagination, no audit write) + new `list_runs` repository read; ConnectorsView
  run-history panel replaces the placeholder (status/counts/error_summary + Load
  More id-dedupe); per-credential Test Connection button surfaces the existing
  probe; `/session/me` gains `canViewConnectorHealth` so the SPA gate mirrors the
  route. No migration. Remaining Track D (OAuth consent, live pulls, token-expiry
  schema + background monitoring) stays creds/schema-blocked.
- ✅ Registry Phase 1 wiring — merged to main as PR #73 (56bf9a8): the Channel
  Registry table is wired to `GET /channels` (replacing `REGISTRY_ROWS` mock).
  Client-side derivation: avatar initials, CMS badge tone from `cms_status`,
  source label from `revenue_source_status`, state (Option A — field-complete,
  no migration), trace key (`"channel:{id}"` or `"pending"`). Extracted to
  `views/RegistryView.tsx`; 16 new Vitest tests. Frontend-only; all pages off
  mock.
- ✅ Soft Dark design system applied — on `feat/design-system-softdark` (stacked
  on Registry Phase 2): token-value conversion of `frontend/src/styles.css` to
  the UMS Revenue Design System Soft Dark theme (GitHub dark_dimmed
  surfaces/ink/status; Anthropic-orange accent unchanged; new `--ink-strong`
  near-white tier for money/KPI values; variable-font weight tiers) + OFL
  webfonts (Mona Sans / Newsreader / Monaspace Neon from `mockups/FontsGH`)
  shipped in `frontend/public/fonts/` with licenses. Visual-only: no selector
  or component-logic change; `DESIGN.md` updated off the old warm palette.
- ✅ Registry Phase 2 — on `feat/registry-phase2` (spec
  `Docs/superpowers/specs/2026-06-07-registry-phase2-design.md`): new read-only
  `GET /org-units` (tenant-scoped, active-only, fail-closed VIEW_ANALYTICS gate
  mirroring `GET /channels`; no migration) feeds Company/Sector display names
  (honest raw-id fallback when a unit is missing/deactivated or the fetch
  fails) and the Map modal's company options. Row actions are LIVE: "Map" →
  `PATCH /channels/{id}/mapping` via the mapping-change panel (row click
  presets the channel; required audited reason; in-flight latch — one PATCH
  per click burst; reload on success; typed inline 403/404/409/422); "Assign"
  → `POST /revenue/channel-account-links` proposing an UNVERIFIED
  OPERATOR_ASSERTED link (verification stays the dual-gated admin flow);
  "Review" → navigates to Trace preselected on the channel (AppShell
  `onOpenTrace` + TraceView `presetChannelId`). Mock Save-Draft/effective-month
  controls removed (no backend concept). Remaining (definition-blocked):
  bulk channel inventory import; "Scoped changes" tile; month-lock enforcement
  on the mapping route (pre-existing backend gap, named follow-up).

---

## Phase 6 — Export center

### Build

- ✅ Excel export — shipped: GET /exports/{export_id}/finance-workbook.xlsx
  (+ preview) via build_finance_workbook_xlsx; tenant-scoped export jobs,
  persisted + audited. Final column template/branding may iterate.
- ✅ PDF report — shipped: GET /exports/{export_id}/executive.pdf
  (build_executive_pdf_report + build_executive_pdf_bytes; executive summary,
  gross/net, rankings, problem sections, persisted + audited). Layout polish
  may iterate.
- ✅ Branded slide export — shipped: GET
  /exports/{export_id}/branded-slide-pack.pptx (build_branded_slide_pack_pptx;
  cover + content slides with brand bar/footer, persisted + audited). Final
  theming may iterate.
- ⏳ Configurable export templates — remaining: not started.
- ✅ Export audit log (tenant-scoped audit log infrastructure via PR #22).

### Outputs

- ✅ Monthly finance workbook / Executive PDF / Management slide pack — all
  three generate end-to-end (xlsx/pdf/pptx under /exports/{id}/..., persisted
  + audited). Brand/template polish may iterate.

### Acceptance gate

- ⏳ Finance can generate a locked monthly report by company, sector, or
  all UMS — partially met: scoped xlsx/pdf/pptx generation ships; remaining is
  full reconciled-net content (tax/deduction ingestion + allocation-rule
  application) feeding the report bodies.

### Status (2026-05-29)

Tenant-scoped export-job persistence exists, and all three document types
generate end-to-end (Excel workbook, executive PDF, branded slide pack) with
real bytes and audit. Remaining polish is final column/layout/brand templating
and the reconciled-net content (Phase 4 allocation/tax) feeding report bodies.

---

## Phase 7 — Hardening

### Build

- ✅ Audit logs (tenant-scoped per PR #22; foundation in PR #1).
- ⏳ Failure alerts — remaining: month-level smart alerts ship
  (`GET /revenue/months/{month}/smart-alerts`) and now include a per-channel
  `CHANNELS_MISSING_REVENUE_FACTS` HIGH coverage alert (this PR); proactive
  push/notification delivery is not built.
- ⏳ Data quality checks — remaining: revenue-fact required-field gate
  (PR #8) plus the `CHANNELS_MISSING_REVENUE_FACTS` per-channel coverage check
  (this PR, active+revenue_required channels with no monthly fact); broader
  quality checks not built.
- ⏳ Backup/export retention — remaining: not started.
- ⏳ OAuth token monitoring — remaining: credentials repo (PRs #33, #34) +
  four `api_connector_credentials` refresh-telemetry columns (last-attempt,
  token-expiry, last-status, last-error-class) stamped at the
  `resolve_connector_credentials` chokepoint (PR #95 Part 2, migration
  `20260612_0001`) + a per-credential Test Connection probe (PR #72); active
  background expiry monitoring + auto-flip on refresh failure not built.
- ✅ Month locking — shipped: explicit POST /finance-close/{month}/lock +
  /unlock workflow (readiness-gated, audited MONTH_LOCKED/MONTH_UNLOCKED).
  Strengthened this PR: `PATCH /channels/{id}/mapping` now rejects (409) a
  re-parenting that would rewrite a LOCKED month's company/sector attribution.
- ✅ Migration target-DB safety (PR #96) — alembic env.py no longer lets an
  ambient `UMS_DATABASE_URL` silently override an in-code-injected
  `sqlalchemy.url`, closing the wrong-DB footgun where migration round-trip tests
  could drop/upgrade whatever DB the env var named. The resolver
  (`db/migration_url.py::resolve_database_url`) re-reads the ini's on-disk url and
  honors any differing configured value (a deliberate in-code injection) over the
  env var; production's ini-placeholder + env-var-wins contract is preserved. 8
  unit tests + 2 PG e2e; no schema change.

### Acceptance gate

- ⏳ Detect missing channels, missing reports, unmatched payments,
  manually overridden values — partially met: missing channel-fact coverage
  (`CHANNELS_MISSING_REVENUE_FACTS`, this PR) and unmatched payments
  (PAYMENT_VARIANCE smart alert + payment-match) are detected; missing-REPORT
  detection is deferred (no expected-connectors baseline).

### Status (2026-06-13)

Audit-log infrastructure shipped end-to-end and tenant-scoped, and the
smart-alerts surface now flags per-channel missing-revenue-fact coverage
(`CHANNELS_MISSING_REVENUE_FACTS`, this PR). Remaining hardening (report-coverage
baseline, backup/retention, live OAuth token monitoring) awaits the ingestion +
expectation-model work that produces the underlying signals.
