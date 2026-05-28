# Implementation Plan

## Status (2026-05-27)

Mainline merge status is reconciled through PR #36 (S2 multi-tenant
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
- ⏳ PR #10: Frontend preview shell — remaining: real dashboard pages, API
  client, `X-UMS-Tenant` header wiring.

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
- ⏳ PR #36: S2 stack merged onto `main` (commit `96dbe73`) with resolver
  admission control, trusted-gateway tenant middleware, bootstrap UMS
  tenant, cross-tenant principal binding hardened, and tenant-scoped
  export jobs. Remaining: frontend `X-UMS-Tenant` header wiring +
  tenant-aware API client (tracked as S2 spec Phase 5; not started).

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
  keys + `app_tenant` / `app_platform` Postgres roles. None implemented
  in code. Spec for the S3 PR series not yet written.
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
    - ⏳ PR #49 (B2.6) — AdSense Management client + connector audit
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
  exists in the registry; outside-CMS revenue sourcing is unresolved
  (Hard Problem #1 in `15_DELIVERY_BACKLOG.md`).
- ⏳ Revenue-required flag — remaining: column exists in the registry;
  UI surfacing not built.
- ✅ Group builder (tenant-scoped channel group registry, PR #25 + direct
  tests in PR #30).
- ✅ Role model (PRs #3, #4, #24 — tenant-scoped roles & permission grants).

### Outputs

- ⏳ Channel master table — remaining: schema exists; bulk inventory load
  not yet driven.
- ⏳ Company/sector/group mapping — remaining: registries exist; assignment
  workflow not built.
- ⏳ Outside-CMS monitor — remaining: status column exists; monitor UI and
  alerts not built.

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

- ⏳ YouTube Reporting API jobs — remaining: credentials repository
  (PRs #33, #34); no real ingestion run.
- ⏳ YouTube Analytics API targeted queries — remaining: same; no real
  query layer.
- ⏳ YouTube Data API metadata sync — remaining: same; no real sync.
- ⏳ Raw report storage — remaining: ORM + repository tests (PR #32);
  ingestion pipeline not built.
- ⏳ Normalized monthly channel facts — remaining: revenue facts foundation
  (PR #2); ingestion source not wired.
- ⏳ Missing report alerts — remaining: not started.

### Outputs

- ⏳ Daily/monthly channel metrics — not shipped (no real ingestion).
- ⏳ Monthly revenue facts where available — not shipped (no real
  ingestion).
- ⏳ Revenue source labels and source-reported currency evidence — not
  shipped (no real ingestion).

### Acceptance gate

- ⏳ Dashboard can show gross monthly revenue and performance for
  CMS-linked channels — not yet met.

### Status (2026-05-22)

Scaffolding only. This is the single largest gap between doc-claimed state
and reality: the entire phase depends on real YouTube API ingestion, which
has not been started. Credentials and raw-storage substrates exist to plug
into. The ingestion work must store Google-reported monetary values and
currencies as source evidence before any finance facts consume them.

---

## Phase 3 — AdSense payment matching

### Build

- ⏳ AdSense account connector — remaining: credentials repository
  (PRs #33, #34); real OAuth + pull not built.
- ⏳ Monthly payment pull — remaining: AdSense payment ORM + repo tests
  (PR #26); real pull not built.
- ⏳ Paid/unpaid status — remaining: status field exists in the payment
  ORM; reconciliation pass not driven.
- ⏳ Payment month matcher — remaining: not started.
- ⏳ Payment-vs-YouTube comparison — remaining: bank reconciliation repo
  (PR #29) is the substrate; comparison logic not driven.

### Outputs

- ⏳ Monthly AdSense payment table — schema only; real pulls must preserve
  Google's reported payment currency and source report identity.
- ⏳ Payment match status — not driven.
- ⏳ Payment gap value — not computed.

### Acceptance gate

- ⏳ System can show whether YouTube revenue total matches the AdSense
  payment amount — not yet met.

### Status (2026-05-22)

Same shape as Phase 2: schema and tenant-scoped repos exist; no real pull
or matching pass runs. Matching must start from AdSense-reported payment
amounts/currencies, not market FX-derived amounts.

---

## Phase 4 — Reconciliation and allocation engine

### Build

- ⏳ Finance month-close screen — remaining: close-gate backend (PR #8);
  UI not built.
- ⏳ Manual bank/payment input — remaining: bank recon repo (PR #29);
  input UI not built.
- ⏳ Tax/deduction ingestion — remaining: not started.
- ⏳ Allocation rules — remaining: not started.
- ⏳ Net revenue by channel/company/sector — remaining: revenue facts
  (PR #2); allocation pass not built.
- ⏳ Manual override rules — remaining: number explanation entry + repo +
  factory (PR #31) is the substrate; override approval flow not built.

### Outputs

- ⏳ Gross revenue / Deductions / Net revenue / Deduction percentage /
  Unresolved gap / Confidence rating — none of these aggregations are
  produced yet; foundations exist.

### Acceptance gate

- ⏳ Finance can generate a channel-level net revenue table for a selected
  month — not yet met (depends on allocation engine).

### Status (2026-05-22)

Foundations present; the allocation engine and tax/deduction ingestion are
the largest blockers.

---

## Phase 5 — Smart dashboard

### Build

- ⏳ Revenue command center — remaining: frontend preview shell only
  (PR #10); pages not built; no API client.
- ⏳ Explain-number drawer — remaining: number explanation backend
  (PR #31); drawer not built.
- ⏳ Smart problem panel — remaining: not started.
- ⏳ Company/sector/channel ranking — remaining: not started.
- ⏳ Outside-CMS issue monitor — remaining: not started.
- ⏳ Month-close status — remaining: close-gate backend (PR #8); UI not
  built.

### Outputs

- ⏳ Internal decision dashboard / Smart alerts / Management-ready
  summaries — none shipped (no real UI pages).

### Acceptance gate

- ⏳ A user can select month + group and receive source-backed gross,
  deduction, net, currency, and explanation — not yet met. Optional display
  conversion is later work and must be labeled non-official unless it is the
  currency reported by Google/AdSense.

### Status (2026-05-22)

Frontend is a sparse Vite/React shell. Building the dashboard requires
both (a) a real API client (with `X-UMS-Tenant` header per S2) and (b) the
allocation engine (Phase 4) producing real numbers to render.

---

## Phase 6 — Export center

### Build

- ⏳ Excel export — remaining: export artifacts foundation (PR #9) +
  tenant-scoped export jobs (PR #36); format and template not finalized.
- ⏳ PDF report — remaining: not started.
- ⏳ Branded slide export — remaining: not started.
- ⏳ Export templates — remaining: not started.
- ✅ Export audit log (tenant-scoped audit log infrastructure via PR #22).

### Outputs

- ⏳ Monthly finance workbook / Executive PDF / Management slide pack —
  not shipped.

### Acceptance gate

- ⏳ Finance can generate a locked monthly report by company, sector, or
  all UMS — not yet met (depends on templates + allocation).

### Status (2026-05-22)

Tenant-scoped export-job persistence exists. The actual document templates
(Excel format, PDF layout, slide brand) are not yet defined.

---

## Phase 7 — Hardening

### Build

- ✅ Audit logs (tenant-scoped per PR #22; foundation in PR #1).
- ⏳ Failure alerts — remaining: not started.
- ⏳ Data quality checks — remaining: revenue-fact required-field gate
  (PR #8); broader quality checks not built.
- ⏳ Backup/export retention — remaining: not started.
- ⏳ OAuth token monitoring — remaining: credentials repo (PRs #33, #34);
  monitoring not built.
- ⏳ Month locking — remaining: close-gate (PR #8); explicit lock/unlock
  workflow not built.

### Acceptance gate

- ⏳ Detect missing channels, missing reports, unmatched payments,
  manually overridden values — not yet met.

### Status (2026-05-22)

Audit-log infrastructure shipped end-to-end and tenant-scoped. Other
hardening items remain ungroomed until the ingestion + allocation phases
produce real signals to monitor.
