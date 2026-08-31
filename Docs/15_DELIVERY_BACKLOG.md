# Delivery Backlog

## Status (ledger through 2026-08-30; program plans recertified 2026-08-31)

The detailed marker ledger below was last fully reconciled through PR #170
(owner-stamp recovery, merged 2026-08-06). Current-main status is separately
verified for PR #171 (merged 2026-08-07) and PR #211 (merged 2026-08-30);
intervening entries are not implicitly reclassified by this note. Marker
conventions match `01_IMPLEMENTATION_PLAN.md`:

- `✅ PR #N` — shipped end-to-end at the layer being marked.
- `⏳ PR #N — remaining: <note>` — partial; concrete remaining work is
  named.
- `🗑️ removed in PR #N — <reason>` — dropped from scope.

Honesty rule: scaffolding-only items (ORM + repo + tests but no real
ingestion / UI / user-facing path) are marked `⏳`, not `✅`.

**Post-reconciliation status note (2026-08-30):** PR #171
(`feat/scheduled-group-sync`) merged to `main` on 2026-08-07 as `cc8892d`; its
inline entry below is now `✅ PR #171`. PR #211 (rolling month window) merged to
current `main` on 2026-08-30 as `41b4953`; its inline entry below is now
`✅ PR #211`. This note does not claim a full review of intervening PRs.

**Program plans note (2026-08-31, branch `docs/program-plans-consolidated`):** consolidates
the five plan documents from former draft plan PRs (#209 Docs/20–21, #218 Docs/23,
#219 Docs/24, plus Docs/25) into the #220 docs PR after the gap-fix review. See:
[`20_DEPLOYMENT_READINESS_AUDIT.md`](20_DEPLOYMENT_READINESS_AUDIT.md),
[`21_BETA_IMPLEMENTATION_PLAN.md`](21_BETA_IMPLEMENTATION_PLAN.md),
[`23_ADMIN_ACCESS_AND_CONFIG_PLAN.md`](23_ADMIN_ACCESS_AND_CONFIG_PLAN.md),
[`24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md`](24_US_WITHHOLDING_AND_US_REVENUE_PLAN.md),
[`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md).
Living P0 execution is tracked by current successor PRs **#221–#225 (P0-a…P0-e)**.
PR #210 is historical: it merged into the non-main `docs/deployment-readiness-audit`
branch and is not the source of truth on `main`. See
[`25_PROGRAM_DEPENDENCY_GRAPH.md`](25_PROGRAM_DEPENDENCY_GRAPH.md) for the live-status snapshot.

**US-withholding plan note (2026-08-31):** Docs/24 — Egypt–US treaty copyright-royalty
withholding is **15%** when confirmed on AdSense tax info (not 16%); no-form defaults:
**30% on US-source earnings (business)** / **24% on worldwide (individual)**. Program
U1–U4: probe → additive country-sliced ingest (normalization fence plus non-alerting
intentional-evidence telemetry) → backend-emitted estimate from a PostgreSQL
account/category effective interval (**no env/default fallback**) → optional durable,
idempotent, payment-linked actual revision. **Fence:** recon
`DEFAULT_US_WITHHOLDING_RATE = 0.30` stays dormant (Docs/21 P3). D-U1 (AdSense tax-info
confirmation + persisted effective-dated row) blocks U3.

**Test-harness note (2026-06-11, branch `fix/pg-migration-test-lock-timeout`):** the PG
migration round-trip tests' `fresh_engine` schema reset now sets `SET LOCAL lock_timeout`
so a contended `DROP SCHEMA public CASCADE` fails fast with a diagnosable error instead of
hanging indefinitely. Run these against a **fresh** Postgres cluster — reusing a
shared/contended cluster (orphaned roles/locks from a prior run) causes spurious
failures/hangs (see `Docs/superpowers/specs/2026-06-11-pg-migration-test-lock-timeout.md`).

**Docs-correctness note (2026-06-16, branch `docs/finance-security-contract-correctness`):**
a finance/security contract-doc correctness sweep corrected stale statements in
`Docs/02/07/08/11/12/13/14/17` against current `main`: `POST /revenue/recalculate`
`dry_run=false` is a committed, audited finance write (not dry-run-only); `app_platform`
is `NOBYPASSRLS` (not `BYPASSRLS`); the real audit table is `audit_logs` and the real
tenant migration chain is `20260516_0001`→`20260517_0001`→`20260518_0001` with RLS
enforced in `20260608_0001`/`20260612_0002` (no `platform_audit_logs`, no `20260520_*`);
`adsense_payments` uses the 4-column key `(tenant_id, source_account_id, month,
payment_name)`; the `audit_logs` DDL has 13 columns; the explain confidence wire shape is
`{label, score}`; and the audit-event (38) and role (16) catalogs are complete.

**DeepSource-baseline note (2026-08-08, branch `fix/deepsource-baseline-wave1`):**
wave 1 of the repo-baseline cleanup (the 214-issue default-branch backlog, distinct
from the per-PR checks): the two CRITICAL `PTC-W0063` unguarded-`next()` sites and
the mechanical MAJOR/MINOR classes — `PTC-W0043` ×2, `SH-3014` ×15, `SH-3015` ×6,
`SH-3012` ×1, `JS-0339` ×15, `JS-0067` ×11, `JS-R1005` ×1, `SQL-L029` ×3 (56 findings,
22 files) — all fixed by conforming code, zero suppressions. `security_schema.sql`
keyword-named columns (`key`, `sensitive`) are quoted, not renamed: they mirror the
live alembic-managed schema. Still open in the baseline: `SCT-A000` secret-shaped
strings ×158 (per-string triage: rotate vs reshape) and the de-suppression wave for
files still carrying `skipcq` markers (those findings are invisible to the 214 count).
Container note: the standing `ums-mig-pg-test` container's default `postgres` DB
carries a stray fully-migrated schema whose grants block the migration tests'
`DROP ROLE` teardown (10 pre-existing PG-tier failures on unmodified `main` when
pointed at that container — a live instance of the 2026-06-11 fresh-cluster rule
above); cleanup of the stray objects is an operator decision.

**Repo-visibility note (2026-08-04, branch `chore/public-repo-hygiene`):** the
repository is public as of 2026-08. Hygiene pass: `.gitignore` now blocks the
workstation-only session artifacts (`.tmp*`, `.worktrees/`, `.cursor/`,
`.omo/`, stray session scripts) from ever being committed, and the
2026-05-21 runlogs no longer carry the operator's personal email. Convention
going forward: docs and fixtures must not embed operator-real identifiers
(CMS content-owner ids, live revenue figures, personal emails) — use
placeholders and mark redactions.

**Coverage + tracker note (2026-06-17, branch `docs/coverage-tracker-reconciliation`):**
broader coverage and tracker reconciliation sweep: AGENTS.md Neo4j references removed;
README/Docs/02 stack fiction (Next.js, Celery, asyncpg, RLS-planned) corrected to shipped
reality; Docs/00 file table updated; Docs/05 fictional output tables and health-state enum
corrected; Docs/14 fictional symbols replaced; Docs/15 net-revenue "Phase 4 unbuilt" note
corrected + PR #111/112 entries added + FX scaffold and deduction-ingestion CLI noted +
12 "this PR" placeholders resolved to PR #98; Docs/16 stack/allocation/RLS decisions
closed; Docs/01 status header and 12 "this PR" placeholders updated.

**Backlog reconciliation note (2026-08-06):** the "Reconciled through" line above
had read PR #121 since the 2026-06-18 sweep while PRs #122–#170 kept appending
entries beneath it, so the header date and the claim had drifted apart. Verified
in this pass against the merged history of `a7ttaf/Youtube` and the code on
`main`. Five entries misstated shipped behaviour and are corrected inline below:
live Google ingestion was still described as "blocked only on owner-approved
credentials" (the 2026-06-22 operator smoke ran for real, so that clause and the
matching one on the credential-smoke sub-item are retired); the Registry Phase 2
item still called the bulk inventory import "definition-blocked" (shipped as
`POST /channels/import`, PR #159); the Phase 5 item still called missing-REPORT
detection deferred (closed from the connector side by PR #131); the CMS group
sync entry still listed both of its follow-ups as "Still open" (both closed by
PR #170); and the CI entry listed only the original vendored gate while four CI
surfaces had been added and removed since. Six shipped PRs had no entry at all
and are added: #127, #129, #130, #131, #155 and #159. Entries that named only a
branch now also carry their merged PR number. PRs #136–#148, #150, #151, #153
and #154 are absent from this doc on purpose — they are Dependabot PRs that were
closed unmerged and superseded by the consolidated batch in #156.

## P0 — Must build first

- ⏳ Dynamic org hierarchy — remaining: ORG models (PR #25); hierarchy
  assignment workflow not built.
- ✅ Channel registry (tenant-scoped, PR #25).
- ⏳ CMS/outside-CMS status — remaining: schema column (PR #25); outside-CMS
  revenue sourcing partially resolved (Track F 1:1 attribution, PR #87) and the
  outside-CMS monitor panel surfaces the rest (PR #98); many/zero-link channels
  stay open (Hard Problem #1).
- ✅ Group builder (tenant-scoped channel group registry, PR #25 + tests
  in PR #30).
- ⏳ YouTube report ingestion — the live pull engine + `run_one`
  orchestrator + C1 normalizer wiring + executing `POST /connectors/jobs` are all
  built (PRs #47-#50, #90/#93, #94/#95/#97) and have now run against live Google
  APIs: the 2026-06-22 operator smoke ingested one content owner for 2026-04 and
  produced 25 real `monthly_channel_revenue_facts` (PRs #132 credential contract,
  #134 smoke CLI, #135 live-run gate). **The long-standing "blocked only on
  owner-approved Google connector credentials" note is retired** — approval
  arrived and the credentials work. Remaining: ingestion is operator-triggered
  only (the CLI or `POST /connectors/jobs`); there is no recurring revenue-pull
  schedule (only the group sync has one, on the unmerged
  `feat/scheduled-group-sync` branch), and breadth beyond the single smoked
  owner/month is still unproven at the 300+-channel target. API-key-only access
  is valid
  only for YouTube Data API public metadata where Google permits it; private
  YouTube Reporting/Analytics and AdSense revenue/account data require official
  Google authorization tokens/scopes, never Gmail passwords, browser cookies, or
  linked personal Gmail sessions.
    - ✅ Google credential setup/smoke runbook (branch
      `codex/google-credential-smoke`) — adds the credential-only smoke CLI and
      defines the owner approval packet, GCP Secret Manager payload/ref
      contract, UMS metadata-only credential registration, audited credential
      token-refresh probe, ingestion CLI `--dry-run` smoke, live-run gate,
      rollback/rotation notes, and test coverage references. No live
      credentials are committed to the repo. **Superseded (2026-06-22):** this
      entry's original "live ingestion remains blocked until owner-approved
      Google material/scopes are supplied" clause was true when the runbook
      landed; owner approval then arrived and the smoke ran for real — see the
      ingestion item above and the B2-credentials-CLOSED notes below.
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
- ⏳ Monthly revenue normalization — revenue facts foundation in PR #2;
  normalization bridge from google_revenue_source_rows shipped in PR #44;
  run_one now wires the post-run normalizer (PR #90) behind the adapter
  (PR #93).
    - ✅ Connector-job executor (PR #95) — POST /connectors/jobs now EXECUTES: it
      submits a real ingest pull to a bounded in-process `ConnectorJobExecutor`
      and returns 202 `submitted` (the old `recorded_not_executed` no-op is retired;
      the CLI `scripts/run_google_connector.py` remains a valid trigger). The
      worker runs the CLI pattern on its own session under
      `connector_tenant_context` -> `run_one`. Fail-closed setting
      `connector_job_executor_enabled` (default OFF -> 503); duplicate guard
      (in-process registry + DB RUNNING reader with stale-orphan supersede);
      route-owned `job_submitted`/`job_rejected` audit + worker
      `job_failed_before_start` Bucket-A audit. Frontend "Run pull" control
      (report_month + dry-run, submitted banner, run-history refetch).
    - ✅ Part 2 credential refresh telemetry — four additive nullable columns on
      `api_connector_credentials` (`last_refresh_attempt_at`, `token_expiry_at`,
      `last_refresh_status`, `last_refresh_error_class`) + CHECK
      `ck_connector_last_refresh_status` (migration `20260612_0001`), stamped at
      the single `resolve_connector_credentials` chokepoint; surfaced on
      GET /connectors/credentials.
    - Deferred: no celery/redis broker (in-process only); no partial-unique
      index on RUNNING runs (TOCTOU is code-level, accepted at max_workers=1);
      no owner-approved live Google connector credentials; no auto-flip of
      credential `status` to `failed_auth` on refresh failure.
    - ✅ Executor Bucket-A audit RLS regression fix (PR #97) — three post-#95
      reverts (`bdf5b71`/`15c0818`/`06af2ed`) dropped the `TENANT_CTX`
      minimal-tenant set
      in `_audit_failed_before_start`, leaving `app_current_tenant_id()` NULL so
      the `audit_logs` `WITH CHECK (tenant_id = app_current_tenant_id())` RLS
      policy denied the failure-audit INSERT on Postgres (main red gate on
      `test_bucket_a_audit_persists_under_platform_lane_on_postgres`;
      `app_platform` is `NOBYPASSRLS` so `platform_lane` alone is insufficient).
      Restored the fabricated-`Tenant` (id-only) contextvar bridge, reset via
      `finally`, so the `after_begin` hook writes the trusted context row. Only
      the Bucket-A path was restored (dry-run path stayed on
      `connector_tenant_context`, which already passes); SQLite tier unaffected.
    - ✅ PR #94 (ingestion RLS lane fix; branched off main `82fd67f` = PR #93) —
      made the merged ingest→normalize pipeline executable on RLS-enforced
      Postgres (it previously composed only on SQLite). New
      db.lane.platform_lane elevates every run-path
      platform-only-write transaction (audit_logs lifecycle/REPORT_IMPORTED/
      PROJECTION_FAILED emits, monthly_channel_revenue_facts upserts,
      finance_month_close creation) to app_platform. Only the credential read,
      the LOCKED-month prefilter SELECT, and the post-loop deferred Analytics
      stale-row flush stay on app_tenant; each per-report ingest transaction
      (raw files, source rows, mark_parsed, in-savepoint stale deletes) is
      elevated because its DOWNLOADED/PARSED/FAILED audit edges commit
      atomically with the ingest evidence. New
      connectors/runs/tenant_context.connector_tenant_context sets TENANT_CTX in
      scripts/run_google_connector.py so the run no longer dies fail-closed at
      the credential read. after_begin now respects a platform-lane flag so a
      nested SAVEPOINT cannot re-pin the tenant lane and undo the elevation.
      Restored the analytics_cleanup_blocked normalize gate (read removed in
      a3a584a; the docstrings already promised it). New PG-tier proof
      tests/connectors/runs/test_run_one_rls_postgres.py (5 obligations); no
      grant/policy/migration change.
- ✅ Alembic env.py URL-precedence hardening (PR #96) — `get_database_url()` no
  longer lets an ambient `UMS_DATABASE_URL` silently override an in-code-injected
  `sqlalchemy.url`. That footgun meant any box with `UMS_DATABASE_URL` exported
  (a dev/staging DB) would run migration round-trip tests' schema drops + upgrades
  against that ambient DB — data-destructive and nondeterministic. The precedence
  logic is extracted to a unit-tested seam
  `db/migration_url.py::resolve_database_url`: it re-reads the ini's on-disk
  `sqlalchemy.url` and treats any *differing* configured value as a deliberate
  in-code injection that wins over the env var; when the configured url equals the
  ini placeholder (production, no override) `UMS_DATABASE_URL` still wins,
  preserving the prod contract. This is robust for BOTH caller patterns —
  `Config()` (no ini) and `Config(alembic.ini)` + `set_main_option` (the
  tests/tenancy + test_session_tenant_hook migration tests, which a review found
  the first config_file_name-only gate had missed). 8 unit tests (4 spec
  quadrants + empty-guard + edge + 2 ini-injection) + 2 PG e2e regressions (decoy
  env var cannot retarget either a no-ini or an ini-backed `command.upgrade`). No
  schema/data change; no graph projection impact.
- ✅ AdSense payment sync — shipped: live `accounts.payments.list` pull
  (GoogleAdSensePaymentClient + pure fail-closed mapping/parse +
  AdSensePaymentSyncService with read-only locked-month skip + audit + operator
  CLI `scripts/run_adsense_payment_sync.py`), re-keyed on `source_account_id`
  (migration 20260529_0001). Follow-up: bare ambiguous-symbol amounts ($, ¥, kr)
  fail closed by design (no $→USD guess), so $-denominated settlements need an
  explicit-currency resolution before they can sync.
- ✅ AdSense payment matching + paid/unpaid status — month-total YouTube↔AdSense
  matcher (`GET /revenue/months/{month}/payment-match`, verified pre-existing)
  and per-account/per-currency settlement-status breakdown
  (`GET /adsense/payments/status`, `finance/payment_status.py`, PR #52).
  Payment-match remains USD-only; the paid/unpaid status view groups
  AdSense-reported amounts by currency. Outstanding = PENDING + UNPAID;
  CANCELLED shown for evidence; no FX, per Docs/18.
- ✅ Finance month-close screen — shipped (PR #69): CloseView wired to
  GET/POST /finance-close (status, readiness checklist, lock/unlock with
  audited reason); close-gate backend (PR #8).
- ✅ Net revenue calculation — shipped: GET /revenue/months/{month}/net-revenue
  (build_month_net_revenue_summary; per-channel gross/net/deduction roll-up,
  scope-filtered, USD-only). Tax/deduction components are computed and persisted
  by Track F reconciliation (PR #87); the allocation engine (Phase 4,
  PRs #58–#76) applies gross_revenue_proportional, post_tax_revenue_proportional, company_level,
  manual, and no_allocation methods against committed ACCOUNT-scoped allocation
  snapshots (PAYMENT-scoped allocation remains a separate follow-up).
- ⏳ Confidence labels — labels ARE computed in services
  (net-revenue B_RECONCILED/D_ESTIMATED/E_MISSING; explain confidence label)
  and returned by the net-revenue/explain APIs; Trace/Explain shows label +
  score (PR #69) and CommandView now renders human confidence badges
  (`confidenceDisplay` helper, raw code in title/aria — Track C,
  `feat/audit-track-c`). Remaining: finance adoption/validation of the labels.
- ✅ Explain-number API — shipped: POST
  /revenue/channels/{channel_id}/months/{month}/explain
  (build_channel_month_revenue_explanation; per-metric source/formula/
  confidence/warnings, persisted to number_explanations). Explain-number
  drawer UI remains unbuilt (Phase 5).
- ✅ Smart issue panel — shipped (PR #69): smart-alerts problem panel wired
  into the Command Center from GET /revenue/months/{month}/smart-alerts
  (build_monthly_smart_alert_summary). Extended in PR #98 with a per-channel
  `CHANNELS_MISSING_REVENUE_FACTS` HIGH coverage alert (active+revenue_required
  channels with no monthly fact).
- ✅ Excel export — shipped: GET /exports/{export_id}/finance-workbook.xlsx
  (+ preview) via build_finance_workbook_xlsx; tenant-scoped export jobs,
  persisted + audited. Final column template/branding may iterate; templates are
  now configurable per tenant via `/export-templates` (PR #130).

## P1 — Strong beta features

- ✅ PDF export — shipped: GET /exports/{export_id}/executive.pdf
  (build_executive_pdf_bytes; executive summary, gross/net, rankings, problem
  sections, persisted + audited). Layout polish may iterate.
- ✅ Branded slide export — shipped: GET
  /exports/{export_id}/branded-slide-pack.pptx (build_branded_slide_pack_pptx;
  cover + content slides with brand bar/footer, persisted + audited). Final
  theming may iterate.
- ✅ Outside-CMS monitor — shipped (PR #98): CommandView monitor panel wired to
  `GET /channels/outside-cms` + `GET /channels/issues` (both VIEW_ANALYTICS-gated,
  scope-filtered, `{items, summary}`, no money/audit), gated on `canViewAnalytics`
  with no-fetch-when-restricted; summary tiles (outside-CMS / missing-official-
  revenue / open-issues), distinguishing "outside CMS but covered" from "outside
  CMS + missing source". Track F (PR #87) already attributes outside-CMS revenue
  for the single verified link case.
- ✅ Recalculation by allocation method — shipped: POST /revenue/recalculate.
  `dry_run=true` returns `build_recalculation_preview` (allocation-method preview
  with blocking-issue detection); `dry_run=false` now performs a real committed
  allocation through the same service path (PR #76 — same advisory lock /
  idempotency / version chain; preview as a pre-flight BLOCKED_BY_ISSUES 409
  gate; write-only VIEW_FINALIZED_PAYMENTS gate; manual redirected to the commit
  endpoint). See the manual-method + recalculate write-path entry below.
- ✅ Channel `content_owner_id` write path — shipped: the operator key that
  `list_target_channels` matches against the CMS account id is now settable.
  `content_owner_id` is an optional field on channel create and has a dedicated
  `PATCH /channels/{id}/content-owner` route (MANAGE_CHANNELS, audited
  `CHANNEL_UPDATED` with old/new, no-op-suppressed, no locked-month coupling
  because it only retargets future ingestion). Closes the latent silent-zero
  ingestion gap where every channel kept `content_owner_id=None` and matched no
  account. No migration (the `youtube_channels.content_owner_id` column already
  existed).
- ✅ Channel↔account map — shipped (PR #57): two-layer canonical map
  (`adsense_content_owner_links` operator-verified + `content_owner_channel_links`
  derived from source rows), audited propose/verify/reject API behind dual
  MANAGE_ORG_MAPPING + CHANGE_ALLOCATION_RULE gates, per-account advisory-lock
  overlap invariant, and `list_verified_adsense_account_channels` for Spec 2b.
  - ✅ PR #57 review hardening (Codex/Kody): allocation permission now checked
    for every finance month in a link's effective range (not just start month);
    verify blocked on LOCKED months (409); AdSense account ids canonicalized
    (`accounts/` prefix stripped) before persist so verified reads match
    ingestion; duplicate-proposal IntegrityError narrowed to true unique
    violations (non-unique integrity errors re-raise instead of mis-mapping
    to 409).
  - ✅ PR #57 review hardening round 2 (Codex/Kody/CodeRabbit): unique-violation
    detection now recognizes psycopg3 `sqlstate` (the pinned driver), not only
    psycopg2 `pgcode`, so duplicate POSTs return 409 on PostgreSQL; verify AND
    reject now guard the FULL covered month range (a bounded link spanning a
    later LOCKED month no longer verifies, and rejecting a VERIFIED link over a
    closed month is blocked so it can't silently drop closed-month allocation
    evidence); owner↔channel derivation skips LOCKED months (no new post-close
    evidence); and the request model strips whitespace before canonicalizing
    `adsense_account_id` (a padded `accounts/…` value previously 422'd due to
    Pydantic v2 before-validator ordering).
  - ✅ PR #57 review hardening round 3 (Codex/Kody/CodeRabbit): `_require_range_open`
    no longer materializes every covered month — it lock-checks only the start
    month, then row-locks the `finance_month_close` rows that already exist across
    the covered range with a single `SELECT ... FOR UPDATE`. This (a) stops an
    authorized far-future `effective_month_end` from inserting/advisory-locking
    ~95k rows in one transaction, and (b) serializes against the month-close path
    (which takes the same `FOR UPDATE` on the close row via
    `get_or_create_month_close_row(for_update=True)`), so a concurrent close can no
    longer flip an existing OPEN covered month to LOCKED between the scan and the
    verify/reject commit. `reject` now reloads the link under the per-account
    advisory lock (TOCTOU guard: a concurrent verify committing during the
    lock-wait is observed so the locked-month guard isn't skipped on a stale
    UNVERIFIED status); the duplicated finance-local `_iter_months` helper was
    removed (now unused). Proven on live Postgres by
    `test_repo_verify_blocks_on_held_covered_month_close_row_lock` (verify is
    canceled by the held covered-month row-lock wait; reverting the scan to a plain
    SELECT makes it fail).
  - ✅ PR #57 review hardening round 4 (Codex/Kody/CodeRabbit): the same
    concurrent-close protection now covers the *derivation* path, and more
    strongly than the verify/reject path can. `upsert_owner_channel_links_from_source`
    reads observed source-row months first, then for EVERY observed month
    get-or-creates + row-locks its `finance_month_close` row under the same
    advisory + `FOR UPDATE` guard the close path uses
    (`get_or_create_month_close_row(for_update=True)`), in sorted order for a
    stable lock sequence. Because the observed months are bounded by months that
    actually have source rows (unlike a verify link's open-ended/far-future range),
    derivation can serialize row CREATION too: an absent observed month is created
    OPEN and locked here, so a concurrent close blocks until we commit (month stays
    OPEN) or commits first (we read LOCKED and skip the month). This fully closes
    the absent-month window on the derivation side — there is no N9 residual here
    (GSk; proven on live Postgres by two contention tests that each fail "DID NOT
    RAISE" when the guard is downgraded:
    `test_repo_derivation_blocks_on_held_observed_month_close_row_lock` for an
    existing OPEN month and
    `test_repo_derivation_blocks_on_held_absent_observed_month_advisory_lock` for a
    month with no close row). Each derived insert now runs in its own `SAVEPOINT` and swallows only a genuine
    `uq_content_owner_channel_links_key` unique violation (a duplicate landing
    between the existence probe and flush from a parallel worker), re-raising any
    other `IntegrityError` so the derivation stays idempotent under concurrency
    (GSr; covered by `test_derivation_swallows_concurrent_duplicate_insert`). The
    verify/reject audit `details` now records the full `effective_month_start`/
    `effective_month_end` range, not just the start-month scope, so month-level
    audit review of a later (now closed) period still surfaces the mutation that
    changed its allocation eligibility (GSp; covered by
    `test_verify_records_full_effective_range_in_audit_details`). `_require_range_open`
    gained an explicit `Raises:` docstring section documenting
    `ChannelAccountLinkLockedMonthError` (KHa). Derivation now also drops blank
    source identities before grouping: the source identity columns are nullable
    `Text` with no non-empty CHECK, so a `''`/whitespace-only `content_owner_id` or
    `youtube_channel_id` would otherwise hit `content_owner_channel_links`'
    `length(...) >= 1` CHECK (sqlstate 23514, which the unique-only SAVEPOINT does
    not swallow) and abort the whole derivation, or persist a bogus active link
    (V8b; covered by `test_derivation_skips_blank_source_identities`, which fails
    with that exact CHECK violation when the filter is removed).
  - ⏳ Deferred follow-up (PR #57 N9): narrowed residual concurrent-close race in
    `_require_range_open` (verify/reject path ONLY — the derivation path is now
    fully closed, see round 4 above). The start month is materialized + locked via
    `get_or_create_month_close_row(for_update=True)`, and the `FOR UPDATE` range
    scan closes the race for covered months whose close row ALREADY exists. The
    remaining window is a covered month *after* start with NO close row at scan
    time that is closed concurrently — the close inserts a fresh LOCKED row that a
    row-level lock on absent rows cannot cover. The derivation fix (get-or-create +
    lock every observed month) does NOT transfer here: a verify link's range can be
    open-ended (`effective_month_end = None`) or far-future bounded, so per-month
    materialization is unbounded/infeasible and would reintroduce the ~95k-row
    insert the round-3 -iW fix removed. Eliminating this needs a shared
    serialization point on the month-close path itself (e.g. PostgreSQL
    `SERIALIZABLE`/predicate-range lock, or a per-tenant close-epoch advisory lock
    both paths take) — a close-path change outside the scope of PR #57 that also needs
    owner approval as a finance-close refactor. Risk bounded: no production
    consumer of `list_verified_adsense_account_channels` until Spec 2b; both
    verify/reject and close are dual-gated admin actions. File:
    `backend/ums_smart_revenue/finance/channel_account_links.py`
    (`_require_range_open`); sequence with Spec 2b / month-close hardening.
  - ✅ PR #57 N10 — DONE (branch `feat/auth-allocation-range-shortcircuit`):
    `_require_allocation_permission_for_range` now short-circuits the bounded
    path when the caller holds CHANGE_ALLOCATION_RULE at global scope (a GLOBAL
    grant is a strict superset of the per-finance-month checks via
    `OrgAccessIndex.contains`), eliminating the ~95k in-memory authz iterations
    for a far-future `effective_month_end`. Uses the non-raising `has_permission`
    so a non-global caller still falls through to the per-month loop (gated
    month-by-month); identical authorization decision, never more permissive.
    Authz test matrix added (global short-circuit asserts `_iter_months` never
    runs; month-scoped still iterates + 403s; missing/disabled fail closed). The
    optional propose-time range cap stays a separate optional hardening. File:
    `backend/ums_smart_revenue/api/channel_account_links.py`
    (`_require_allocation_permission_for_range`).
  - ⏳ Deferred follow-up (PR #57 N2): supersede/close-range workflow to
    end-date an open-ended VERIFIED link without `reject` wiping its historical
    months. Needs a dedicated atomic cap-then-verify operation; out of this
    PR's contract. Sequence ahead of / with Spec 2b allocation consumption.
  - ⏳ Deferred follow-up (PR #57 N8, Codex): reconcile/deactivate stale derived
    `content_owner_channel_links` when a replacement import removes the backing
    source rows for a month. `upsert_owner_channel_links_from_source` is
    currently insert-only, so a derived link can outlive its evidence and keep
    a channel in `list_verified_adsense_account_channels`. Net-new deactivation
    behavior with its own locked-month interactions (must not deactivate links
    for already-closed months); no production consumer until Spec 2b, so
    sequence with the allocation engine. File:
    `backend/ums_smart_revenue/finance/channel_account_links.py`
    (`upsert_owner_channel_links_from_source`). Paired requirement (PR #57 V8d):
    the derivation existence probe `_owner_channel_link_exists` is intentionally
    active-agnostic and the read contract filters `active IS TRUE`. Today that is
    correct and the V8d scenario is unreachable — NO code path deactivates a
    `content_owner_channel_links` row (the derivation insert is its only writer,
    always `active=True`; the only `row.active = False` writes in the codebase are
    on unrelated tables: user roles, user permissions, channel groups). When this
    deactivation/reconcile path is built, the probe must become active-aware and
    REACTIVATE an existing inactive row when source evidence returns, because the
    unique key `uq_content_owner_channel_links_key` blocks inserting a fresh active
    row for the same start month — build the deactivate (N8) and reactivate (V8d)
    halves together.
- ⏳ Allocation engine (Spec 2b) — PR-1 (#58) + PR-2 (#59) + PR-3 (#60) + PR-4 (#61) + PR-5 (#62) + PR-6 (#65) + post_tax (#67) shipped: account-level
  deduction allocation compute + read. `finance/allocation.py` distributes
  ACCOUNT-grain `deduction_components` across each account's verified channels
  (`list_verified_adsense_account_channels`) by source-aligned raw-gross-proportional
  share with exact per-component conservation; `net_applicable` from
  `NET_APPLICABLE_COMPONENT_KINDS`; fail-closed UNALLOCATED on unmapped/missing/
  incomplete basis. Read-only `GET /revenue/months/{month}/account-allocations`
  (ACCOUNT-only query, `VIEW_REVENUE@global` + `VIEW_FINALIZED_PAYMENTS@finance_month`,
  REVENUE_VIEWED + PAYMENT_VIEWED). No persistence, no migration.
  PR-2 shipped (PR #59): net-revenue API + finance exports consume account-allocated
  net-applicable (TAX/DEDUCTION) lines on the missing-net path (COMPONENT_DERIVED), with
  per-channel channel_direct/account_allocated breakdown fields, a global-scope-only
  unallocated-account surface, a VIEW_FINALIZED_PAYMENTS gate (on the route's org target_scope)
  + PAYMENT_VIEWED audit on the net route, and PAYMENT_VIEWED on all finance-artifact exports.
  Net-revenue audit envelope
  changed from `audit_event` to `audit_events` (plural).
  PR-3 shipped (PR #60): a `net_revenue_usd` metric on
  `POST /revenue/channels/{channel_id}/months/{month}/explain` that reuses the PR-2 net builder
  + a shared no-drift `resolve_applicable_channel_deductions` helper to emit channel-direct +
  account-allocated deduction provenance in the existing `number_explanations` components JSON
  (read + persist, no migration, no schema change), gated by
  VIEW_FINALIZED_PAYMENTS@finance_month(month) with dual REVENUE_VIEWED + PAYMENT_VIEWED audit.
  PR-4 shipped (PR #61): the channel-direct/account-allocated deduction split now
  renders in all finance exports — per-channel columns in the XLSX Channel Breakdown +
  Deductions sheets, month-level aggregate rows in the XLSX Executive Summary +
  Company/Sector breakdown sheets, the PDF gross-vs-net table, and the PPTX deduction
  slide — backed by two additive total_channel_direct/account_allocated aggregate fields
  on MonthNetRevenueSummary (no migration, no auth/audit/allocation-math change).
  PR-5 shipped (PR #62): persisted/committed allocation — a versioned, audited
  POST /revenue/months/{month}/account-allocations/commit writes a snapshot of the
  gross_revenue_proportional compute (4 new tables: committed_allocation_runs/lines/
  unallocated/notes; month-scoped idempotency; lock-held compute; reject-on-unallocated;
  CHANGE_ALLOCATION_RULE gate + ALLOCATION_COMMITTED summary-only audit reusing
  CHANGE_ALLOCATION_RULE). Readers (net-revenue, allocation-read, exports) still compute
  live — read-switch deferred.
  PR-6 shipped (PR #65, 2026-06-03): read-switch — allocation GET, net-revenue,
  explain, and exports prefer the committed snapshot for LOCKED months (lock-aware +
  live fallback when no committed run; OPEN stays live), with lossless reconstruction and
  full allocation_source/committed_run provenance on every surface plus an export
  disclosure token. No migration / no auth / no write-path change.
  ✅ post_tax method shipped (PR #67, 2026-06-04): `post_tax_revenue_proportional`
  is now a second COMMITTABLE allocation method alongside `gross_revenue_proportional` —
  the engine/orchestrator parameterize on `allocation_method` (gross weights by source
  gross; post_tax weights by source net_revenue_usd, fail-closed omitting any
  (channel, source_kind) key with a null-net fact), the commit path is un-gated to a
  two-method allowlist (service + DB CHECK + migration 20260603_0001), the persisted basis
  field was renamed `basis_gross_usd`→`basis_amount_usd`, and `/revenue/recalculate`'s
  dry-run net check moved to (channel, source_kind) grain so READY can no longer be
  reported while commit would go UNALLOCATED.
  ✅ company_level + no_allocation methods shipped (branch
  `spec/no-allocation-company-level-methods`, 2026-06-06): `company_level` weights by
  company source-aligned gross with a flat in-company split (org-access channel→company
  index resolved at the route boundary; fail-closed COMPANY_UNMAPPED /
  COMPANY_BASIS_INCOMPLETE / ZERO_COMPANY_BASIS; requires the full SECTOR→COMPANY
  hierarchy because the access index only maps channels whose company has a sector
  parent); `no_allocation` commits a zero-line snapshot persisting every component as a
  typed INTENTIONAL_NO_ALLOCATION unallocated row (reject-on-unallocated bypassed for
  this method only; needs neither facts nor verified links). Service allowlist now four
  methods; DB CHECK widened to five (migration 20260606_0001 — 'manual' pre-cleared at
  the DB layer only, still service-rejected). Recalculate preview parity: no_allocation
  exempt from NO_REVENUE_FACTS; company_level blocks on COMPANY_MAPPING_MISSING.
  Remaining: PAYMENT-grain allocation is BLOCKED — pending live remittance/bank evidence
  + an operator-asserted (tenant_id, month, bank_reference)→account(s) receipt-assertion
  model (verified 2026-06-03: no deterministic bank_reference→account bridge exists in the
  data — see Docs/superpowers/specs/2026-06-03-spec-payment-account-modeling-design.md).
  ✅ manual method + recalculate committed write-path shipped (branch
  `spec/manual-allocation-recalc-write`, 2026-06-06): `manual` commits an
  operator-asserted per-channel split via `manual_lines` on the commit endpoint (pure
  fail-closed builder: exact per-component Decimal sums, verified channels only, ≤6dp
  non-negative amounts, every ACCOUNT component covered, out-of-range amounts rejected
  typed; the engine keeps rejecting manual — service-level allowlist only; manual lines
  fold into the idempotency fingerprint, legacy digests unchanged).
  `/revenue/recalculate` `dry_run=false` performs a real committed allocation through
  the same service path (same advisory lock / idempotency / version chain; preview runs
  as a pre-flight BLOCKED_BY_ISSUES 409 gate; write-only VIEW_FINALIZED_PAYMENTS gate;
  `idempotency_key` required when writing; cross-endpoint idempotent replay with the
  commit endpoint; manual is redirected to the commit endpoint).
- ✅ Month lock/unlock — shipped: POST /finance-close/{month}/lock + /unlock
  (readiness-gated, audited MONTH_LOCKED/MONTH_UNLOCKED, fail-closed
  permissions). Month-close status UI shipped in PR #69 (CloseView wired to
  status + readiness + lock/unlock with audited reason).
- ✅ Manual override approval — shipped: POST /revenue/manual-overrides +
  /manual-overrides/{id}/approve (create + approve flow, locked-month guard,
  APPROVE_MANUAL_OVERRIDE scope, audited).
- ✅ Audit dashboard — merged to main (PR #71, 31a7641): the
  AuditView timeline is wired to `GET /audit/events` (the tenant-scoped audit
  log backend from PR #22). Cursor-paginated read, server-driven sensitive-
  payload redaction (the UI reflects `details_redacted`, never reveals withheld
  payloads), fail-closed gate (a non-audit viewer sees the restricted
  placeholder and fires no fetch — so the self-auditing read is never spammed),
  403 → no-permission copy. Track C follow-up (`feat/audit-track-c`) finished
  the surface: Load More via `next_cursor` (append + id-dedupe + reset on
  filter change), a live event-type filter on the existing `event_type` param,
  and "Download Audit View" wired to the new `GET /audit/events/export` sync
  CSV route (same gate/filters/redaction as the list route; 10,000-row cap
  with `X-Truncated` surfaced in the UI; snapshot-before-audit
  `EXPORT_DOWNLOADED` emission; deterministic, formula-injection-guarded CSV).
  The summary tiles are now wired to the live `GET /audit/summary` aggregate-count
  route (see the audit summary endpoint entry below); the Retention tile stays a
  static policy constant.
- ✅ Rolling month window (item P1.2, PR #211, merged 2026-08-30) — the
  frozen `DEFAULT_MONTH = "2026-03"` / `MONTH_OPTIONS` literals and the AppShell
  topbar month `<select>` now all derive from `frontend/src/lib/months.ts`:
  current calendar month + the 3 before it, from LOCAL date components with an
  injectable `now`. Integration follow-ups on the same branch, from the audit of
  what the new current-month default exposed: (a) Month Close reads the
  close-status `404` as "no close record yet" — data absent, not an error — and
  renders the honest OPEN / not-started summary, because close rows are only
  created by finance writes so the rolling default month has none by
  construction; every other status still renders today's error tile.
  (b) Connectors seeds its month state (a WRITE default: the connector run's
  `report_month` and the AdSense payment month, both whole-calendar-month pulls)
  from the new `lastCompleteMonthKey` — `MONTH_OPTIONS[1]` — so accepting the
  default cannot ingest a PARTIAL month; the current month stays selectable.
  (c) `scripts/seed_demo_month.py` and `scripts/smoke_mvp.py` compute their
  default `--month` at run time (local civil date) instead of the frozen
  `"2026-03"`, and `frontend/README.md` documents seeding the current month with
  both a bash and a PowerShell form.

## P2 — Advanced features

- ⏳ Admin, access & configuration UI — Docs/23 (this consolidated program-plans PR;
  supersedes draft #218): backend already ships the account/access surface
  (list/create/patch users — no DELETE; scoped role assignment + scoped permission
  grants + catalog reads, audited with required reasons) and NO view exposes it.
  Program: **prerequisite P0-c**; A1 Admin MVP (+ `users.read_scoped`; Admin nav on
  `can_manage_users || can_assign_roles`, assignment-only surface independently gated);
  A2-owned matrix + `/security` proxy residual; A6 delegated admin with
  `home_org_unit_id` + no-amplification/read-isolation and account-global lifecycle
  reserved to global admins behind `can_manage_user_lifecycle`; A7 Google-only sign-in
  + explicit audited, one-time enrollment into active-only-unique
  `external_identities` revision history; A3–A5. Tripwires: no role editor, no secrets,
  no delegation before A6. Remaining: whole program (plan only).
- ⏳ Display-only currency conversion foundation — remaining: display-only
  conversion is not started. Note the distinction: bank-side FX + transfer-fee
  effects ARE derived as evidence-only `deduction_components` by Track F
  reconciliation (PR #87, AdSense->bank fee+FX, attributed ∝ CMS gross; only TAX
  feeds `net_revenue_usd`). Official finance ingestion must still preserve
  Google/YouTube/AdSense reported amounts and currencies per `Docs/18`;
  public/provider FX rates are not an official source for monthly revenue, tax,
  deduction, AdSense payment, or reconciliation values.
- ⏳ Anomaly detection foundation for source-backed month-over-month
  revenue movement — remaining: not started.
- ⏳ Detailed Shorts revenue handling foundation — remaining: not started.
  Shorts revenue currently flows undifferentiated through the same monthly
  channel facts; no Shorts-specific split or attribution exists.
- ⏳ Custom report builder — remaining: not started.
- ⏳ Saved dashboard views — remaining: not started.
- ⏳ User-level favorite groups — remaining: not started.
- ⏳ Scheduled report emails — remaining: not started.

## P3 — Later update

- ⏳ Title/show mapping — remaining: not started; out of scope for first
  release per Phase 0.
- ⏳ Ramadan/event title profit — remaining: not started.
- ⏳ Playlist/hashtag mapping — remaining: not started.
- ⏳ Video-level title classification — remaining: not started.
- ⏳ AI-assisted mapping review — remaining: not started.
- ⏳ Content ID/fingerprint/claims modules if needed later — remaining:
  not started; out of scope for first release per Phase 0.

## Cross-cutting shipped (not in original P0–P3)

Infrastructure delivered across S0/S1/S2 that does not map cleanly to any
single P-tier above.

- ✅ Backend foundation: FastAPI + SQLAlchemy + Alembic + pytest baseline
  (PR #1).
- ✅ Auth foundation: user accounts, roles, permission grants, DB-backed
  principal authorization, lifecycle APIs, access read APIs
  (PRs #2 – #7, #20, #22 – #24).
- ✅ Multi-tenant infrastructure: `tenants` + `platform_admins` tables;
  header resolver + contextvar; principal–tenant binding; `tenant_id` on
  18 operational tables; tenant-scoped repositories across auth, org,
  finance, connectors, reports; tenant-aware export jobs
  (PRs #18 – #34, #36).
- ✅ Trusted-gateway tenant middleware + bootstrap UMS tenant for
  SQL-backed trusted-header requests (PR #36).
- ✅ Governance + quickstart docs (PR #11).
- ✅ CI gate (Elite-CI vendored under `ci/`) + Dependabot (PRs #14, #17).
  CI-surface churn since, because this entry alone would imply more automation
  than exists: the Pullfrog workflow (PR #103, SHA-pinned in #108) was removed
  in PR #124; the CircleCI pipeline (PRs #114/#116, hardened by the Bun frontend
  lane #123, image-digest pinning #126, and binary checksum verification #128)
  was removed wholesale in PR #133; the DeepSource analyzer config (PRs
  #117/#118) was deleted in PR #160 after an earlier delete in PR #101. **Net
  surviving automation:** the vendored `ci/` gate, Dependabot, and a single
  Claude Code (`@claude`) GitHub Action workflow (PR #107) — the only file in
  `.github/workflows/` today.
- ✅ Frontend test layout unified + the JS gate lanes switched on (2026-08-09,
  branch `opus/peaceful-gauss-2bf1e6`) — the suite had been split between 38
  co-located `src/**/__tests__/` files and 3 already under `frontend/tests/`, and
  Qodo compliance rule 1052070 ("place all automated test files under the
  top-level `tests/` directory") fired on every PR touching a co-located one.
  **All 38 moved into `frontend/tests/`**, leaving **41** collected there,
  mirroring `src/` minus the `__tests__`
  segment. Pure `git mv`: every test imports through the `@/` alias, so there
  were **zero** relative-import fixes and zero content edits — 477 tests passed
  identically before and after. `test.include` in `vitest.config.ts` now
  *declares* the layout. **But `include` alone makes things worse, not better**:
  a test file outside the glob stops being collected and passes by never
  running. Verified, not assumed — a `tests/lib/silentSkip.spec.tsx` asserting
  `1 === 2` was added and the suite still reported 477 passed. So the layout is
  enforced by a new **`ci/checks/test-layout.sh`** (manifest `test-layout`,
  blocker/hygiene/pre-commit, `languages: []` so it cannot be skipped by
  language detection) which fails on four conditions: a test anywhere under
  `frontend/` outside `frontend/tests/`, a lingering `__tests__/` dir, a file in
  the right tree with a suffix the glob misses (`*.spec.tsx`), and the `include`
  no longer being *live* config. Each of the four was exercised and observed to
  exit 20. Two review findings hardened it before merge: the outside-tree scan
  originally walked only `frontend/src`, so a `frontend/e2e/x.test.ts` was
  excluded by `include` yet passed the guard; and the drift check was an
  unanchored `grep -F`, which a commented-out `include:` satisfied while vitest
  had already fallen back to its default glob. The manifest entry is
  documentation — `ci/scripts/gen-checks-doc.sh` is its only consumer — so the
  check is also scheduled in `ci/preflight.sh` (`run_common_checks` for
  pre-commit `quick`, `run_full_or_ship_checks` for `full`/`ship`) and in
  `ci/config/lanes.conf`; registered-but-never-run is the exact failure mode
  this guard exists to prevent. `ci/tests/test_test_layout.bats` pins all of
  it, including that the pre-fix script exits 0 on both evasions.
  **Rode along — the JS lanes were dead.** `ci/checks/tests.sh`,
  `ci/checks/typecheck.sh` and `ci/checks/node.sh` all `cd` to the repo root and
  bail on a missing root `package.json`/`tsconfig.json`; this repo keeps both in
  `frontend/`, so **no frontend test or type-check had ever run in the gate** —
  `tests-js` and `typecheck-js` are manifest blockers that were silently passing
  by skipping. New `ci::common::node_workspaces <manifest>` resolves workspaces
  (root-manifest repos still resolve to `.` and behave exactly as before); all
  three lanes iterate it. The lanes invoke `node_modules/.bin/` **directly**
  rather than via `npx`, because npx walks up out of the workspace when bun's
  Windows shims (`vitest.exe`/`.bunx`) don't match the names it expects — in
  this checkout it found a different Vitest major against a different Vite and
  produced ~160 phantom failures indistinguishable from real breakage. Lanes now
  green: 41 files / 477 tests via JUnit, `tsc --noEmit` clean, `vite build` ok.
  Review caught that *making the lanes work* and *making them run* are two
  different things, in three places. (1) `ci/config/checks.yml` had all four JS
  checks `enabled: false`, and preflight drops a lane when every related check
  is disabled — so the whole `node` lane was still skipped. `tests-js` and
  `typecheck-js` are now `true`; `lint-js`/`format-js` stay off because the
  workspace has neither eslint nor prettier configured, and their stale "no JS
  in v1.0" comments now say so. (2) `ci/config/affected.yml` mapped only
  root-relative `src/**`, so a frontend change produced no JavaScript patterns;
  when a Python file changed alongside it, `AFFECTED_TESTS` was non-empty and
  `tests.sh` logged "skipped: no affected JavaScript tests" while frontend code
  had in fact changed. Frontend-prefixed rules added. (3) A failing package
  script exits 1, which is not a gate result code — `result_severity` ranks it
  with `FAIL_INFRA`, so a real regression was reported as broken infrastructure.
  New `ci::common::normalize_result` maps off-contract codes onto
  `FAIL_NEW_ISSUE`; `node.sh` normalizes each workspace result before merging.
  `ci/tests/test_js_lane.bats` pins all three, and the pre-fix config was re-run
  to confirm each assertion actually failed before.
  A second review round found three more layers of the same "wired but never
  runs" problem, plus three narrower defects. (4) Scheduling `test-layout` in
  `preflight.sh` was still not enough: `_check_should_skip` filters any lane
  absent from both the changeset reverse-mapping and the always-run list, and
  the changeset only ever emits *language-derived* check ids — never
  `test-layout` — so the guard was discarded whenever the diff did not look
  like JavaScript, which is exactly when a misplaced test goes unnoticed. It is
  now an always-run check alongside `git-safety` and `changed-files`. (5)
  `ci/lib/changeset.sh` classified `package.json` as `json` and `bun.lock` as
  `unknown`, neither of which emits JS check ids, so a dependency bump or a
  changed script skipped the whole node lane — no install, tests, typecheck or
  build. Manifests and lockfiles now classify as `javascript`. (6) Pinning
  every off-contract exit to `FAIL_NEW_ISSUE` was too broad: `bun install
  --frozen-lockfile` failing on a registry outage is provisioning, not code, so
  each install now pins its own `FAIL_INFRA` exit and keeps that meaning
  through `normalize_result`. (7) The guard's scans missed the `.mts`/`.cts`/
  `.mjs`/`.cjs` suffixes that vitest's *default* glob collects, so a
  `tests/foo.test.mts` read as a real test to every tool except the include
  that would run it — verified: the pre-fix guard exits 0 on exactly that tree.
  (8) `frontend/src/**/*.ts` does not match `frontend/src/test-setup.ts`,
  because the matcher collapses `**` to `*` and then requires a path segment;
  that file was covered only incidentally by the `frontend/*.ts` rule, so the
  direct children are now spelled out. (9) `ci/UMS_INTEGRATION.md` still
  advertised all four JS checks as disabled.
  A third round found three more. (10) The prune list used unanchored
  `-name 'build'`/`'dist'`/`'coverage'`, which prunes a *first-party*
  `frontend/e2e/build/` at any depth — a test under it is neither collected by
  vitest nor reported by the guard. Build-output prunes are now exact paths at
  the workspace root; `node_modules` stays unanchored because nested copies are
  real and never first-party. (11) The install cache was fingerprinted on the
  lockfile alone, so a `package.json` edited without a matching lockfile update
  looked cached and the frozen install that would have caught the mismatch was
  skipped; the fingerprint now covers both. (12) Deleting the `test` script
  removed the entire frontend suite while the lane still exited 0 —
  `run_script` only logs `Skipping missing script`. The lane now fails closed
  when a workspace ships test files but defines neither `test` nor
  `test:unit`, keyed on the files rather than on `checks.yml` so the rule
  travels with the workspace.
  A fourth and fifth round closed the last of it. The drift guard was bound to
  any active `include:` anywhere in the config, so an `optimizeDeps.include`
  carrying the glob satisfied it while `test.include` ran one file; and once
  that was fixed by brace-matching a `test: { }` block, the *first* such token
  in the file still won, so a helper object declared above `defineConfig` could
  shadow the exported config. The extractor now anchors on `export default`,
  brace-matches the exported object, and takes the `test` key at that object's
  own level. `ci/checks/tests.sh` and `ci/checks/typecheck.sh` both fell back to
  a `jest`/`vitest`/`tsc` found on `PATH` — not the pinned version, and the
  cause of the ~160 phantom failures recorded above; both fallbacks are gone.
  `affected.yml` gained root-level `frontend/tests/*` rules and lockfile
  mappings, and `changeset.sh` now classifies web build inputs (`html`, `css`,
  `tsconfig.json`) as `javascript` so a change to the entry document or the
  stylesheet still schedules the lane.
  A seventh round closed the last four. The guard scanned the **worktree** while
  git commits the **index**: a partially staged move left
  `frontend/src/probe.test.ts` staged and moved on disk, and the guard passed a
  commit whose test is never collected. Candidates are now the union of the
  filesystem walk and `git ls-files`, so a stray file that is only staged, or
  only on disk, fails either way. `test.exclude` was unchecked, so a correct
  include could still collect nothing — `exclude: ["tests/lib/**"]` drops 23 of
  41 files while every include assertion passes; the guard now refuses an
  exclude it cannot evaluate. `.mts`/`.cts` were missing from the changeset
  classifier, so module-suffixed **source** scheduled no lane at all. And the
  bats suites themselves were never scheduled: a change to `ci/checks/*.sh`
  emitted only `lint-shell` and `format-shell`, so the tests guarding this gate
  ran only by hand. New `ci/checks/tests-shell.sh` wrapper (preflight executes a
  script path directly and cannot carry `CI_GATE_CHECK_ID`), scheduled in
  `run_full_or_ship_checks` and `lanes.conf`, with `tests-shell` added to the
  shell language mapping so the lane is not filtered straight back out.
  An eighth round found four more, two of them P1. The drift check still read
  only the **worktree** `vitest.config.ts` after the candidate list moved to the
  index: staging a narrowed `test.include` and restoring the good config on disk
  passed the guard. Both copies are now checked and the failure names which one.
  The new `tests-shell.sh` was committed **100644** — `core.fileMode=false`
  means a local `chmod` is not what git records, and `run_phase` executes the
  path directly, so the lane would have exited 126 before any test ran; the bats
  case asserting `[ -x ]` passed because it tested the worktree, the same
  index-vs-worktree blindness the P1 above describes. It now asserts
  `git ls-files -s` mode. A missing `bats` made the enabled blocker log
  `skipped: bats not installed` and report PASS having executed nothing —
  `uv sync` does not provision bats — so the wrapper now exits `FAIL_INFRA`.
  And `htm`/`sass`/`less`/`vue`/`svelte`/`astro` were classified as javascript
  but unmapped in `affected.yml`; since that classifier/mapping gap had recurred
  three times, the bats case now **derives** its extension list from
  `changeset.sh` rather than hand-listing files.
  A ninth round found three more, one P1. `test-layout`'s result was **cached**:
  `_changeset_content_hash` hashes worktree copies of the changed paths, so
  staging a narrowed `vitest.config.ts` while keeping the passing worktree copy
  leaves both the path list and the content hash identical, and the guard would
  serve a cached PASS for a commit it never inspected — defeating the
  index-awareness added the round before. A `_check_is_cacheable` predicate now
  excludes it on both the read and the write side; the check costs under a
  second, so caching it bought nothing. A `ci/config/*.yml`-only change emitted
  `lint-yaml` and nothing else, filtering the `tests-shell` lane even though
  `test_js_lane.bats` asserts on `affected.yml` and `checks.yml` directly — the
  lane now also runs when any `ci/` path changes, keyed on path rather than
  language so unrelated yaml does not pull in a four-minute suite. And the
  `.gitignore` negations added earlier covered only `frontend/tests/lib/` at the
  tree root, so a mirrored `frontend/tests/features/lib/widget.test.ts` was
  still silently untracked — the same defect this PR already fixed, one level
  deeper. Negations now apply at any depth under both `tests/` and `src/`, with
  a case asserting `node_modules` is still ignored.
  A twelfth round found three more, one P1. `config_sources` reads the staged
  copy of `vitest.config.ts` only when one exists in the index, so staging the
  file's **deletion** while leaving a valid copy in the worktree validated the
  worktree alone: the guard exited 0 for a commit that would carry no vitest
  config at all, leaving vitest to fall back to its default glob — precisely the
  layout this check rejects. `config_missing_from_index` now fails that case
  with the staging command in the message, and stays inert where there is no
  index (a tarball or plain export). The `tests-shell` path exception added in
  round nine was **unreachable**: `ci/`, `.githooks/` and `.gitignore` classify
  into no language, so `preflight.sh` hit its "no relevant changes" fast exit
  before `run_mode` ever called `_check_should_skip` — a regression in the
  `.gitignore` negations could ship with its own test never running. The fast
  exit now also consults the raw changed-path list for the gate's own inputs,
  and still fires for a genuinely irrelevant commit. Finally, `classify_file`
  returned `unknown` for `frontend/.env` and anything under `frontend/public/`,
  so a change to what actually ships scheduled no install, typecheck, tests or
  build. Rather than extend the extension table a fourth time, the fallback now
  anchors on `package.json`: a file inside a Node workspace tree is a build
  input for that workspace. It is the last resort — after the extension table
  and both sniffs — so `frontend/scripts/build.sh` stays `shell` and
  `frontend/README.md` stays `markdown`, and it does not reach a repo-root
  dotfile or a backend asset. `affected.yml` gained the matching `frontend/*`
  catch-all so the lane it schedules finds tests instead of reporting none; the
  per-extension rules stay, because the bats case that derives its list from the
  classifier is what keeps the two files from drifting.
  The last finding held open for the owner is now fixed too: the node lane
  accepted whatever `node` and the package manager resolved to, so a green run
  vouched for nothing — the same commit could pass here and fail on a machine
  matching `frontend/package.json`. The lane now evaluates `engines.node` and
  the `packageManager` pin **before** installing or running anything, and exits
  `FAIL_INFRA` (a non-conforming toolchain is a broken environment, not a code
  regression). The comparator handles `>=`/`>`/`<=`/`<`/`=`/bare/`^`/`~`,
  wildcard components, space-conjunction and `||` alternation; a range it does
  not recognise is reported as unverifiable rather than assumed to hold, since
  failing open there would reinstate the finding. `packageManager` is compared
  as the exact pin corepack means, with the `+integrity` suffix stripped, and
  its name must be the manager the lockfile selects. A manifest that declares
  neither is untouched. This was previously deferred because it would have
  blocked local commits on the owner's Node 20.20.2 / bun 1.2.14; the machine
  now reports **v22.14.0** and **bun 1.3.14**, and the full lane (install,
  `tsc --noEmit`, 477 tests, `vite build`) passes under enforcement.
  A thirteenth round found six more, two P1 — five of them regressions in the
  round-twelve code, which is the cost of adding enforcement. The node lane
  returned PASS when **workspace discovery found nothing**: deleting
  `frontend/package.json` leaves the lockfile, tsconfig and vitest config
  behind, so the lane ran no install, typecheck, test or build while
  `test-layout` still passed on the surviving config. That branch now fails
  when workspace *configuration* remains without a manifest — keyed on config
  rather than on source files, because this branch is also where a repo with no
  JavaScript at all lands. The **result cache** outlived the toolchain it
  vouched for: `_compute_cache_key` fingerprinted only `node` for the node lane
  and only `bash` for `tests-shell`, so moving bun off the `packageManager` pin
  or uninstalling bats replayed the old PASS and the new fail-closed checks
  never executed. Both keys now include every tool their lane consults, absent
  ones recorded as `absent`. The comparator read `"node": "20"` as `20.0.0` and
  rejected Node 20.20.2 — npm's X-range semantics say an unstated component is
  unconstrained, and a false infra failure is how a fail-closed check gets
  switched off; a `cut` left in the wildcard path was the cause. It also
  accepted `">=999.0.0 ||"`: the token counter was cumulative across
  alternatives, so an empty alternative from a trailing `||` inherited the
  previous one's count and its untouched `ok` flag. Malformed input is now
  unverifiable however well another alternative matches, while an
  *unrecognised* range form still loses to a satisfied sibling — the two are
  deliberately different. In `test-layout.sh`, a **quoted** `"exclude"` was
  invisible to the identifier-only pattern, so an exclusion dropping whole test
  directories read as clean; quoted `"include"` and `"test"` failed the other
  way, reporting valid config as broken. And the `lib/` negations used
  `!<dir>/**`, which re-includes every descendant outright and overrode the
  artifact and secret rules above them — `ci/lib/__pycache__/x.pyc`,
  `ci/lib/x.pyc` and `ci/lib/.env` had all become trackable. Un-excluding the
  directory alone is sufficient and is what the file now does: git skips rule
  evaluation only *inside* an excluded directory, so its contents are matched
  normally once it is back. Re-stating the artifact rules after the negations
  would have re-ignored `.env.example`, which has its own negation further up.
  A fourteenth round found six more, three P1, all in the round-thirteen code.
  Node workspace discovery read the **filesystem**, so staging the deletion of
  `frontend/package.json` and restoring it in the worktree passed both
  pre-commit and pre-push for a commit carrying no manifest — the same
  index-vs-worktree divergence already fixed in `test-layout.sh`, one file over.
  The comparator accepted **malformed operands**: `">=banana"` and a bare `">="`
  both parsed to `>=0.0.0` and admitted everything, so a typo in a manifest
  switched the toolchain boundary off; operands are now validated before they
  reach the comparator and report unverifiable otherwise. `"~20"` was rejected
  because the tilde branch compared the minor unconditionally — `~20` is the
  whole of major 20, and only `~20.1` pins the minor (the same fix applies to
  bare `^0`). `extract_test_block` read **through string literals**, so a decoy
  resembling `test: { include: [...] }` in a string at the exported object's own
  level was taken for the config and the real, narrowed include was never
  checked; the scanner now treats strings as opaque, inspects them only for a
  quoted key, and copies them verbatim into the captured block — which also
  fixes the latent case of an unbalanced brace in any string, the declared glob
  itself being full of braces. `tests-shell` was still **cacheable** although
  its result depends on the whole `ci/` tree and on files a changeset need not
  mention, so a PASS cached for a `.gitignore`-only branch survived a rebase
  onto a base carrying a regression in a gate script; it joins `test-layout` as
  non-cacheable, which covers inputs a key never could, and the now-unreachable
  `tests-shell` key branch was removed with it. Finally, deriving a cache key
  could **end the run**: under `set -Eeuo pipefail` a bare
  `$(tool --version | head -1)` aborts preflight the moment a present-but-broken
  package manager exits non-zero, before any check has run; probes go through
  `_tool_fingerprint`, which cannot.
  A fifteenth round found eight more, four P1. The staged-manifest check
  verified only that `package.json` **existed** in the index, while every
  decision below it — engines, `packageManager`, the dependency fingerprint,
  whether a `test` script exists — still read the worktree copy; index/worktree
  divergence is now rejected outright, because a rule applied at each of a dozen
  reading sites is a rule that gets forgotten at one of them. A workspace could
  **lose its entire suite**: delete every file under `tests/` and both scripts
  and nothing is orphaned, `test-layout` reports "0 file(s)", and a successful
  build carries the gate to exit 0 — a workspace whose HEAD carries tests must
  still have them. A package script exiting **10** propagated through as
  `PASS_WITH_KNOWN_DEBT`, so a failing lane was recorded as passed and the
  remaining scripts skipped; every nonzero script status is now
  `FAIL_NEW_ISSUE`, decided in the workspace child rather than relying on the
  parent's normalisation. The operand grammar check was **first-character and
  charset only**, so `>=20banana`, `>=20..1` and `>=20.1.2.3` all parsed as
  `>=20.0.0`; the whole shape is now matched, for bare operands as well as
  operator ones. `"=20"` was routed through exact comparison and rejected a
  conforming runtime — node-semver normalises it to `>=20.0.0 <21.0.0-0`, so it
  takes the X-range path like every other partial. A **spread** in the test
  object (`test: { ...hidden, include: [...] }`) carried in an exclude the guard
  never saw; a spread it cannot evaluate is now a failure. The **node** lane was
  still cacheable although it runs the workspace's complete test, typecheck and
  build scripts, so a PASS cached for one frontend change survived a rebase onto
  a base with a regression in another; it joins `test-layout` and `tests-shell`
  as non-cacheable, and the unreachable key branches went with them. And
  `frontend/README.md` was missing from the `tests-shell` dependency paths even
  though the suites assert on its prose — a README-only change classifies as
  markdown, scheduling `lint-markdown` and nothing else.
  A sixteenth round found three, one P1 — the first round to shrink. The
  manifest-divergence rule from round fifteen was both **too narrow and too
  wide**: it compared only `package.json`, while the lane installs, typechecks,
  tests and builds every file in the workspace (staging a failing source file
  and restoring the passing copy on disk reported "Node lane passed"), and it
  rejected *any* divergence, so an ordinary edited-but-unstaged manifest failed
  the lane — a false positive that would have made the gate unusable by hand.
  The rule is now **partial staging**, across the whole workspace: a file whose
  index copy differs from HEAD *and* whose worktree copy differs from the index.
  Comparator-prefixed partials still used zero-filled comparison, so `<=20`
  rejected 20.20.2 and `>20` admitted it; node-semver reads a partial as an
  X-range, and both comparators now use the same upper bound (`20` → `21.0.0`).
  And the exclude/spread greps were **line-anchored**, so the compact
  `test: { include: [...], exclude: [...] }` slipped past: properties are now
  split on commas at the object's own depth with strings opaque, which makes
  the check independent of formatting and of quoting in one move.
  A seventeenth round found seven, three P1, and two of the fixes invert a rule
  rather than extend it. Workspace discovery still stated the **filesystem**, so
  a workspace staged into the commit and then removed from disk was never
  visited at all; the index is now a second source of workspaces, and one
  staged-but-absent is a failure rather than a skip. A workspace could declare
  TypeScript and define **no `typecheck` script**: `run_script` logged "Skipping
  missing script" and the lane exited 0 after tests and a `vite build` that,
  as `frontend/README.md` says, never runs `tsc`. `">= 20"` — valid npm, with a
  space — was split into a bare `>=` and rejected as malformed, blocking a
  conforming environment; operator and operand are rejoined before evaluation.
  `testNamePattern` made vitest exit 0 with **every test reported skipped**
  while the guard called all 41 files runnable; that is the third route to a
  silent drop after `exclude` and the spread, so the rule is now an **allow-list**
  — a property that cannot reduce what is collected is named in the script, and
  anything else stops the guard until someone decides which it is. An `include`
  computed by an expression hid the declared glob in the branch vitest does not
  take, so the value must be a literal array. A **rename** was reduced to its
  destination before classification, so `R100 frontend/src/x.ts Docs/x.md`
  yielded `lint-markdown` alone; both sides are classified now. And workspace
  membership was consulted only in `classify_file`'s unknown fallback, so a
  *recognised* type never reached it — `frontend/src/data.json` scheduled
  nothing — which makes membership a second signal alongside the language
  rather than a last resort.
  The ship gate itself then turned up an eighth, which no reviewer had raised:
  `tests-shell` came back **FAIL_INFRA at exactly 1200s** -- the gate's default
  per-check timeout. The suites had grown past it, so the blocking lane was
  being killed rather than run, announced in one word at the end of a two-hour
  gate. `checks.yml` has carried a per-check `timeout_sec` since before this PR
  and **nothing read it**; the runner applied the global value to everything.
  The runner now honours the declared value, and `tests-shell` declares 3600.
  An eighteenth round found five, two P1, one of which undid a round-seventeen
  fix a line after it landed: `emit_json` **recomputed and wrote back**
  `_CI_CHANGESET_LANGUAGES` and `_CI_CHANGESET_CHECKS` from its own view, which
  still collapsed a rename to its destination — and `preflight.sh` calls it
  immediately after `detect`, so the scheduler's correct classification was
  replaced by the report's wrong one. The report generator no longer assigns
  scheduler state at all; it serialises it, so the two cannot disagree by
  construction. The partial-staging rule missed a **staged deletion recreated as
  an untracked file** (`D  app.js` plus `?? app.js`): `git diff` compares
  tracked content, so the intersection stayed empty and the lane tested a file
  the commit deletes. A **malformed operand was outvoted by a satisfied
  alternative** — `">=20banana || >=20"` came back satisfied — because invalid
  operands shared a status with merely-unsupported range forms; they are now
  distinct, and only the unsupported one can lose to a sibling. `include` was
  checked for *starting* with `[`, so `[...].slice(1)` passed while vitest
  received one element; the bracket's match must now be the value's last
  character. And the per-check timeout added the round before applied only on
  the parallel path, so `CI_GATE_PARALLEL=0` ignored both it and the global
  timeout and could hang indefinitely.
  A nineteenth round found four, one P1, and it is the clearest instance yet of
  a rule being right about the wrong tree. The partial-staging guard added in
  round fifteen compares the index against the worktree, which is the correct
  reference for exactly one gate: `quick`, the pre-commit hook. In `ship` — the
  **pre-push** gate — the commit already exists, the index matches `HEAD`, and
  `git diff --cached` is empty, so the guard never fires. A workspace committed
  broken and repaired only on disk therefore passed the gate on the strength of
  the repair, and the push carried the broken commit. The reference tree now
  follows `CI_GATE_MODE`, which `preflight.sh` exports: `ship` stands behind
  `HEAD`, every other mode behind the index. Note what the changeset mode could
  not have supplied here — `--all` rewrites it to `all` without changing what
  the run is vouching for. Three P2s: `~20.x` **states a minor textually and
  none semantically**, and testing the operand for a non-empty second component
  pinned the upper bound at `20.1.0`, rejecting a runtime npm reads as in range;
  the array-literal check accepted `[...(cond ? [glob] : [])]` because it only
  constrained the **brackets**, so every element must now be a plain quoted
  string; and the `export default` anchor was still a raw substring search while
  everything read after it had already been made string-aware, so a quoted
  `export default` anywhere earlier re-pointed the whole extraction at prose.
  A twentieth round came from a surface the review-thread audits had been
  missing entirely: Qodo files its findings in a **pinned issue comment**, not
  as review threads, so "every thread resolved" was a true statement about the
  wrong list. Nine findings were open there. Six were real. The comparator had
  three, all in operand handling rather than in the comparisons themselves:
  node-semver resolves an X-range **major** before any comparator runs — `>=x`
  and `<=x` become `*`, `^x` and `~x` become `*`, `>x` and `<x` become nothing —
  and reading the wildcard as `0` got four of those six backwards; `^0.0.3` is
  `>=0.0.3 <0.0.4`, so pinning only the minor admitted `0.0.9`; and any `x` in
  an operand makes everything to its right an `x`, so `>=20.*.3` is `>=20.0.0`
  and was instead compared against `20.0.3`. `_semver_upper_bound` had always
  truncated at the first wildcard, which is why `<=20.*.3` was right while
  `>=20.*.3` was wrong — the operand is now normalised once, after the grammar
  check, so every comparator reads the same range. In `preflight.sh`,
  `\.gitignore$` and `frontend/README\.md$` were anchored on end of line, but
  `_CI_CHANGESET_FILES_RAW` holds `STATUS<TAB>PATH` records and a rename is
  `R100<TAB>old<TAB>new`: renaming `.gitignore` away filtered out the very suite
  that guards it. Three were test-side: the sequential-timeout case asserted
  `rc=124` unconditionally, which fails on a machine with neither `timeout` nor
  `gtimeout` for the one reason the runner is entitled to (both branches are
  asserted now, rather than skipping either); the fingerprint fixture hashed
  with `sha256sum` while production uses `ci::common::hash_file` and its
  fallbacks; and two cases extracted shell functions with `sed` ranges anchored
  at column zero, so re-indenting a file would have failed the test rather than
  the code.
  Three of the nine did not survive checking, and each disposition is pinned by
  a test rather than by an argument: `20.*.3` is **not** malformed — npm accepts
  it, and rejecting it would fail a manifest that installs; `frontend/src/*.mts`
  **does** match `frontend/src/lib/x.mts`, because the matcher collapses `**` to
  `*` and compares with `case`, where `*` crosses `/`; and `tr -d ' \t'` is
  POSIX, though it is now written `tr -d $' \t'` so the tab cannot depend on
  `tr` interpreting the escape.
  A twenty-first round was self-found, by going looking for the round-nineteen
  P1's *shape* rather than for more instances of it: a check that is correct
  about a tree the gate is not standing behind. `git-safety.sh` was the same
  defect on the security path. Every content scan it runs — sensitive files,
  `node_modules`, virtualenvs, build output, blobs over 5MB, conflict markers
  and the secret-pattern diff — read the index, so in `ship` mode, where the
  index matches `HEAD`, all seven inspected an empty diff and the pre-push gate
  passed. Demonstrated rather than reasoned: the identical `secrets.env` exits
  20 staged and exited 0 once committed. The reference now follows
  `CI_GATE_MODE`, and the push range is computed once in
  `ci::git::push_range` — `branch-protection.sh` had its own copy, and a second
  copy of a computation is exactly how the changeset scheduler and its report
  drifted apart in round eighteen. Fixing it surfaced a second, older defect in
  the same loop: the file list included **deletions**, so committing
  `git rm secrets.env` was blocked for the file it removes. The list is now
  additions, copies, renames and modifications, which cures the index form too.
  A twenty-second round found eight, four P1, and every one of them is a
  **guard that fires only in the arrangement its author happened to picture**.
  The suite-loss check sat under "and no test script", so deleting all 41 test
  files was safe as long as `vitest run --passWithNoTests` stayed behind — the
  one script somebody actually leaves. The orphan-configuration scan sat under
  "the repository has no workspace at all", which made an orphan a property of
  the repository rather than of the directory it sits in: with two siblings,
  deleting `b/package.json` left `a`, and `b` was never looked at. The
  partial-staging rule used `git ls-files --others --exclude-standard`, so a
  file recreated after its deletion was staged became `!!` rather than `??` the
  moment `.gitignore` covered it and dropped out of the intersection — it now
  asks each staged path directly, because whether a path is ignored has nothing
  to do with whether the lane is about to read it. `_in_node_workspace` walked
  up to `.` and stopped without examining the only directory it had not looked
  at, so in a root-manifest repository nothing was inside a workspace. The
  literal-array rule accepted any backtick value, and `` `${cond ? glob : one}` ``
  is quoted by every test it applied. The `export default` anchor was made
  string-aware two rounds ago and was still blind to the other delimiter a JS
  file can hide text behind, so `/export default/` re-anchored it; both scans
  are now regex-aware, with the division case pinned from the other side. And
  `tests-shell` had no HEAD/worktree guard at all — Codex found the twin of the
  defect I had just reported finding myself, in the lane next door.
  One report did not reproduce: `~20.x.1` was already cured by the operand
  normalisation in round twenty, which truncates at the first wildcard before
  any comparator runs. Pinned rather than argued.
  A twenty-third round found three, and two of them were **defects in the
  round-twenty-one fix itself** — filed independently by both reviewers, which
  is the clearest signal in the whole sequence that the fix had been reasoned
  about rather than attacked. `git diff base..HEAD` collapses the endpoints, so
  a token added by one outgoing commit and removed by a later one vanished from
  the diff while both commits were pushed and the blob stayed in the history
  forever; the scan now walks `git rev-list` and reads what each commit added,
  and the blob-size check takes the maximum across the range rather than
  whatever survives at `HEAD`. And `HEAD~1` sat in `ci::git::push_range` as a
  "last resort" when it is nothing of the kind: on the first push of a
  three-commit branch with no upstream it silently reduced the range to the
  final commit, so a secret in the first sailed through a gate reporting on the
  third. A wrong base is worse than no base, because it produces a confident
  green — the order is now the hook's own SHAs, `@{push}`, `@{upstream}`, the
  merge base with the remote default branch, and only then the whole of `HEAD`.
  The third: `"test": "vitest run -t no-such-name"` exits 0 with every collected
  test skipped, and the layout guard sees an untouched config. That is the
  config-level filter it already rejects, one layer out, and it is the same
  sentence as the typecheck rule — that a script *exists* says nothing about
  whether it runs anything.
  A twenty-fourth round found three, all of them **the previous round's fix,
  applied one step short of where the same argument leads**. `.gitignore` had
  already defeated the pre-commit drift rule once; the ship-mode branch and the
  new `tests-shell` guard both used `--exclude-standard` and fell to the
  identical trick — an outgoing commit that deletes a path and ignores it makes
  the worktree replacement invisible to `git diff HEAD` (HEAD has no such path)
  and to the untracked list (documented to drop it) at the same time. Both now
  consult the ignored list as well, pruned to the directories a workspace is
  *expected* to ignore, because taking it whole means `node_modules` is drift
  and the ship gate never passes. And the regex-aware anchor read `return /x/`
  as division, because the character before the slash is the `n` of a keyword:
  an identifier character does not settle the question, so the whole preceding
  word is read and checked against the keywords a value may follow.
  A twenty-fifth round, from the pinned Qodo review, found the **fail-open under
  the fail-closed rule**: every ship-mode scan reached its commit list through
  `git rev-list … || true`, so a range that could not be walked produced no
  commits, no scanning, and a confident PASS on the security path. "Nothing to
  push" and "the walk failed" are indistinguishable in the output and must not
  be indistinguishable in the result. The commits are enumerated once now, and
  a failed enumeration is `FAIL_INFRA`. The same masking sat in
  `branch-protection.sh`, where it decided whether every pushed commit is
  signed. And `push_range`'s no-base case emitted `<empty-tree>..HEAD` — a tree
  object on the left of a revision walk, which is exactly the input that makes
  the walk fail; a first push is every commit reachable from `HEAD`, and it now
  says so.
  A twenty-sixth round was self-found, by building the oracle the earlier
  comparator rounds had been reasoning without: node-semver 7.8.5 itself, driven
  over a 585-case table. It found five defects in the comparator, and the
  headline one is that **prerelease precedence was never implemented and was
  answered anyway**. Prerelease ordering is a different ordering — `1.2.3-alpha.1`
  is *below* `1.2.3`, and `alpha.7` above `alpha.3` by identifier rules, not by
  any comparison of three numbers — so ignoring the tail did not make those
  cases unsupported, it made them wrong in the fail-open direction: measured,
  `1.2.3-alpha.1` came back satisfied by `1.2.3`, `<=1.2.3-alpha.1` by `1.2.3`,
  and a plain `20.1.0` by a `20.1.0-rc.1` runtime, all npm=false. It is declared
  unsupported now, on both sides, and an unsupported form stops the lane.
  The prerelease/build **grammar** was the loose charset rather than
  node-semver's identifier rules, so `1.2.3-01`, `1.2.3-a..b` and `1.2.3+.` — every
  one of which npm refuses to parse — were accepted, and `1.2.3-01 || >=20` came
  back SATISFIED for a range that cannot be constructed at all. `_semver_num_ok`'s
  15-digit cap was neither node-semver's boundary (`MAX_SAFE_INTEGER`, sixteen
  digits) nor bash's (nineteen), so it rejected the exact value npm documents,
  fail-closed across all of `[1e15, 2^53-1]`, and because an oversized operand
  is classed *malformed* it poisoned siblings that plainly admitted the runtime.
  Two findings were the previous round's own fix over-applied. `invalidXRangeOrder`
  was put inside the operand predicate and therefore reached every operand — but
  node-semver calls it from `replaceXRange` alone, so `~22.x.1` and `^20.*.3`
  are valid in every published version and the gate refused them; `hyphenReplace`
  never routes through it either, and hyphen endpoints also absorb a leading `=`
  and admit 16-digit numbers, so `= 20 - 22`, `20.x.3 - 22` and
  `1000000000000000 - 2000000000000000` were all read as typos rather than as the
  hyphen ranges npm builds — and a missed hyphen range is malformed, which
  poisons the sibling the hyphen handling exists to let win. The rule itself was
  also dated: diffing `classes/range.js` across 7.8.0, 7.8.3, 7.8.4 and 7.8.5
  shows `invalidXRangeOrder` arrives in **7.8.4**, so `20.x.3` is valid before it
  and invalid after; the gate refuses it, which is the fail-closed reading.
  One divergence from npm is deliberate and is documented as such at the code:
  an empty alternative from a stray `||` is ANY to node-semver (7.8.5 gives
  `new Range(">=999.0.0 ||").range == ""`, `.test("20.1.0") == true`), and
  copying that would let one keystroke nullify a declared constraint, so it is
  classed malformed. Finally the unreadable-runtime message was pointing at the
  wrong thing — a broken `node --version` printed "Cannot evaluate the
  engines.node range declared by …/package.json", sending an operator to stare
  at a healthy manifest; unreadable and prerelease runtimes now each report
  against the runtime. The comparator is re-measured at 585 cases with **zero
  rows where it says satisfied and npm does not**, and zero shell errors; the
  48 remaining disagreements are all `unverifiable`, which stops the lane.
  A separate sweep for the same shape one layer up — a guard that cannot tell
  "I found nothing" from "I could not look" — found four more, all fail-open and
  all reproduced. `branch-protection` **could never be scheduled**: nothing
  anywhere emits it as a changeset check id, so the only arm that could keep it
  was unreachable and the filter dropped the lane on every run in every mode,
  including a direct commit on `main` (the check alone exits 20 there; through
  the gate it printed "Skipping [branch-protection] (filtered)" and exited 0).
  Which branch you are on is not a function of the changed-file list, so it
  joins the always-run set. `ci::changeset::detect` wrapped every git call in
  `|| true`, making "git is broken" and "nothing is staged" the same empty
  result — and the caller reads an empty result as "no relevant changes" and
  exits 0 before a single check runs: with only `git diff` failing, a tree
  carrying two staged secrets passed the gate while `git-safety.sh` run directly
  against it exited 20. Detection failure is now reported, and the caller
  answers it by running everything rather than nothing. Its pre-push branch also
  resolved the push base a **second** way, contradicting the comment in `git.sh`
  claiming otherwise: `push_range` falls back to the whole of `HEAD`, this fell
  back to nothing. It calls `push_range` now. And `push_range` itself accepted a
  *local* default-branch guess equal to the tip — on a branch named `main` with
  no remote, `merge-base HEAD main` is `HEAD`, the range is empty, and every
  ship-mode check reports "nothing changed" over a push carrying the whole
  branch; a guess that resolves to ourselves is discarded, while a
  remote-tracking ref equal to the tip still means what it says. `python` was
  cacheable although it runs `ruff`, `pytest` and `compileall` over the whole
  package: a syntax error in an unstaged file is invisible to a key built from
  the changed-file list, and a cached run returned PASS where `CI_GATE_NO_CACHE=1`
  returned `FAIL_INFRA` on the identical tree.
  One more came out of fixing a test rather than reading the code.
  `_check_disabled_in_config` was a `while read` loop piping every line of
  `checks.yml` into six separate greps and seds, and on this repository's
  213-line file **a single call took 34 seconds** — measured, after a new case
  appeared to hang and turned out merely to be waiting. `_check_should_skip`
  makes one call per lane plus one per related check, so `quick` mode spent
  minutes deciding what to run before it ran anything, against a mode that
  declares a pre-commit budget. It is one awk pass now: all 32 ids resolve in
  **1 second**, the parse is unchanged line for line, and the two
  implementations were run against each other over a fixture covering every
  branch — nested and list forms, comments on the id line and on the value, a
  check with no `enabled:`, and a top-level key that ends the block mid-file —
  agreeing on all of them.
  Two of the new cases were weak on their first draft and were fixed before
  landing, which is the same discipline applied inward: the `git diff` shim test
  passed on exit **127** — the shim could not find git, so it never exercised
  the path it named — and now resolves the real binary and asserts the premise
  both ways; and the preflight helper extractor silently produced a file whose
  callees were missing, where "command not found" is non-zero and reads as a
  decision. Separately, `ws_setup` never copied `ci/lib/git.sh` into its
  sandbox, so every case in that file aborted on the missing source before its
  first assertion.
  A twenty-seventh round, from the pinned Qodo review and from DeepSource, found
  three — and two of them are the reason a threads-only audit is not an audit.
  Qodo files its findings in a **pinned issue comment**, not in review threads,
  so the GraphQL `reviewThreads` sweep that showed zero unresolved was reporting
  on a surface those two findings do not live on. One was a **weak test of
  mine**: the case asserting that an unwalkable push range fails closed stubbed
  `ci::git::push_range` with `export -f`, and two separate things defeated that
  — bash will not carry a function whose name contains `::` into a child, and
  `git-safety.sh` sources `ci/lib/git.sh` itself, redefining the function over
  whatever the environment supplied. What the case actually measured was the
  ordinary secret scan, exit 20, and `[ "$status" -ne 0 ]` accepted it; the
  assertion named the fail-closed path while exercising the path beside it. The
  stub is written into the sandbox's own `git.sh` now, the premise is asserted
  (`git rev-list` on that range really does fail), and the expectation is **30**
  rather than merely non-zero — 20 being precisely the answer it used to get.
  The second was live code: `branch-protection.sh` captured `2>&1` into the
  string it then parses as `%G? %H` records, so a git *warning* — dubious
  ownership, a replace-ref advisory, anything git chooses to mention while
  succeeding — arrived as a record whose first word is not `G/U/X/Y`. Measured
  against the pre-fix script, verbatim: `Unsigned or unverified commit detected
  dubious ownership in repository (status: warning:)`. A false failure on the
  check that decides whether a push may proceed, and the merge count had the
  sharper form of it, comparing prose to an integer. stderr goes to its own file
  now — still reported, since it is the useful part of an infra message, just
  not parsed — and a non-numeric count is `FAIL_INFRA` rather than an arithmetic
  abort. DeepSource supplied the third: `local dir` in
  `ci::common::node_workspaces` survived the rewrite from a one-level scan to a
  recursive find, naming nothing.
  A fourth arrived from Qodo on the next push, in the stdin reader added two
  rounds earlier: the pre-push **tip was chosen by arrival order**.
  `_push_new="$_lsha"` ran on every record, so the tip was whichever ref git
  happened to list last — while the base beside it was already being widened by
  ancestry. The two halves of one range therefore described different pushes,
  and the answer changed when git reordered its input. The tip is chosen by
  ancestry now, keeping the descendant. Reading the code for that turned up a
  second order dependency in the same loop: a zero remote sha (a new branch)
  did `break`, which stopped reading stdin altogether, so any ref listed after
  it was never seen — it is recorded and the loop continues instead. And where
  two pushed refs have no ancestry in common, one `A..B` range cannot describe
  both and picking either leaves the other unscanned; that is refused with a
  message naming the refs rather than silently gated on half the push. All
  three cases fail against the previous dispatcher.
  A twenty-eighth round found five, two P1, and three of them are a rule that
  asked whether a thing *exists* rather than whether it *works*. The `typecheck`
  guard added in round seventeen tested for the presence of the script key, so
  changing its command to `"typecheck": "true"` satisfied it, exited 0 and never
  invoked a compiler — a workspace containing `const n: number = "not a number"`
  passed the lane with no checker installed, since `vite build` does not
  typecheck either. Editing the command reaches exactly where deleting the key
  reached. It is an allow-list now, like the test-script filter: `tsc`, `tsgo`,
  `vue-tsc`, `svelte-check`, `astro`, `tsd`, `attw`, or a runner that delegates
  to one, and anything else stops the lane until someone says which it is.
  `extract_test_block` returned on the **first** top-level `test:` key while
  JavaScript keeps the later one, so a broad `include` followed by
  `test: { include: ["tests/only.test.ts"] }` reported every file runnable while
  vitest collected one; duplicates are refused rather than resolved, because a
  config with two of them is a mistake whichever wins. And changeset detection
  still consumed non-NUL `--name-status`, so git quoted `frontend/src/café.ts`
  into `"frontend/src/caf\303\251.ts"` — a string ending in a quote character
  rather than in `.ts`. Its language became `unknown`, the emitted checks were
  the always-list alone, and the Node tests, typecheck and build were filtered
  out of a commit that changes TypeScript: the same defect the git-safety scan
  had, on the scheduler instead of the scanner. All four modes read `-z` now,
  through one reader that keeps both sides of a rename.
  The last two were in workspace discovery, and they pull in opposite
  directions. A root `package.json` ended discovery, so a repository with both a
  root and child packages ran only the root and exited 0 while the children's
  failing scripts were never invoked — but emitting the children instead is not
  the fix, because in a workspaces monorepo only the root carries a lockfile and
  this lane refuses to install a workspace that has none, which would turn every
  real monorepo red. Neither reading is safe to guess, so the ambiguity is
  reported and the lane stops. Separately, the root sentinel was dropped with
  `grep -v "^${manifest}$"`, and `package.json` as a regex makes every `.` a
  wildcard — so a workspace directory named `package-json` matched the pattern
  and was skipped. Path surgery is literal parameter expansion now, and the
  index-side scan beside it, which had the same one-level-deep `NF == 2` limit
  discovery had already outgrown, walks to any depth.
  The typecheck fix broke an existing case, and the way it broke is the point:
  `node lane: a typecheck script satisfies the TypeScript requirement` used
  `"typecheck": "true"` as a convenient stub — so the suite had been asserting
  that a typecheck running no compiler is acceptable, which is the defect
  itself, written down as an expectation. The fixture is a real command now,
  and the case asserts what it can honestly claim (neither declared-TypeScript
  rule fires) rather than an exit of 0, since the sandbox has no tsc to run.
  A twenty-ninth round found five, two P1, and three of them are the previous
  round's reasoning applied where it had not been carried. The `test` script had
  the defect the `typecheck` script had just been fixed for: `"test": "true"`
  runs, exits 0 and collects nothing, so a one-word manifest edit removed the
  whole suite from the gate while the lane reported PASS over a workspace that
  still contained tests. Same allow-list, one script over. The vitest **flag**
  rule was still a deny-list, and it was missing `--exclude` and
  `--passWithNoTests`, which together make `vitest run` print "No test files
  found" and exit 0 while being neither a name filter nor a positional — the
  fourth time enumerating the dangerous options lost this race, so the flags are
  now inverted the way the config properties already were: a flag that cannot
  reduce what is collected is named, and anything else stops the guard.
  `--passWithNoTests` would be excluded by name regardless, since it converts
  "collected nothing" into success.
  The other two were defects in the round-twenty-seven fix itself. The pre-push
  **base** was still chosen by "the older, else whichever arrived first", so two
  remote tips that are not each other's ancestors made the range order-dependent
  again — `A0..tip` re-walks everything reachable from the discarded `B0`, and
  the gate can then block a push over a secret, an unsigned commit or a merge
  the remote already has. Refused now, like the tip. And the refusal message
  said "unrelated histories / no ancestry in common" while the test performed is
  containment: two branches forked from a shared base fail it and have a
  perfectly good merge base, so the diagnostic sent people looking for a
  rootless history that is not there. Refusing is still right; the words now
  describe the condition. Qodo also caught a predictable `/tmp/bp-err.$$`
  fallback opened for writing — a symlink target waiting to happen — introduced
  in the same round; there were three of them once the changeset temp files were
  counted, and a gate that cannot obtain a private temp file now stops rather
  than falling back to something worse.
  The fixtures followed the typecheck one, and at a scale worth recording: **51
  cases** used `"test": "true"` as their stub, because the sandbox has no real
  runner and `true` was the shortest thing that exits 0. The suite had therefore
  been writing down the exact defect codex reported, 51 times, as the normal way
  to declare a test script. They are `bash -c true` now — a wrapper, which is
  what they always meant — and the three `exit 1`/`exit 10` variants with them.
  Two controls needed more than a rename: `an ordinary test script is not read
  as a filter` and `a wrapper script with a positional argument is not a filter`
  both now create a real `scripts/test.sh` in the sandbox, so the lane can
  actually run what the manifest names rather than relying on a command that
  ignores its arguments. Cases that assert the *rejection* keep the bare stub,
  since that is the thing under test.
  A thirtieth round found four, one P1, and the sharpest is a hole opened by the
  round-twenty-nine fix rather than closed by it. That fix accepted any token
  from a runner list as evidence of delegation, so `"typecheck": "bash -c true"`
  satisfied the guard — the exact no-op the rule was written to reject, one
  wrapper out, and the same for `"test"`. Delegation counts only when it *names*
  what it delegates to: `bash scripts/check.sh` and `npm run check:all` name a
  target, `bash -c true` names nothing, and an inline `-c` command is judged on
  its own contents (so `bash -c tsc` is still accepted, because that is a
  checker being invoked). Both scripts share one predicate now.
  That in turn condemned the fixtures a second time. `bash -c true`, adopted one
  round earlier to replace `true`, is itself a wrapped no-op — so `ws_setup`
  creates real `scripts/test.sh`, `scripts/fail.sh` and `scripts/fail10.sh`, and
  the 74 fixtures name one of those. A workspace wrapping its suite has a script
  on disk; the fixtures say that now rather than standing in for it twice over
  with a command that ignores its arguments.
  The P1 was a security gap: the sensitive-filename list was written in
  repository-root spellings, and a case pattern like `.npmrc` matches that
  string and nothing else — so `frontend/.npmrc` and `packages/app/.pypirc`
  passed. The content scan is no backstop, since
  `//registry.npmjs.org/:_authToken=` matches none of the canonical token
  prefixes; `.env` survived only because `*.env` happens to match a nested path,
  and the entries without a leading-wildcard twin did not. Names are matched on
  the basename at any depth now, with `.netrc`, `.pgpass` and `credentials`
  added, and a control asserting that an ordinary `frontend/src/lib/env.ts` is
  still allowed. Nested workspaces are the layout this PR spent rounds teaching
  the rest of the gate to expect, and this list had not been told.
  `["test"]: { ... }` was the third: a computed key JavaScript applies exactly
  like `test:`, and later if it comes later, but the quoted token is consumed by
  the scanner's string handling — so a broad plain block followed by a narrow
  computed one was counted once and reported as fully covered. A computed
  property at the exported object's own level now stops the guard, with a
  control asserting that ordinary array *values* are not mistaken for one.
  The fourth was the predictable temp path in `all` mode, already removed with
  the other two the round before it was reported.
  A thirty-first round found five, two P1, and three are the same mistake in
  different files: a rule fixed in one direction and left wrong in the other.
  (1) The round-thirty fix taught the index-side workspace scan to walk to any
  depth, so it would agree with filesystem discovery about *depth* — and left it
  disagreeing about *vendoring*. Discovery drops `ci/tests/fixtures/node`
  through `ci::common::is_vendored_path`; the index scan added it straight back,
  and alphabetically first it was the workspace the lane entered before any real
  one. With no lockfile beside it, `CI_GATE_MODE=full bash ci/checks/node.sh`
  exited **30** on a fixture and never reached `frontend` — every scheduled
  full-mode Node lane was failing. Indexed candidates go through the same
  predicate now: the predicate itself, not a second copy of its rules, since a
  second copy is how the two drifted apart to begin with.
  (2) The pre-push hook read the destination ref off stdin and exported only the
  SHAs, so `branch-protection.sh` kept asking `git rev-parse --abbrev-ref HEAD`
  and approved `git push origin feature:main` as an ordinary feature-branch
  push. Destinations are exported and enforced now, collected *above* the
  zero-sha skip so `git push origin :main` — deleting a protected branch,
  carrying no content — is not waved through for being empty. Set-and-empty is
  kept distinct from unset: empty means the push names no branch (a tag), and
  falling back to the checkout there would refuse `git push origin v1.2` for the
  branch you happened to be standing on.
  (3) `test.include` could subtract. `['tests/**/*.test.{ts,tsx}',
  '!tests/lib/**']` is a literal array containing the declared glob, so every
  check passed while vitest collected no `tests/lib/` file — the silent drop the
  `exclude` rule exists to prevent, written one property over and out of its
  reach. Any `!` in an include entry stops the guard, not a leading one only,
  since picomatch's `!(...)` subtracts from the middle of a pattern just as well.
  The fourth was a performance failure severe enough to be a correctness one.
  `git-safety.sh` sized blobs per *emitted* path, and `_gs_content_files` emits
  one record per commit per modification — so a file touched by forty pushed
  commits was sized forty times, each sizing walking every commit again.
  Invisible on an ordinary push, quadratic on one with no resolvable base, where
  the commit list is the whole history. Measured on a clone of this tree with
  its remote removed: 419 commits, **19s to size one path**, 15 paths costing
  **371s**, extrapolating to **6,711s** for the 271 unique paths — against a 120s
  pre-push budget. Paths are deduplicated, and the per-pair `git cat-file -s` is
  replaced by one `git cat-file --batch-check`: **113,549 pairs in 3.2s**. The
  question asked is deliberately unchanged — the max of `<commit>:<path>` over
  the enumerated commits — and that was checked rather than assumed: old and new
  return byte-identical sizes. `diff-tree`'s post-image blobs would have been
  cheaper still and *not* equivalent, since a path that was 40MB before the
  range and is modified down inside it is still 40MB in the pushed trees.
  The symptom was checked end to end, not just the term that caused it: on a
  synthetic 419-commit first push the fixed check completes in **82s**, and the
  previous one was still running when a 300s cap killed it. The five remaining
  per-commit `git show` loops are linear and untouched — 18.7s each here, ~96s
  in total — so the budget is met with room, and reducing it further would mean
  restructuring the read helpers earlier rounds hardened for fail-closed
  semantics.
  The fifth came from the pinned comment: `merge-base --is-ancestor` exits 128
  for an object the repository does not have and 1 for "genuinely not
  contained", and the hook read both as the second — so a remote base that had
  merely never been fetched hard-refused the push. A missing remote object is
  now the same statement as a new branch: no base, so nothing narrows the range
  and ship mode walks all of HEAD. Scanning more, never less.
  A thirty-second round found two, both P1, and both are the gate reporting
  confidently on something it had not actually looked at.
  (1) Ship mode resolves its *ranges* from the hook's SHAs, so the history
  checks read the right commits — while every check that runs content reads the
  worktree, of which there is one. `git push origin other-branch` therefore had
  the two halves of a single run describing different branches, and the half
  that executes things was describing the wrong one: a passing checkout vouched
  for an outgoing branch whose tests fail. Reproduced before fixing — the lane
  exited 0 on a branch whose test script exits 1. Refused rather than worked
  around: running the outgoing tip means checking it out or building a second
  worktree inside a pre-push hook, which is a far larger change than this is a
  bug and fails badly on a dirty tree, and the developer's remedy is one
  command. The comparison lives in `ci::git::push_tip_is_checkout` and is called
  from `preflight.sh` and `node.sh` — the node lane is not the only check that
  reads the worktree, so fixing it only where it was reported would have left
  the identical hole in test-layout, the suites and the build.
  (2) Delegation was accepted on the strength of a target *token*: any
  recognised wrapper followed by any non-flag token passed, without ever reading
  the target. `"test": "bash scripts/test.sh"` therefore passed while that
  script was `exit 0`. The suite proved it — `ws_setup` wrote exactly that
  script and **90 fixtures** named it, so the fixtures had been asserting that a
  workspace running no tests is in good standing. That is the third fix to this
  rule and the fourth time the fixtures encoded the defect under test: `"test":
  "true"`, then `bash -c true`, and now a file whose contents run nothing —
  each the previous no-op wearing one more layer. The stated reason for
  allowing delegation ("a script the gate cannot read either way") was false: a
  package script is in the manifest and a shell script is a file in the
  workspace. Both are read now and judged by the same predicate, line by line
  with comments stripped, depth-capped at 8. What genuinely cannot be resolved —
  `make test`, or a target that is neither a manifest script nor a readable
  file — is refused, which is a real behaviour change and the honest one.
  Fixtures are wrappers that reach a runner now, via a `scripts/vitest` stand-in
  that marks the true boundary: the gate can see a script invoke something named
  `vitest` and cannot see what that binary does.
  A thirty-third round found six, three P1, and the sharpest lesson is that one
  rule had now been fixed five times while asking the wrong question every time.
  `_script_names_a_checker` asked whether a command *contains* a checker; codex
  supplied a third way for a string to contain one that never runs -- `"test":
  "echo vitest"`, where the name is an argument -- after `bash -c true` and a
  `scripts/test.sh` that does nothing. Presence is not execution, and widening
  the token scan was never going to reach that. The predicate is rebuilt around
  **command position**: a token counts only where a command starts, at the
  beginning or after a separator. `echo bash scripts/test.sh` closes with it, a
  hole one layer out that was not in the report. Compositions that cannot prove
  the checker runs are refused: `true || vitest run` never reaches it, and
  `vitest run || true` is worse -- the suite runs in full and its result is
  discarded. `&&` stays accepted, because either the checker runs or the script
  fails with what preceded it. And the composition case had been *rejected for
  the wrong reason* before this, by the positional-filter rule, with "'true'
  selects a subset" -- a diagnosis describing a filter that is not there; it has
  its own check and its own message now, with a test asserting the old wording
  is absent.
  Two more were the guard failing correct work, which is the expensive kind
  because a gate that blocks good commits gets switched off. Qodo caught the
  round-30 computed-key rule marking *any* `[` at the exported object own level
  as a computed key -- and `[` does not change brace depth, so an ordinary
  `plugins: [react()]` sat at that level too. Adding a bare `plugins: []` to
  this repository's vitest config made the check exit 20. It is a computed key
  only where a *key* would be, after the brace or a comma. The nested-config
  case was the same shape: `frontend/e2e/tsconfig.json` extending
  `../tsconfig.json` was reported as an orphan because no `package.json` sat
  beside it, and a full-mode run exited 20 before reaching the workspace. The
  walk goes upward now and stops at the first manifest; a config that reaches
  the root without finding one is still reported.
  The rest: `--config`/`-c`/`--root` are out of the runner flag allow-list --
  they do not narrow what vitest collects, they change which file *declares*
  what it collects, while `test-layout.sh` validates one fixed path, so the two
  checks were reporting on two different files. `defineConfig(Object.assign({
  test: {broad} }, { test: {narrow} }))` validated the object composition had
  already replaced, so what sits between `export default` and the brace is an
  allow-list now -- nothing, or `defineConfig(` -- and a spread at that same
  level went in beside it rather than waiting to be reported. And a
  deletion-only push (`git push --delete`) set no tip, which
  `ci::git::push_range` resolved to `HEAD`, so the content and history checks
  audited the checked-out branch; `CI_GATE_PUSH_DELETIONS_ONLY` states that
  case explicitly and those checks skip, while destination protection still
  runs -- deleting `main` is the push that must still be refused.
  The rewrite introduced two defects of its own before the suite caught them,
  both worth recording because both are the direction that blocks correct work.
  The token unquoting is a bracket expression matching a leading or trailing
  quote; rewriting it dropped one backslash before the single-quote, and that
  form matches any first character, so it strips one from every token. `tsc`
  became `sc` and every TypeScript workspace was told it has no type checker.
  Five acceptance cases failed at once; the quotes are matched one at a time
  now, and pinned by a case of their own. The new depth-1 spread rule flagged
  `{ ...shared, test: {...} }`, which is correct: the explicit `test:` comes
  later and wins, so only a spread *after* the test key can override it.
  `make` also left the runner list -- its argument is a Makefile target, not a
  package script or a file, so `make test` had been resolving a `test` script
  from the manifest and accepting whatever that ran.
  A thirty-fourth round found one, and it was a regression from the round
  before it. The ship-mode guard asked "is the pushed tip the checkout" and
  refused otherwise -- correct for `git push origin other-branch`, wrong for
  `git tag v1.0 <older commit>; git push origin v1.0`, where the tip is the
  tagged commit and the whole ship gate failed on an ordinary release workflow.
  Qodo caught it. The question being asked is whether the worktree can stand in
  for what is going out, and "is it the checkout" is the common answer rather
  than the whole of it: a tag on an ancestor of HEAD is content the worktree
  already contains. Renamed `ci::git::worktree_covers_push`, since the old name
  described one way of answering rather than the question. The relaxation is
  narrow -- only when the push names no branch destination at all, so `git push
  origin other-branch` walks straight back into the refusal, and a tag pointing
  somewhere HEAD does not contain is still refused.
  A thirty-fifth round found five, three P1, and one of them broke the lane
  outright. `[ -e "$_d" ] && printf ...` left the test's false status as the
  status of the deletion loop whenever the last deleted path was genuinely
  gone -- the ordinary case for a clean deletion commit -- and that propagated
  through the command substitution, the assignment and `set -e`. Reproduced:
  the pre-fix lane exits **raw 1 with 39 bytes of output**, the section header
  and nothing else, before install, typecheck, test or build. An `if` makes the
  loop end successfully.
  Two more were the checker rule again, now five and six. A pipeline reports its
  *last* command's status, so `tsc --noEmit | cat` prints errors and exits 0
  while the command-position rule returned success without looking past the
  checker; and `unused() { node --test; }` followed by `exit 0` named a runner,
  in command position, inside a function nobody calls. Pipelines join `||` and
  `&` as compositions whose result cannot be attributed to the checker, and a
  delegated script now counts a line only at its top level, outside every brace
  group and block. Qodo added the third: `a||b` is the same operator as `a || b`,
  and matching only the spaced spelling was a bypass two keystrokes wide.
  The fifth was a fail-open on the layout guard. Its candidate list was
  line-oriented, so a path containing a newline arrived as two and the tail could
  be spelled to sit under `frontend/tests/`. Reproduced: **exit 0, "Test layout
  OK: 1 file(s)"** on a stray test vitest will never collect. find, ls-files and
  ls-tree are NUL-delimited now and the candidates are held in arrays, because a
  command substitution cannot carry NUL bytes.
  Three defects of my own turned up while fixing these, all caught before they
  landed: `exec vitest run` was rejected by the new control-flow rule (that is
  how a wrapper normally hands over); a multi-line function body was accepted
  because the definition line was skipped before its braces were counted; and
  the typecheck path rejected the pipeline as "does not appear to run a type
  checker", which is false -- it names tsc and runs it, then discards the
  answer. The composition check is one shared function now, so both paths give
  the same reason.
  Worth recording about the refutations rather than the fixes: the deletion case
  passed twice against the broken code before it reproduced. First because
  setting `CI_GATE_NODE_WORKSPACE` skips the discovery pass the drift scan lives
  in; then because `ws_setup` copies the current `ci/lib/git.sh` beside an older
  `node.sh`, whose renamed helper no longer exists, so the run died on a missing
  function instead. A refutation that passes is not the same as code that works.
  A thirty-sixth round found seven, three P1, and one was two reviewers
  disagreeing about the same rule. codex and Qodo pulled the tag exception in
  opposite directions and both were right about their own failure: refusing
  every tag push failed the ship gate on an ordinary release workflow, while
  allowing any tag on an *ancestor* of HEAD let a failing commit be tagged,
  repaired in a descendant, and pushed while the lanes validated the repaired
  tree. Ancestry says the worktree contains that history; it says nothing about
  the tagged tree having been checked. **Publication** settles it — a commit a
  remote branch already contains was gated when its branch went out, so the tag
  adds a label and no content; one no remote branch contains is carried out by
  the tag itself. Stale remote-tracking refs make that stricter, not looser.
  The lost-suite guard compared against HEAD, and in ship mode the deletion is
  already committed — so HEAD carried no tests either, nothing looked missing,
  and a push removing every test file and the test script exited **0** with
  "Node lane passed". It compares against the push base now. `"test": "exit 0 ;
  vitest run"` was the checker rule again: a separator resets where a command
  *starts*, which is not whether one runs. Fixing that broke a control —
  `bash scripts/test.sh ; exit 0` was rejected because the separator cleared the
  pending delegation before anything resolved it — so resolution moved into its
  own function that runs wherever a command ends.
  Three more: a shorthand, method or getter `test` overrides the colon-form
  block and only the colon form was recognised (41 files reported runnable while
  vitest listed four); an index entry whose blob git cannot produce was dropped
  silently, leaving the worktree copy validated alone, now FAIL_INFRA; and
  `timeout_sec` was filtered rather than validated, so `1e3` became 13 and `-1`
  became 1 and the runner killed a blocking check seconds in. Qodo's portability
  finding went with them — `sort -z -u` is a GNU extension against a stated Bash
  3.2 floor, so the dedupe is done in the shell, quadratic over 219 candidates
  and cheaper than the subprocess it replaces.
  Two findings were put to the owner rather than changed inside a review round,
  and both were decided on 2026-08-17 as **keep as it is**, with the reasoning
  written at the code rather than only on the thread. `is_vendored_path` treats
  any `build`, `dist`, `coverage` or `htmlcov` segment as output, so a
  first-party `packages/build/` is skipped — but `ci/checks/git-safety.sh`
  refuses to push those same paths with no override, so the skip is not a route
  to a branch, and narrowing would have had to widen the push gate in the same
  change. The JS test lane invokes the pinned runner rather than the declared
  `test` script; the silent half of that is fixed (the script's own environment
  now reaches the runner), and the residual is a setup *command* not running,
  which surfaces as the runner failing rather than as fewer tests passing
  quietly. Both comments name the condition that would reopen them.
  A thirty-eighth round found five, and the sharpest is a list that had drifted
  five times. The generated-directory names lived in three copies inside
  `ci/checks/test-layout.sh` — the legacy-directory find prune, the candidate
  find prune, and `path_is_pruned` — and the round that added `.cache` reached
  one of them. `path_is_pruned` is the copy that answers for the **index** side
  of both scans, because `find` applies its own prune and `git ls-files` does
  not, so it was simultaneously the most load-bearing and the one nobody looked
  at: a generator writing `frontend/.cache/__tests__/` was reported as a retired
  directory to delete, and driving the new case against the parent showed
  `.cache`, `.nuxt` and `htmlcov` mis-flagged by the *candidate* scan as well —
  wider than the finding said. Fixing the fifth instance of that is not a fix,
  so the names have one definition and the three readers are built from it.
  `.cache` was missing from `ci::common::is_vendored_path` too, from the
  direction "check the siblings" does not cover: it was added to a prune list
  and not to the predicate documented as the single definition, so a bundler
  cache made discovery report a nested workspace and exit 1.
  Three more: `tr '/' '-'` is not injective, so the independent workspaces
  `a/b-c` and `a-b/c` both wrote `js-a-b-c.xml` and the second silently
  overwrote the first's JUnit results; the non-ship half of the indexed-manifest
  scan still read `git ls-files` line-oriented, so a committed `café/package.json`
  arrived quoted and the lane refused a workspace that is in the index; and the
  active plans still directed operators to `frontend/src/**/__tests__/`, which
  this PR's guard now rejects — 38 references rewritten, each checked against
  the working tree first, which caught five pointing at a `.ts` file that was
  written as `.tsx`.
  Combined suite: `bats ci/tests/` = 565 cases, 0 failures.
  The node lane was run end
  to end against `frontend/`: install, `tsc --noEmit`, 477 tests, `vite build`.
  Counts here are the ones the files actually contain at this commit; an
  earlier revision of this paragraph quoted stale ones.
  Also fixed: the Python-convention `lib/` rule in `.gitignore` was swallowing
  **new** files under `frontend/tests/lib/` (17 of the moved tests live there;
  the moved ones survived only because `git mv` tracks explicitly), so a
  newly-authored test there would have been invisible to git — never committed,
  never run, nothing on screen to say so. Negations added to match the existing
  `frontend/src/lib/` ones, plus `.ci-gate/` (generated dep-cache fingerprints,
  now written root-anchored per workspace instead of scattered).
- ✅ Dependency supply-chain hardening (PR #155, merged 2026-08-03) — `main`'s
  `uv.lock` had drifted from `pyproject.toml` (still resolving `fastapi==0.136.3`
  and `pytest==9.0.3` against declared `0.137.1`/`9.1.0`), and the Dockerfile ran
  `uv sync --frozen`, which reads the lockfile **without comparing it to the
  manifest** — so the image built green while silently installing stale
  packages (`uv lock --check` and `uv sync --locked` both exit 1 on the same
  tree; `--frozen` exits 0). Fixed with the surrounding batch: PR #156
  consolidated the Dependabot bumps that the drift had been blocking (closing
  #140/#153/#154 unmerged).
- ✅ Docker + docker-compose stack (PR #15).
- ✅ Architecture docs: multi-tenant (`Docs/17`), source-reported currency
  policy (`Docs/18`, revised 2026-05-23) — PR #16 plus B1 planning update.
- 🗑️ Neo4j graph component retired entirely (PR #12).
- ✅ Local validation gate: `scripts/run_validation_gate.py` invokes
  `backend/ums_smart_revenue/devtools/quality_gate.py` which runs ruff
  (backend + tests + scripts), the AST-based no-skip/xfail policy gate,
  pytest full suite (strict-config + strict-markers + isolated
  `.pytest-tmp`), and `git diff --check` (working tree + staged) — PR #38.
- ✅ Developer agent rules: `AGENTS.md` (Codex), `.agents/skills/`
  (vitest + postgresql-table-design), `skills-lock.json` — PR #38.
  The Claude-Code-local `CLAUDE.md` is gitignored as a per-machine copy.
- ✅ Per-PR documentation system at `Docs/pulls/` (one
  `report.md` + `changelog.md` + `handoff.md` per PR) — coexists with
  inline `✅ / ⏳ / 🗑️ PR #N` marks on this doc and
  `01_IMPLEMENTATION_PLAN.md`.
- ✅ Mockup catch-up: OFL-licensed soft-dark variant
  (`mockups/ums-smart-revenue-command-center-soft-dark.html` + 9 QA
  screenshots + `generate-screenshots-soft-dark.py` + `mockups/FontsGH/`
  with Mona Sans / Monaspace Neon / Newsreader) committed as a
  redistributable sibling to the canonical
  `mockups/ums-smart-revenue-command-center.html`, referenced from
  `Docs/09_SMART_DASHBOARD_UI.md` — PR #39.
- ✅ Frontend tenant-header foundation: `TenantContext`, `useApiClient`, `GET /tenants/me`, Vite dev gateway proxy, Vitest framework + validation-gate integration — PR #41.
- ✅ June-14 MVP dashboard wiring: six screens wired to live APIs
  (Command Center + smart-alerts problem panel, Close, Trace/Explain,
  Exports, Connectors; Registry/Audit still mock at that point), demo-month seed
  (`scripts/seed_demo_month.py`), end-to-end smoke (`scripts/smoke_mvp.py`),
  demo runbook (`frontend/README.md`) — PR #69.
- ✅ Production session role hydration — merged to main (PR #70, 4d7f154):
  `GET /session/me` (`backend/ums_smart_revenue/api/session.py`) returns the
  authenticated principal's identity, optional tenant, active roles/permissions,
  and **global-scope** camelCase capability booleans derived (fail-closed) from
  the backend permission policy. The SPA bootstraps it (`useSessionBootstrap` +
  `SessionContext`) and the AppShell renders the dashboard gated by those
  capabilities — a production build no longer shows the permanent access-denied
  screen; the dev preview role is now presentation-only. Failed hydration
  (401/403/network) or a `disabled` principal fails closed to `AccessDenied`;
  connector controls require `canRunConnectorJobs`; the Vite dev proxy now
  forwards `/session`. Smoke (`scripts/smoke_mvp.py`) asserts the contract.
  Live ingestion needs real connector credentials.
- ✅ Production Audit view wiring — merged to main (PR #71, 31a7641): the
  dashboard Audit page now reads the real `GET /audit/events` feed (was mock
  `AUDIT_EVENTS`). Distinct cursor-pagination types
  (`AuditLogEntry`/`AuditEventCursor`/`AuditEventPagination`), a memoized
  `useAuditEvents` hook (one self-auditing fetch per mount, no loop), and an
  extracted `views/AuditView.tsx`. (Registry was later wired to `GET /channels`
  in PR #73/#78.)
- ✅ Audit summary endpoint — `GET /audit/summary` (tenant-scoped,
  VIEW_AUDIT_LOG fail-closed, snapshot-then-excluded-self-audit): returns
  total/sensitive/recent event counts excluding `AUDIT_LOG_VIEWED`, then records
  one excluded `AUDIT_LOG_VIEWED` read row after the snapshot, with a
  `window_hours` param (default 24) for the recent count. The Audit summary
  tiles are wired off it
  (live Total events / High sensitivity / Events-24h; Retention stays a static
  policy constant), replacing the `AUDIT_SUMMARY` mock and the "live aggregate
  endpoint coming" disclaimer — PR #91, branch `feat/audit-summary-endpoint`.
- ✅ Connector credential test-connection probe — `POST /connectors/credentials/{connector_key}/{account_id}/test`
  (branch `docs/plan-hygiene-post-71`): wraps `resolve_connector_credentials()` (load
  credential row → resolve secret URI → official Google token refresh, no live data pull).
  Surfaces `CredentialNotFoundError` as 404; `InactiveCredentialError` / `OAuthRefreshError` /
  other `GoogleConnectorError` as 200 with machine-readable `status` field (`inactive_credential` /
  `auth_failed` / `error`) and a string `detail`. Every probe is audited (`CONNECTOR_TESTED`
  event, `MANAGE_CONNECTORS@connector(connector_key)` gate, reason required).
  5 TDD tests (ok, not-found, inactive, oauth-error, 403). Backend only; no migration.
  Merged to main as PR #72 (28da1a6).
- ✅ Connector run history + Test Connection — merged to main as PR #81 (Track D
  buildable chunk) — new read-only `GET /connectors/runs`
  (`VIEW_CONNECTOR_HEALTH` gate, tenant-scoped, optional connector_key/account_id
  filters, newest-first `(started_at, id)` cursor pagination, no audit write)
  backed by a new `list_runs`/`ConnectorRunPage` repository read method; the
  ConnectorsView "Run history not yet available" placeholder is replaced with a
  live run-history panel (status badges, counts breakdown, error_summary, Load
  More with id-dedupe) and the existing test-connection probe is surfaced as a
  per-credential Test Connection button (fixed audit reason). `/session/me` now
  exposes `canViewConnectorHealth` so the SPA gate mirrors the route gate exactly.
  No migration; no live credentials needed. Remaining Track D is creds/schema
  blocked: official Google authorization setup, live pulls,
  token-expiry/last-error schema + background monitoring (the refresh-telemetry
  columns landed in PR #95 Part 2).
- ✅ Connector credential token-health surface — PR #105, branch
  `feat/connector-credential-health`: new read-only `GET
  /connectors/credentials/health` (`VIEW_CONNECTOR_HEALTH` gate, fail-closed;
  distinct from the `MANAGE_CONNECTORS`-gated `GET /connectors/credentials`)
  returns `{credentials: [{...telemetry, health_state}]}` — each entry repeats
  the credential metadata + four refresh-telemetry fields and appends a derived
  `health_state` (`healthy`/`expiring`/`auth_failed`/`missing`/`unknown`) from a
  pure, unit-testable `derive_credential_health_state(entry, *, as_of)` helper
  over already-persisted columns (no live token refresh). Connector-scoped
  viewers narrowed to their granted connector ids (no foreign-credential leak);
  offset-paginated (`limit` ≤ `100`). Token-health frontend wired into
  ConnectorsView. Read-only: no audit write, no migration.
- ✅ Import stepper UI — PR-B of the import/sync UI arc (2026-08-09, branch
  `feat/import-stepper-ui`) — closes the arc PR-A opened: the Registry
  header's disabled Bulk Import placeholder becomes a live **Import CSV**
  action that swaps the main panel for a three-step **Upload → Preview →
  Applied** stepper over `POST /channels/import`, reusing PR-A's
  `ActionStepper`/`OutcomeTable` primitives and the credential-fed
  content-owner picker (empty state points at Connectors). Upload collects
  the roster CSV + content owner + required audited reason and always fires
  the read-only dry-run first; Preview renders per-row outcome chips
  (CREATE/UPDATE/UNCHANGED/ERROR), the field-level `changes` diff, group
  effect, the revenue flag (spec-mandated — on CREATE rows the diff is
  empty by design, so the column is the only preview surface for the
  default-true `revenue_required` before the all-or-nothing apply), and
  non-zero counts, with **ERROR rows blocking Apply** (the API
  422s the whole file — all-or-nothing; remedy named inline) and the 422
  apply race (concurrent editor between preview and apply) replacing the
  stale plan with the refreshed payload the backend ships as `detail`;
  Applied echoes counts + reason, and leaving the flow reloads the table
  (Cancel restores it untouched — no refetch unless an apply committed).
  Backend touch (1, additive): a `can_import_channels` session capability
  derived from MANAGE_CHANNELS **and** MANAGE_GROUPS (both-permission
  render hint — a group-bearing roster needs both, so a channels-only
  principal never sees a control that 403s mid-flow) gating the header
  action — hidden, not disabled, without it. No new endpoint; no migration.
  `can_import_channels` is the **Python field name**; `SessionCapabilities`
  sets `alias_generator=to_camel`, so the key a client actually reads from
  `/session/me` is **`canImportChannels`**. Both names are given because this
  is the repo's frontend/backend casing seam, and a consumer that checks the
  snake_case key finds `undefined` and silently hides Import CSV for everyone
  (review #184, qodo).
  Review round (2026-08-09, PR #184) added four corrections, all about the
  preview telling the truth: (1) **both exits fail closed mid-request** —
  Cancel and Preview's Back were live while an apply POST was in flight, and
  since the hook has no abort and the backend commits independently, either
  exit unmounted the flow while the write went on to land, showing a
  cancelled import that actually committed (`ActionStepper` gained an
  optional `cancelDisabledReason`); (2) the Channel cell shows the durable
  `youtube_channel_id` **alongside** the mutable, non-unique `channel_name`
  instead of falling back to it; (3) the Applied step labels its counts
  **"Approved plan"** and names `CHANNEL_IMPORTED` as the authority, because
  the route answers an apply with its PRE-write plan payload while the apply
  tallies what it actually wrote under the write-boundary lock; (4) a second
  additive backend touch — `group_action` (`CREATE`/`JOIN`) on each import
  row, from a fourth bulk group lookup (`list_owned_cms_group_ids`), so the
  Group cell can say whether a `Group_ID` **mints a new SECTOR group** (a
  finance-scope object) or attaches to one this owner already holds. Still
  no new endpoint and no migration.
  **Backend scope correction (2026-08-11).** The two "additive touch" notes
  above were written early and undercount what the PR ships: the final diff
  is **2029 insertions across eight backend files as of `742dcc66`**, the
  commit that last changed `backend/`, and
  calling it additive reads as frontend-with-a-flag. Beyond
  `can_import_channels` and `group_action`, the route gained
  `expected_plan_fingerprint` — a **plan-bound apply** that 409s with the
  refreshed plan when the reviewed pre-state has drifted — a
  `plan_fingerprint` widened to cover the target *and* the server-resolved
  tenant, `revenue_source_status` disclosure on every row, and a
  write-boundary recheck of the previewed group effect under the group row
  lock that applies to **unbound callers too**. A later round added a third
  refusal: the parser now rejects a roster that **restates the same
  `(youtube_channel_id, group_id)` pair**, for a reason that differs by shape:
  a pair naming a group is collapsed into one membership by the write pass
  while planning promises it twice, and a pair carrying **no** group never
  reaches that pass at all — it is refused because the second copy is a
  phantom `UNCHANGED` row reporting two outcomes for a channel named once. It
  is the only change on this branch that refuses input the previous code
  accepted, and it is therefore **BREAKING**: such a roster returned 200
  before and returns 422 now, so an existing file carrying a restated line
  must be deduped before it applies again. What is preserved is the REGISTRY
  result — the restated row is not a no-op (`UNCHANGED` rows write through the
  boundary) but only re-wrote what its first copy installed, so the same
  channel rows and memberships land. The one persisted change is the durable
  `CHANNEL_IMPORTED` `counts`, one lower without the restated row, which is
  the double count the rule exists to remove. All three new behaviours are
  refusals, so the no-endpoint and
  no-migration claims still hold, and the unbound "the file wins" rule of
  #159 is untouched. Scope, rollback ordering and the per-file evidence live
  in `Docs/pulls/2026-08-09-pr-184-import-stepper-ui-handoff.md`.
  The insertion figure names the commit it measured because it moves whenever
  backend code does — including comment-only rounds; re-derive it with
  `git diff --stat $(git merge-base origin/main HEAD)..HEAD -- backend/`
  rather than trusting this line if the branch has advanced.
- ✅ Groups view UI — PR-A of the import/sync UI arc (2026-08-07, branch
  `feat/groups-view-ui`) — the grouping loop gets its first operator surface:
  a new **CMS Groups** nav view (table-first: name · CMS id · owner stamp ·
  members · status) with per-row **Clear stamp** (the #170 recovery route,
  reason-required confirm) and **Archive/Restore** (the lockdown-permitted
  active-only PATCH); a **content-owner picker fed by stored
  youtube-analytics credentials** (no free-text owner ids) driving the
  **sync stepper** (Reason → dry-run Preview via OutcomeTable with outcome
  chips, CONFLICT rows warn-toned and blocking Apply with the remedy named →
  Applied counts + refetch). Shared `ActionStepper`/`OutcomeTable`
  primitives built for reuse by PR-B's import stepper. Backend touches (2,
  additive): `content_owner_id` on every group response
  (`ChannelGroupEntry.to_api`) and a `can_manage_groups` session capability
  (MANAGE_GROUPS-derived) gating the manage controls — hidden, not
  disabled, without it. Dev proxy gains `/groups`. Rode along: re-redaction
  of the CMS owner id #169 had reintroduced + the hash-based tracked-file
  hygiene guard (`tests/test_repo_hygiene.py`, also standalone PR #173).
  No migration.
- ✅ Scheduled CMS group sync (2026-08-06, merged PR #171 as `cc8892d`, branch
  `feat/scheduled-group-sync`)
  — grouping now converges automatically instead of only on an operator's
  `POST /channels/groups/sync` curl. **Executor job kind:** a reserved
  `cms_group_sync` connector-key sentinel (month `-`) on the connector-job
  executor, keyed so it can never collide with a report pull, drives the SAME
  `run_group_sync` core the manual route uses — same plan/apply, same
  conflict refusals. **Scheduler:** a new in-process `GroupSyncScheduler`
  ticks a daemon thread every `UMS_GROUP_SYNC_INTERVAL_HOURS` (default 24h,
  first tick one full interval after boot) and, per ACTIVE tenant, submits
  one job for every active `youtube-analytics` credential — the credential
  list itself IS the target registry (registering or revoking one opts a
  content owner in or out), no new table. **Fail-closed OFF by default**
  (`UMS_GROUP_SYNC_SCHEDULE_ENABLED`); boot FAILS FAST if the schedule is
  enabled without the connector-job executor also enabled or without
  `UMS_GOOGLE_CONNECTOR_SERVICE_ACTOR_ID` set. **Audit taxonomy:** per-group
  `GROUP_UPDATED` rows come from `apply_group_sync` exactly as the manual
  route; the run-level `GROUPS_SYNCED` summary is written by the worker ONLY
  when a tick's execution actually changed something (the manual route still
  writes it unconditionally after every apply) — a converged tick therefore
  writes ZERO audit rows. Failures (credential missing/inactive/refresh, CMS
  fetch, conflict/lost race) fold into one `CONNECTOR_JOB_RUN` row,
  `action=group_sync_job_failed`, `error_class` the exception class name only
  (never `str(exc)`). No new HTTP endpoint — the scheduler is the only
  submitter; the manual route is unchanged. Spec:
  `Docs/superpowers/specs/2026-08-06-scheduled-group-sync-design.md`.
- ✅ Owner-stamp recovery (PR #170, 2026-08-06, branch
  `feat/owner-stamp-recovery`) — completes the group-ownership lifecycle merged
  in #169. **Path A:** the
  import now REFUSES a row targeting an existing owner-NULL group (row-level
  ERROR naming the remedy — run that owner's `POST /channels/groups/sync`;
  blocks the whole apply per the all-or-nothing contract), so sync is the
  ONLY stamp-writer on existing groups; the Path B disclosure field
  `will_adopt_content_owner` is gone from the import response (it remains on
  the sync response, where adoption is legitimate); the write boundary fails
  closed on a mid-flight stamp-clear (`ChannelImportAdoptableGroupError` →
  409). **Clear-stamp admin action:** `DELETE /groups/{id}/content-owner`
  (global `MANAGE_GROUPS`, reason-required, atomic audit sink) erases a wrong
  stamp — 409 when there is nothing to clear, works on archived groups,
  serializes against a concurrent sync-adopt via the store's row lock (proven
  on Postgres with `pg_blocking_pids()`), and the cleared group is re-adoptable
  by the correct owner's next sync (round-trip proven end-to-end). Store gains
  `clear_content_owner` (returning `ClearedContentOwner`: the cleared group
  plus the owner id read UNDER the row lock, so the audit detail can never be
  a stale pre-read observation — proven on Postgres by staging an adopt in the
  pre-read window) + `ChannelGroupNoOwnerStampError`. The route's `reason`
  rejects a NUL character with 422, matching the import and sync routes:
  `audit_logs.reason` is a Postgres text column, so reaching the insert raised
  `psycopg.DataError` as an unhandled 500. The clear's store read, locked
  write, and audit row live in `org/channel_group_owner_recovery.py` (the
  `channel_import_apply` / `channel_group_sync_apply` shape), leaving the route
  as orchestration and making the behaviour testable without FastAPI. No
  migration. Spec:
  `Docs/superpowers/specs/2026-08-06-owner-stamp-recovery-design.md`.
- ✅ Bulk channel inventory import (2026-08-05, PR #159, branch
  `feat/bulk-channel-inventory-import`) — `POST /channels/import` loads a CMS
  channel roster from a CSV in one call, closing the blocker that had kept the
  proven connector from ever running at scale: the only creation route was
  `POST /channels`, one channel per call, and the 2026-06-22 live smoke had to
  hand-seed its 25 channels with raw SQL. **The hazard it is designed around:**
  `youtube_channels.cms_status` defaults to `UNKNOWN` and `list_target_channels`
  selects only `INSIDE_CMS` rows with a matching `content_owner_id`, so a
  channel imported without both is silently skipped by ingest — no error, no
  alert, and its revenue never reaches the fact table. Three rules follow:
  unknown CSV headers are rejected rather than ignored, `dry_run` is a required
  form field (no default) returning a field-level diff before anything is
  written, and applying is all-or-nothing (any ERROR row → nothing written)
  while error *reporting* is batch, so one bad row surfaces every bad row.
  `multipart/form-data`: `file`, `content_owner_id`, `dry_run`, `reason` all
  required; `cms_status` defaults to `INSIDE_CMS` and is validated against the
  table CHECK (`IMPORTABLE_CMS_STATUSES` = INSIDE_CMS / OUTSIDE_CMS / UNKNOWN).
  Required CSV columns `youtube_channel_id` (`^UC[A-Za-z0-9_-]{22}$`) and
  `channel_name`; optional `group_id` and `view_revenue` (→ `revenue_required`);
  UTF-8 and BOM-tolerant, headers case-insensitive and order-independent. Every
  row resolves to CREATE / UPDATE (with diff) / UNCHANGED / ERROR; upsert is
  file-wins. `MANAGE_CHANNELS` at **global** scope, fail-closed — the per-company
  scoping of `POST /channels` does not extend here because the import
  deliberately leaves `primary_org_unit_id` unset. The apply serializes on the
  month-close advisory guard so a LOCKED month's `revenue_required` cannot be
  flipped underneath it. Parse/plan lives in `org/channel_import.py`, apply+audit
  in `org/channel_import_apply.py`, leaving the route as orchestration.
- ✅ CMS group sync (PR #169, 2026-08-05, branch `feat/cms-group-sync`) —
  `POST /channels/groups/sync` mirrors a YouTube CMS content owner's groups
  into `channel_groups`: real titles, membership set-reconciled with adds AND
  removals, DEACTIVATE when a group vanishes upstream, REACTIVATE when its key
  returns; mandatory dry-run with a full per-group diff; global `MANAGE_GROUPS`
  fail-closed; `GROUP_UPDATED` per changed group + `GROUPS_SYNCED` summary
  accumulated from actual write-boundary outcomes, atomic with the group
  writes (`PlatformLaneAuditSink` on the tenant transaction — proven on
  Postgres incl. the lost-commit path). Unknown CMS member channels are
  surfaced (capped list + total), never auto-created — `POST /channels/import`
  stays the only channel-creation path. The groups API now 409s manual
  rename/membership edits on synced groups (`active`-only PATCH allowed);
  manual groups are untouched. New read surface
  `YouTubeGroupsClient` (`groups.list`/`groupItems.list`). **Scope caveat:**
  `groups.list` runs on the already-granted `yt-analytics.readonly`, but
  `groupItems.list` does NOT — per Google's GroupItems: list authorization
  notes it needs `.../auth/youtube` alone, or `youtube.readonly` together with
  `yt-analytics.readonly`. A credential holding only `yt-analytics.readonly`
  lists the groups and then fails every member fetch (canned 502). Confirm the
  stored credential's scopes and re-consent before the first live sync. The
  import's `group_id` CSV column
  is legacy-but-working: sync converges whatever it created. The import also
  gained `will_adopt_content_owner` per row, so its dry run discloses the one
  permanent write its `outcome` never implied — attaching to an owner-NULL
  group stamps that group's `content_owner_id`. **Both follow-ups this entry
  left open are now CLOSED by PR #170** (owner-stamp recovery, entry above):
  the import no longer adopts at all — it refuses the row and names sync as the
  remedy (Path A), and `will_adopt_content_owner` is gone from the import
  response (it survives only on the sync response, where adoption is
  legitimate); and `DELETE /groups/{id}/content-owner` is the `MANAGE_GROUPS`
  clear-stamp remedy for a wrong stamp. Spec:
  `Docs/superpowers/specs/2026-08-05-cms-group-sync-design.md`.
- ✅ Channel Registry Phase 1 wiring — merged to main as PR #73 (56bf9a8): the
  Registry table is wired to `GET /channels` (replacing the `REGISTRY_ROWS`
  mock). All display fields derived client-side (avatar, CMS badge, source
  label, state per Option A, trace key). Extracted to `views/RegistryView.tsx`;
  16 new Vitest tests. All six dashboard pages off mock data.
- ✅ Soft Dark design system — PR #79, on `feat/design-system-softdark` (stacked
  on Registry Phase 2): `frontend/src/styles.css` token values converted to the
  UMS Revenue Design System Soft Dark theme (dark_dimmed surfaces/ink/status,
  `--ink-strong` money tier, DS weight tiers, srgb topbar color-mix fix); OFL
  webfonts shipped in `frontend/public/fonts/` (+licenses); `DESIGN.md`
  re-pinned to the new palette/fonts. Visual-only — zero selector/logic change;
  vite build + 190 Vitest + tsc green.
- ✅ Channel Registry Phase 2 — PR #78, on `feat/registry-phase2`: `GET /org-units`
  (read-only, tenant-scoped, active-only, fail-closed VIEW_ANALYTICS; no
  migration) resolves Company/Sector display names with an honest raw-id
  fallback and supplies the Map modal's company options. Live write paths:
  Map → `PATCH /channels/{id}/mapping` (audited reason, in-flight latch,
  reload-on-success, typed inline errors incl. the unmapped-channel
  global-grant dead-zone); Assign → `POST /revenue/channel-account-links`
  (UNVERIFIED OPERATOR_ASSERTED proposal; verify/reject stays the admin API
  flow); Review → Trace navigation preselected on the channel. Backend +5 TDD
  org-units tests; frontend 189 Vitest green (10 new RegistryView + 5 hook).
  Mapping-route month-lock enforcement (the pre-existing gap named here) is now
  CLOSED by PR #98 — `PATCH /channels/{id}/mapping` rejects (409) a re-parenting
  that would rewrite a LOCKED month's attribution. The bulk inventory import
  format is no longer definition-blocked either — it shipped as
  `POST /channels/import` in PR #159 (see the entry below). Remaining
  (definition-blocked): the "Scoped changes" tile.
- ✅ Google source-reported revenue ingestion foundation: `currencies`
  reference table, tenant-scoped `google_revenue_source_rows` with idempotent
  source-row keys (full 64-char SHA-256 hex), storage repository, synthetic-
  fixture parsers for YouTube Reporting / YouTube Analytics / AdSense
  Management. PostgreSQL-backed migration round-trip on disposable
  `postgres:18-alpine` — PR #43. Review-hardened (CodeRabbit + Codex round):
  repository typed-validation boundary extended (ASCII report_month,
  nullable-text types, date-not-datetime, ≤6-decimal scale, JSON-serialisable
  raw_payload) + provenance-preserving upsert (COALESCE), AdSense fail-closed
  accountId / empty-report handling, and strict YYYY-MM-DD parsing — see
  `Docs/pulls/2026-05-23-pr43-spec-b1-google-revenue-source-ingestion-report.md`.
  **B2 live credentials CLOSED (2026-08-03 reconciliation).** The 2026-06-22
  operator smoke ran `run_google_connector.py --month 2026-04` against content
  owner (content-owner id redacted) and produced 25 real
  `monthly_channel_revenue_facts` totalling a redacted USD amount, reconciling to the cent
  against the source rows (PRs #132 credential contract, #134 smoke CLI, #135
  live-run gate). Lifted to ✅ — a real ingestion path has now produced facts.
  Remaining: FX/conversion (B3).
- ✅ Google source-rows -> revenue facts normalization bridge: pure
  `select_canonical_row()` rule per `source_system`, USD-only writes,
  upfront locked-month gate via `get_or_create_month_close_row(..., for_update=True)`,
  read-before-write CREATED/UPDATED/UNCHANGED classification — PR #44.
  Bridges PR #43's `google_revenue_source_rows` substrate to the existing
  `MonthlyChannelRevenueFactORM` via
  `SqlAlchemyRevenueFactRepository.record_fact()`. No schema delta, no new
  exception classes, no Alembic migration. 26 named SQLite tests + 5-test
  PostgreSQL companion (verifies the real `pg_advisory_xact_lock` + `SELECT
  ... FOR UPDATE` lock path executes against a live engine). The bridge now has
  a production caller: `run_one` runs `GoogleSourceNormalizer.normalize_month`
  as a post-run step (PR #90, refactored into
  `connectors/runs/normalization.py` by PR #93), so a connector run projects
  source rows to `MonthlyChannelRevenueFactORM`. **B2 live credentials CLOSED
  (2026-08-03 reconciliation)** — the 2026-06-22 live smoke drove this exact
  bridge end to end, projecting 25 channels of real CMS revenue into
  `MonthlyChannelRevenueFactORM`. Lifted to ✅: a live data source has now
  produced facts. Remaining: FX/conversion (B3). See
  `Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md`
  and `Docs/superpowers/plans/2026-05-25-spec-c1-google-source-normalizer.md`.
- ✅ Track E (PR #85, 2026-06-08) — **Postgres RLS enforcement DONE** (S3
  storage-layer hardening). Migration `20260608_0001` creates the `app_tenant`/`app_platform`
  roles and a `<table>_tenant_isolation` policy on all 25 tenant-scoped tables
  (allowlist drift-checked against live `information_schema`). The tenant
  context is held in an `app_tenant_context` table keyed by `backend_pid` (NOT a
  Postgres GUC): a SECURITY DEFINER `set_app_current_tenant_id(uuid)` writes the
  row while `app_platform` is active, the RLS policies read it through
  `app_current_tenant_id()`, and a SECURITY DEFINER `clear_app_current_tenant_id()`
  (migration `20260609_0002`) clears it; the app lanes hold only SELECT on the
  table, so a tenant lane cannot forge its own context. Single-pool
  `SET LOCAL ROLE` realization (not dual-pool) via a Postgres-only,
  context-gated `after_begin` hook in `db/session.py` that switches to
  `app_platform`, sets/clears the trusted context row, then drops a tenant-lane
  session to the restricted `app_tenant` role (no-op on SQLite and on
  tenant-lane sessions without tenant context — missing context is the
  fail-closed RLS signal), `build_platform_session_factory`, and the
  `assert_tenant_match` write-path helper (write-path classification =
  all COVERED-ELSEWHERE). Runtime login needs
  `GRANT app_tenant/app_platform TO <login> WITH INHERIT FALSE, SET TRUE`;
  migration is idempotent and does not assume superuser. See
  `Docs/superpowers/plans/2026-06-08-track-e-tenant-rls-source-currency.md`.
- ✅ FORCE RLS (PR #106, branch `feat/force-row-level-security`) —
  **`FORCE ROW LEVEL SECURITY` DONE** (S3 tenant-hardening follow-up to Track E).
  Migration `20260612_0002_force_tenant_rls` runs
  `ALTER TABLE ... FORCE ROW LEVEL SECURITY` on all 25
  `db.rls.TENANT_SCOPED_TABLES`, reusing the ENABLE migration's drift primitive
  (`discover_tenant_tables_sql` vs `TENANT_SCOPED_TABLES`, subtracting the
  `app_tenant_context` helper via `TENANT_CONTEXT_TABLE`) so a new tenant table
  cannot ship un-`FORCE`d. ENABLE (20260608_0001) already bound the non-owner app
  roles; `FORCE` additionally binds the **non-superuser table owner** (which
  Postgres otherwise lets bypass RLS), closing the owner-bypass gap as
  defense-in-depth that completes Track-E isolation. Superuser / `BYPASSRLS`
  roles still bypass by Postgres design (so the postgres test-login owner is
  unaffected and existing PG RLS tests stay green); Postgres-only (dialect-guarded
  no-op off Postgres), idempotent, rolls back with `NO FORCE` leaving the
  isolation policies in place. Behavioural A/B proof (throwaway non-superuser
  owner) in `tests/tenancy/test_force_rls.py`; migration-state proof
  (`relforcerowsecurity`) alongside it. See
  `Docs/17_MULTI_TENANT_ARCHITECTURE.md` "FORCE ROW LEVEL SECURITY follow-up".
- ✅ Track E (PR #85, 2026-06-08) — **B1 source-rows read API DONE.**
  `GET /revenue/source-rows?month=&source_system=` + `/{id}`,
  `finance.view_revenue`-gated, tenant-scoped, keyset-paged
  (`{items, pagination:{limit, returned, has_more, next_cursor}}`);
  `raw_payload` never returned (`raw_payload_redacted` always true);
  half-cursor/bad input -> 422, missing/cross-tenant id -> 404. The
  paired-column `*_usd` -> native migration is still ⏳ PENDING as a separate
  future spec (out of scope for Track E).
- ✅ Main red-gate fix (PR #88, 2026-06-09) — **merged Track-E PG suite restored to green.**
  Five clusters fixed on `fix/main-red-gate`: session hook cleared tenant context
  via a new SECURITY DEFINER `clear_app_current_tenant_id()` fn (migration
  `20260609_0002`) + tolerated absent context objects (was a raw `DELETE` →
  permission denied); RLS migration `downgrade()` now `DROP OWNED BY` before
  `DROP ROLE`; SQLite engine uses `StaticPool` (single writer) to end the
  audit-session "database is locked" contention; tenant-context getter test
  compares typed `uuid`; version baseline realigned to pyproject pins. Full
  suite 1956 passed.

- ✅ Track F (PR #87, 2026-06-09) — **Smart revenue reconciliation workflow DONE.**
  Pure compute core derives US tax + YouTube->AdSense fee + AdSense->bank fee+FX
  from actual figures, attributed per channel proportional to CMS gross with a
  rounding-remainder rule; persists typed `deduction_components` +
  `revenue_reconciliation_usd` explanation (deterministic prose; only TAX feeds
  `net_revenue_usd`, transfer-fee/FX are evidence-only).
  `POST /revenue/months/{month}/reconcile` (`CHANGE_ALLOCATION_RULE@finance_month`,
  409 locked / 422 bad month) + `GET .../reconciliation` (`VIEW_REVENUE@channel`,
  404 none). **Outside-CMS 1:1 ALLOCATION attribution DONE** (single verified
  account->channel link writes the gross fact; many -> skip + warn).
- ✅ Track F (PR #87, 2026-06-09) — **Manual report purge DONE.**
  `DELETE /reports/raw-files/{id}` (`MANAGE_CONNECTORS@connector(source)`,
  reason-required -> 422, 404 unknown/cross-tenant, 409 re-purge) marks PURGED
  keeping metadata; additive `purged_at`/`purged_by` columns + CHECK swap.
  ⏳ Refine-later: real US-view-share feed, withholding-rate calibration, and
  multi-API-key ingestion scaling. **Rate ruling + display-estimate program:**
  Docs/24 (15% treaty estimate; recon `0.30` path stays fenced / Docs/21 P3).
- ✅ Phase 5 analytics & monitoring surface (PR #98, 2026-06-13, branch
  `feat/phase5-analytics-monitoring`) — one combined PR closing the
  highest-value Phase 1 / 5 / 7 acceptance-gate gaps plus this doc
  reconciliation:
  - **Company/sector/channel rankings** — finance-gated, scope-safe
    `GET /revenue/months/{month}/rankings` (pure `finance/rankings.py`
    `build_month_rankings` rolls the per-channel net-revenue summary up to
    company/sector, ranks each dimension by gross|net|deduction with None-sink +
    stable id tie-break, top-N; money via `decimal_to_api`, None preserved) +
    CommandView rankings panel (own hook, money gated on `canViewFinance`,
    metric toggle, surfaces `allocation_source`). Copies `get_month_net_revenue`'s
    VIEW_REVENUE + VIEW_CONFIDENCE + VIEW_FINALIZED_PAYMENTS gates and dual
    REVENUE_VIEWED + PAYMENT_VIEWED audit; scoped read restricts the channel set
    before ranking (zero channels -> empty, not 403).
  - **`CHANNELS_MISSING_REVENUE_FACTS` coverage alert** — pure `smart_alerts.py`
    emits a per-channel coverage gap (severity HIGH, confidence E_MISSING,
    `{channel_count, sample_channel_ids capped 20}`) for active+revenue_required
    channels with no monthly fact; the route pre-reads the id list with the same
    query shape as close-readiness (no new permission). Missing-REPORT detection
    was deferred here for want of an expected-connectors baseline; PR #131 later
    closed it from the connector side instead — an empty YouTube Reporting
    report list is now a typed per-report failure that surfaces as the
    `CONNECTOR_RUNS_FAILED` alert (see the entry below).
  - **Mapping-route month-lock** — `PATCH /channels/{id}/mapping` now rejects
    (409) a re-parenting that would rewrite a LOCKED month's attribution
    (read-only locked-fact guard in `org/sql_channel_registry.py`, typed
    `ChannelMappingLockedMonthError`; SQL-path only — the in-memory registry is
    unchanged).
  - **Outside-CMS / channel-issues monitor panel** — CommandView panel wired to
    `GET /channels/outside-cms` + `GET /channels/issues` (VIEW_ANALYTICS,
    no-fetch-when-restricted; 403 -> denied, 503 -> unavailable).
  - **`canViewAnalytics` session capability** — `/session/me` gains a
    scope-aware `can_view_analytics` (any active VIEW_ANALYTICS grant at any
    scope; fail-closed disabled -> false) threaded through the FE AppShell to
    gate the monitor panel.
- ✅ Command Center group/sector rollup scope selector (PR #102, 2026-06-13,
  branch `feat/group-sector-rollup`) — closes the Phase 1 / 5 acceptance gate "user
  selects month + group/sector and receives source-backed gross/deduction/net"
  for **global / sector / company** rollup:
  - **`GET /revenue/scopes`** — new fail-closed read-only endpoint
    (`finance.view_revenue`; disabled or no active grant in any scope -> 403
    `Missing permission: finance.view_revenue`, never a silent empty list) that
    performs **no audit write** (metadata helper like `GET /org-units`, not a
    revenue-number disclosure). Pure `finance/revenue_scopes.py`
    `build_authorized_revenue_scopes` returns ONLY the viewer's authorized
    rollup scopes: a global grant -> global + every active sector + company; a
    sector grant -> the sector + its member companies (reverse `company_sector`
    walk mirroring `OrgAccessIndex.contains`); a company grant -> that company
    only (never its sector). Dedup by (scope_type, scope_id); names via the
    org-unit reader with raw-id fallback; deterministic order (global, then
    sectors by name, then companies by name); `global` present only with a
    global grant. Response `{scopes:[{scope_type, scope_id, label}]}`. This is
    the anti-scope-leak surface — the selector can never over-list the org
    structure or offer a dead option that 403s on the rollup read.
  - **Dynamic CommandView selector** — `useRevenueScopes` hook + the
    `<select aria-label="Scope">` now populate from `GET /revenue/scopes`
    (replacing the hardcoded single global entry), with scope state keyed on a
    stable `{scopeType, scopeId}` pair and a global-only fallback while loading
    or on 403/error (panels still fail-closed on the actual reads). The chosen
    month + scope thread unchanged into the already-wired net-revenue + rankings
    reads.
  - ✅ **Channel-GROUP revenue scope** (PR #122, branch
    `codex/channel-group-revenue-scope`, merged 2026-06-19) — `group` is now a
    runtime finance `scope_type` for the selector, net-revenue reads, rankings,
    and recalculation dry-run previews. Group options come from the channel-group registry and are
    listed only when active, non-empty, and every member channel is covered by
    the caller's revenue grants; read paths resolve group_id -> member
    channel_ids and enforce per-channel authorization as the AND of
    `AccessScope.channel(cid)` checks. Stored grant scopes remain
    global/sector/company/channel/etc.; no persisted group grant or migration is
    required.
- ✅ Surface projection-skipped source rows (PR #111) — `normalize_after_run`
  previously discarded `result.skipped` silently, so revenue rows for
  unknown/inactive channels vanished from the fact projection with no
  audit, alert, or log while the run reported SUCCEEDED. Now emits one
  `CONNECTOR_JOB_RUN` `ROWS_SKIPPED` summary audit edge (counts by reason,
  finance-month scoped) + WARNING log, and the same finance-month audit edges
  feed the `SOURCE_ROWS_SKIPPED` dashboard smart alert. Ingest/finance numbers
  unchanged; only the silent drop is now observable. **Planned U2 exception:**
  `NON_PROJECTING_EVIDENCE` remains in raw audit telemetry but is removed from
  actionable alert counts before sensitive-reason redaction, so healthy country
  evidence does not manufacture a dropped-row WARNING or HIGH alert for either
  dashboard/export audience; every genuinely actionable or unclassified positive skip
  keeps the existing behavior.
- ✅ Gate `/security/*` catalog endpoints behind VIEW_AUDIT_LOG (PR #112) —
  `GET /security/roles` and `GET /security/permissions` previously returned
  the full role→permission catalog (including `sensitive`/`auditOnUse` flags)
  to any authenticated user. Both now require `VIEW_AUDIT_LOG` at global
  scope (fail-closed 403), per `ROLE_PERMISSION_MODEL.md`. SPA does not
  consume either endpoint so no UI impact.
- ✅ Command Center reconciliation cards (PR #127, merged 2026-06-19) —
  CommandView gains a `BankReconciliationStatusStrip` of three cards (AdSense
  payment, bank received, unresolved gap) fed by
  `GET /revenue/months/{month}/bank-reconciliation` through a new
  `useBankReconciliation` hook. The read is disabled unless the session holds
  **both** the payment and bank-reconciliation grants (fails closed), is
  isolated from net-revenue/smart-alert render failures, and hides backend
  diagnostics (403 → permission copy, 5xx → status code only).
  `MonthBankReconciliationSummary.to_api()` gained an additive `money_provenance`
  map (source, formula, confidence token, export value per official money
  field) — serializer metadata only, no persistence or calculation change. The
  `canViewPayments` / `canViewBankReconciliation` session capabilities became
  scope-aware (GLOBAL and FINANCE_MONTH only, disabled users fail closed), so a
  finance-month-scoped admin now gets the render hint while the routes still
  re-check per requested scope.
- ✅ Analytics summary CSV export (PR #129, merged 2026-06-20) —
  `ANALYTICS_SUMMARY_CSV` generation/download over normalized
  `youtube_analytics` source rows (`reports/analytics_summary_csv.py`), with
  artifact persistence, checksum metadata, and scoped CSV output. **Permission
  change:** because the CSV carries revenue amounts, creation *and* download
  require `finance.view_revenue` in addition to `exports.analytics` +
  `analytics.view` for the requested scope — analytics-only export operators can
  no longer queue or download it. Served artifacts emit both `REVENUE_VIEWED`
  and `EXPORT_DOWNLOADED`.
- ✅ Configurable export templates (PR #130, merged 2026-06-20) — tenant-scoped
  CRUD under `/export-templates` (`api/export_templates.py`) plus an optional
  `template_id` on export jobs, validated for tenant, active state, and matching
  `export_type` before persist. Additive migration `20260620_0001_export_templates`
  (new `export_templates` table + nullable `export_jobs.template_id`);
  `EXPORT_TEMPLATE_CHANGED` audit coverage.
- ✅ Alert on missing connector reports (PR #131, merged 2026-06-21) — a
  configured YouTube Reporting job that returned an empty report list used to
  finish as a silent success with zero ingested rows. `_missing_youtube_report_failure`
  now converts that empty list into a typed `ProducedReportFailure`
  (`GoogleApiResponseError` with a sanitized synthetic URL — no listing URL,
  credential, or upstream payload exposed), so the run reports the gap. The pure
  builder in `finance/smart_alerts.py` gained `_connector_runs_failed_alert`,
  emitting a HIGH `CONNECTOR_RUNS_FAILED` alert when the latest terminal
  `CONNECTOR_JOB_RUN` audit edge per (connector, account) for a month is FAILED
  or PARTIAL; a later SUCCEEDED edge for the same pair clears it, and connector-key
  normalization collapses the `youtube-reporting`/`youtube_reporting` aliases so
  a success on either clears an older failure. Read model:
  `SqlAlchemyAuditLogRepository.connector_run_failure_summary()`. Gated on global
  `audit.view` (same gate as `SOURCE_ROWS_SKIPPED`), but carrying no sensitive
  skipped-row reasons its `details_redacted` is always False. Surfaced on the
  monthly smart-alerts route and on global finance export source summaries
  (scoped exports suppress the tenant-wide signal until connector runs can be
  tied to the frozen channel set). No migration — `audit_logs` stays the source
  of truth.
- ⏳ FX exchange-rate scaffold — `backend/ums_smart_revenue/finance/exchange_rates.py`,
  `api/exchange_rates.py`, and migration `20260513_0004_currency_exchange_rates`
  exist as legacy scaffolding (`POST /exchange-rates/sync`,
  `GET /exchange-rates/latest`). The B1 pivot (PR #42) retired FX-rate-led
  official finance design; public/provider FX rates are not an official
  source for monthly revenue, tax, or deduction. The scaffold remains for a
  future display-only conversion path. Remaining: display conversion wiring
  (see `Docs/18_MULTI_CURRENCY_ENGINE.md`).
- ✅ Deduction-ingestion CLI — `scripts/run_deduction_ingestion.py` is an
  operational CLI for importing deduction components from `source_rows`,
  `bank`, and `gap` data sources via `DeductionIngestionService`. Ships
  alongside the Track F reconciliation service (PR #87).

## Known issues on `main` — candidates for a follow-up PR

Recorded 2026-08-13 against `main` at `9435af29` (the squash merge of PR #184),
measured rather than recalled: every figure below comes from a command run at
that commit, and the command is given so it can be re-derived. Nothing here
blocks anything shipped; all of it is the residue a green pipeline does not
show.

### What "green" currently means on `main`

| Gate | Result at `9435af29` | Notes |
| --- | --- | --- |
| `uv run pytest -q` | **2817 passed, 15 warnings, exit 0** | 9m38s and 8m56s on two runs — expect ~9-10 min, not a fixed number |
| `uv run ruff check backend tests scripts` | All checks passed | `line-length = 100` (`pyproject.toml:47`) — matches DeepSource FLK-E501 |
| `uv run mypy backend` | **1 error** | NOT one of the four AGENTS.md gates — see B below |
| `bun run test` (frontend) | 477 passed, 41 files | run from `frontend/` — `(cd frontend && bun run test)`; `bun`, never `npx` |
| `(cd frontend && bunx tsc --noEmit)` / `(cd frontend && bun run build)` | clean | at `9435af29` there was no `typecheck` script in `frontend/package.json` — the compiler was invoked directly; current main adds `bun run typecheck` as the canonical wrapper |
| `git diff --check` | clean | |
| DeepSource (6 analyzers) | all SUCCESS | |

**The pytest figure is only trustworthy against a FRESH Postgres container.**
It needs `UMS_TEST_DATABASE_URL`; a reused container carrying a stray schema
from an earlier round reports **23 failures**, every one an RLS/migration test
and none of them touching the code under change. Both runs above used a
disposable `postgres:18-alpine` container created for the run and removed after.
Matches the repo-standard image in `tests/db/_postgres_helpers.py` and
`docker-compose.yml` (PostgreSQL 18). This recipe is **self-contained** — do
not mix its DSN (`127.0.0.1:55505`, db `test_ums`, password `postgres`) with
the migration-tier example in `tests/db/_postgres_helpers.py` (`55432`,
db `postgres`, password `ums`, container `ums-mig-pg`).

**POSIX shell** (inline env assignment is POSIX-only):

```bash
set -e
uv sync --extra dev --extra test --extra lint
command -v docker >/dev/null 2>&1 || {
  echo "docker is required for this recipe" >&2
  exit 1
}
docker rm -f ums-verify 2>/dev/null || true
docker run -d --name ums-verify \
  -e POSTGRES_PASSWORD=postgres \
  -e POSTGRES_DB=test_ums \
  -p 127.0.0.1:55505:5432 \
  postgres:18-alpine || {
  echo "Failed to create ums-verify container; remove stale instance and retry" >&2
  exit 1
}
ready=0
i=0
while [ "$i" -lt 60 ]; do
  if docker exec ums-verify pg_isready -U postgres -d test_ums >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 1
  i=$((i + 1))
done
if [ "$ready" -ne 1 ]; then
  echo "Postgres did not become ready within 60s; try: docker logs ums-verify" >&2
  docker rm -f ums-verify
  exit 1
fi
set +e
UMS_TEST_DATABASE_URL="postgresql+psycopg://postgres:postgres@127.0.0.1:55505/test_ums" uv run pytest -q -rw
pytest_exit=$?
set -e
docker rm -f ums-verify || true
exit $pytest_exit
```

**PowerShell** (Windows dev — use `$env:` assignment, not inline prefix):

```powershell
uv sync --extra dev --extra test --extra lint
if (-not $? -or $LASTEXITCODE -ne 0) {
  Write-Error "uv sync failed"
  exit 1
}
$null = Get-Command docker -ErrorAction Stop
docker rm -f ums-verify 2>$null
docker run -d --name ums-verify `
  -e POSTGRES_PASSWORD=postgres `
  -e POSTGRES_DB=test_ums `
  -p 127.0.0.1:55505:5432 `
  postgres:18-alpine
if (-not $? -or $LASTEXITCODE -ne 0) {
  Write-Error "Failed to create ums-verify container; remove stale instance and retry"
  exit 1
}
$ready = $false
for ($i = 0; $i -lt 60; $i++) {
  Start-Sleep -Seconds 1
  docker exec ums-verify pg_isready -U postgres -d test_ums *> $null
  if ($LASTEXITCODE -eq 0) { $ready = $true; break }
}
if (-not $ready) {
  Write-Error "Postgres did not become ready within 60s; try: docker logs ums-verify"
  docker rm -f ums-verify
  exit 1
}
$env:UMS_TEST_DATABASE_URL = "postgresql+psycopg://postgres:postgres@127.0.0.1:55505/test_ums"
uv run pytest -q -rw
$pytestExit = $LASTEXITCODE
docker rm -f ums-verify
exit $pytestExit
```

### A. The 15 pytest warnings — two causes, both dependency-config

Captured with `pytest -q -rw` so the summary is verbatim rather than
reconstructed. The 15 are **14 + 1**, and they account for the total exactly.

#### A1 — Alembic `path_separator` (14 of the 15)

```
.venv/Lib/site-packages/alembic/config.py:612: DeprecationWarning:
  No path_separator found in configuration; falling back to legacy splitting
  on spaces, commas, and colons for prepend_sys_path.
  Consider adding path_separator=os to Alembic config.
```

Distribution — one per Alembic `Config` load, which is why the tenancy suites
dominate:

| Test file | Warnings |
| --- | --- |
| `tests/tenancy/test_rls_grant_surface.py` | 7 |
| `tests/tenancy/test_isolation.py` | 4 |
| `tests/db/test_alembic_env_url_precedence_postgres.py` | 1 |
| `tests/db/test_session_tenant_hook.py` | 1 |
| `tests/tenancy/test_rls_restricted_login.py` | 1 |
| **Total** | **14** |

**Cause.** `alembic.ini:3` sets `prepend_sys_path = backend` and the file
declares no `path_separator`, so Alembic 1.18.5 falls back to legacy splitting
and warns once per config load.

**Fix.** Add `path_separator = os` to the `[alembic]` block of `alembic.ini`.

**Risk to check before merging that one line, not after.** `os` resolves to
`os.pathsep`, which is `;` on Windows and `:` on Linux — dev here is Windows,
CI is Linux. The current value is a **single** path (`backend`) with no
separator in it, so no split can change meaning today; the change is safe now
and the note exists so nobody adds a second path later without revisiting it.
Re-run the Alembic-touching suites specifically (`tests/db/`,
`tests/tenancy/`) rather than trusting a full-suite pass, since those are the
only ones that load the config.

#### A2 — Starlette TestClient / httpx (1 of the 15)

```
.venv/Lib/site-packages/fastapi/testclient.py:1: StarletteDeprecationWarning:
  Using `httpx` with `starlette.testclient` is deprecated; install `httpx2` instead.
    from starlette.testclient import TestClient as TestClient  # noqa
```

Emitted once at import, from **inside FastAPI's own `testclient` shim** — not
from our code, and there is no call site of ours to change.

Installed versions at `9435af29`: `starlette 1.3.1`, `httpx 0.28.1`,
`httpx2` **not installed**, `fastapi 0.141.1`.

**Fix.** A dependency migration to `httpx2`, not a code edit. It is the larger
of the two by a distance: `httpx` is a direct dependency of the app as well as
the test client, so the move needs its own PR with the connector/HTTP paths
re-exercised (Google/AdSense clients especially), and it should NOT be bundled
with A1.

**Deliberately not silenced.** A `filterwarnings` entry in the pytest config
would zero the count without changing anything real, and the warning is the
only current signal that this migration is pending.

### B. The mypy error on `main`

```
backend/ums_smart_revenue/devtools/pytest_policy_gate.py:597: error:
  Need type annotation for "candidates"
  (hint: "candidates: list[<type>] = ...")  [var-annotated]
```

The function, in full:

```python
def _resolve_pytest_plugin_path(project_root: Path, module: str) -> Path | None:
    if not module or module.startswith("."):
        return None
    module_parts = module.split(".")
    candidates = []                      # <- line 597
    for root in (project_root, project_root / "backend"):
        module_path = root.joinpath(*module_parts)
        candidates.extend((module_path.with_suffix(".py"), module_path / "__init__.py"))
    return next((candidate for candidate in candidates if candidate.exists()), None)
```

**Fix.** One line: `candidates: list[Path] = []`. `Path` is already imported in
the module.

**Provenance — it is NOT from PR #184.** `git log -1 -- <that file>` returns
`16b6bb58`, PR #105 (`feat(connectors): credential token-health surface`),
merged 2026-06-15. It has been on `main` for about two months.

**Why it survived that long, which is the part worth fixing.** `mypy` is not
one of the four AGENTS.md baseline gates (L113-121: `uv sync`, `ruff`,
`pytest`, `git diff --check`), but **`CONTRIBUTING.md` and `README.md` still
direct contributors to run `uv run mypy backend` before push**, and
**DeepSource's Python analyzer does**. That makes it an ungated gate: reachable
by review and documented workflow, invisible to the four-gate local loop. Run
`uv run mypy backend` before any push regardless of the four gates.

### C. Deferred design questions from PR #184

> **Post-snapshot update.** Everything above reflects `main` at `9435af29`.
> The rulings below were recorded on 2026-08-21; the implementing code lives
> on the follow-up branches cited (PR #195, PR #196), not on `9435af29` itself.
> Re-derive ship status from those PR heads, not from this snapshot commit.

Both were escalated during review rather than guessed at, and both were RULED
on 2026-08-21 (operator decision, recorded per-question below). Each is a real
change with a real failure mode, not a nit.

1. ✅ **Bind the displayed plan contents to the fingerprint**
   (`PRRT_kwDOSZIgN86YC1sW`). `plan_fingerprint` is a server-computed digest the
   client cannot recompute — it folds in the server-resolved tenant. So a
   malformed 2xx that keeps a valid fingerprint while substituting a different
   but structurally valid plan passes every client-side check; Apply then echoes
   the original token with the original CSV and the backend writes the real
   plan, though the operator reviewed different contents. **Ruled: display
   digest** (not the tenant-disclosure option) — **tracked in PR #195**
   (`feat/import-display-digest`): `display_digest` = SHA-256 over canonical JSON of exactly the
   disclosed reviewed set (counts, rows, content_owner_id, cms_status;
   tenant-free, hence recomputable), optional `expected_display_digest` on the
   apply checked independently of the fingerprint, either token opts into
   strict pre-state enforcement, SPA echoes both tokens, contract + recipe in
   Docs/12. One flagged residual on the PR: digest-ONLY applies are tenant-free
   by that same design — cross-tenant binding stays the fingerprint's job.

2. ✅ **Roll back inventory when group-action validation fails**
   (`PRRT_kwDOSZIgN86YGxak`). `_require_planned_group_actions` is a lock-free
   pre-flight that already runs BEFORE the first inventory write
   (`channel_import_apply.py:331`), which closed the direct-caller case. The
   residual window is a group appearing or disappearing between that pre-flight
   and the locked second pass: `_apply_inventory_writes` has already replaced
   every channel, and a store without a transaction cannot take those back when
   the group pass raises. Production SQL is transactional; the exposure is the
   in-memory adapters used by direct/test/bootstrap callers. **Ruled:
   transaction boundary** (not a compensating restore) — **tracked in PR #196**
   (`feat/import-store-transaction`): an explicit `transaction()` boundary on both store
   protocols (implemented on that branch; absent on `main` at `9435af29`). The SQL adapters delegate to the request's own session
   transaction (nothing opens, nothing commits; internal savepoints only). The in-memory
   adapters keep an undo JOURNAL of their own write methods and replay it
   backwards on raise — own writes undone, a concurrent writer's interleaved
   change survives, matching SQL rollback semantics, which the existing race
   tests pin. `apply_channel_import` wraps the pre-flight, both passes, and a
   buffered audit flush (`_BufferedAuditSink`, defined on the PR #196 branch) in one boundary, so a mid-apply
   failure no longer even transiently writes audit rows on any tier. Noted
   follow-up on the PR: `channel_group_sync_apply.py` has the analogous
   in-memory exposure and can now adopt the same boundary.

### D. Analyzer dispositions carried forward — NOT defects

Recorded so nobody re-opens them, and so the reasoning is auditable rather than
asserted.

- **Qodo, 6 × "Empty `Connections:`"** — the matcher requires content on the
  same line as the label; AGENTS.md L209-225 puts the label on its own line with
  entries beneath it, which is what every flagged block does. Qodo reviewed the
  evidence and agreed in writing: *"matcher/configuration findings, not missing
  contract coverage or code defects."* They were left **active, not dismissed** —
  no ignore rule, no suppression — and they double as a coverage detector,
  firing whenever a new contract block lands.
- **DeepSource `SCT-A000`** on `Docs/` files — the accepted syntax-matcher
  ruling from the baseline program. Do not re-litigate.

### Suggested PR split

Three PRs, in this order. A1 and B are trivial and independent; keep them apart
from the two that need design.

| PR | Contents | Size |
| --- | --- | --- |
| 1 | A1 (`path_separator = os`) + B (`candidates: list[Path]`) | two lines, plus a re-run of `tests/db/` and `tests/tenancy/` |
| 2 | A2 — `httpx2` migration | dependency-wide; re-exercise the connector HTTP paths |
| 3 | C1 and/or C2 — the two #184 design questions | one each; both RULED 2026-08-21; C1 tracked in PR #195, C2 tracked in PR #196 (code on those branches, not on `9435af29`) |

## Hard problems to solve early

1. ⏳ Revenue source for 70 outside-CMS channels — partially solved: Track F
   (PR #87) attributes outside-CMS revenue via the single verified
   account->channel link (1:1 ALLOCATION writes the gross fact; many-link ->
   skip + warn). Channels with zero or multiple verified links remain open, and
   the CommandView outside-CMS monitor panel (PR #98) now surfaces the
   outstanding "outside CMS + missing source" set.
2. ⏳ System-managed report availability and retention — remaining: raw
   report file ORM + repo (PR #32); ingestion + retention policy not
   built.
3. ✅ Payment gap explanation — shipped with residuals: the composed
   `GET /revenue/months/{month}/gap-explanation` endpoint decomposes both
   chain legs (`youtube_facts -> adsense_paid -> bank_received`) as
   gap = evidence-backed components + unexplained residual, with statuses,
   explain-shape confidence, full money provenance, warnings, and
   deterministic narrative prose, and the Command Center "Gap narrative"
   panel renders it next to the PR #127 bank cards. Residuals by design:
   month-grain only (the PAYMENT-grain receipt-to-account bridge is
   repo-proven absent), and whatever the evidence components cannot cover is
   reported as an UNEXPLAINED/PARTIALLY_EXPLAINED residual, not resolved.
   The codex read-consistency follow-up is RESOLVED
   (`feat/composed-finance-read-consistency`, 2026-08-23): all four composed
   finance reads begin one REPEATABLE READ snapshot on the platform-lane
   session, so mid-read writers can no longer tear composed totals or
   mispair the close status; the smart-alerts tenant-lane audit signals stay
   outside the snapshot by ruling (authorization laning wins). Contract:
   Docs/12 "Composed-read consistency"; proofs:
   `tests/api/test_composed_read_snapshot_postgres.py`. Extended 2026-08-23
   (`feat/composed-read-snapshot`): net-revenue, rankings,
   reconciliation-issues, the channel-month summary, the channel-month
   facts listing and reconciliation-preview (both ride the same
   guard-then-select repository read, whose single-select exemption was
   disproven in review), the deduction-components page (a three-statement
   repository read), and the dry-run recalculation preview begin the same
   snapshot inside extracted `_load_*` loaders after their permission
   gates; scoped attribution (org-unit selection, rankings grouping,
   recalculation readiness map) re-resolves on the snapshot index
   intersected with the gate-time authorized set, group-scoped selection
   re-reads the group row and active roster through the registry on the
   snapshot, intersects deny-only, permission-filters surviving members on
   the snapshot index, and empties for a group archived mid-request (the
   per-member covered-subset authorization stays gate-time), grant coverage
   over org-unit and channel scopes is re-asserted deny-only on that index
   (a reparented target unit — or a channel target moved out of its
   granting unit — 403s; direct channel grants pass on scope identity) with
   the same channel re-check inside the per-channel facts/preview/summary
   loaders, the issue-queue loader intersects its covered sets re-derived
   on the snapshot index before paging, and the allocation resolver's close
   probe reads through the snapshot too — each red→green-proven against
   mid-request org moves, reparents, roster drops, channel moves, a group
   archive, and a mid-request lock.
   Eleven GET routes plus the dry-run recalculation POST are pinned in
   `tests/api/test_composed_read_snapshot_wiring.py`; the recalculation
   write branch and the explain POST stay READ COMMITTED by the recorded
   write-path ruling. Extended again 2026-08-23
   (`feat/export-read-snapshot`): the finance export builder (workbook
   preview + workbook/PDF/slide-pack downloads) composes its finance
   sources on the same snapshot via the platform session — a mid-build
   writer can no longer tear a persisted artifact's totals
   (red→green-proven); audit-derived signals stay tenant-lane and the
   frozen channel set stays gate-time by the export-determinism rule.
4. ⏳ Bank/transfer/local-currency variance evidence — evidence is now
   surfaced: the gap-explanation bank leg reconciles paid AdSense money
   against bank receipts using the operator-entered transfer-fee and signed
   FX-difference evidence (never public FX rates), and the Command Center
   finally renders `fx_difference_usd`; remaining is finance
   adoption/validation of the evidence workflow (entering fees/FX per bank
   row so residuals shrink).
5. ⏳ Confidence labels that finance trusts — remaining: computation rules
   exist (net-revenue B_RECONCILED/D_ESTIMATED/E_MISSING + explain confidence
   label), PR #69 surfaces the explain confidence label + score in the
   Trace/Explain screen, and Track C (`feat/audit-track-c`) surfaces human
   confidence badges on CommandView; remaining is finance adoption/validation
   of those labels.
6. ✅ Flexible grouping without hardcoded UMS structure — channel group
   registry (PR #25 + tests in PR #30).
