# Delivery Backlog

## Status (2026-08-07)

Reconciled through PR #170 (owner-stamp recovery, merged 2026-08-06). Marker
conventions match `01_IMPLEMENTATION_PLAN.md`:

- `✅ PR #N` — shipped end-to-end at the layer being marked.
- `⏳ PR #N — remaining: <note>` — partial; concrete remaining work is
  named.
- `🗑️ removed in PR #N — <reason>` — dropped from scope.

Honesty rule: scaffolding-only items (ORM + repo + tests but no real
ingestion / UI / user-facing path) are marked `⏳`, not `✅`.

**Unmerged-branch note (2026-08-06):** the "Reconciled through PR #N" line
above counts MERGED PRs only, per the PR-numbered marker conventions. The
scheduled group sync is reconciled inline under its branch name
(`feat/scheduled-group-sync`, open as PR #171 at reconciliation time) and is
deliberately OUTSIDE that high-water mark; on merge its entries convert to
`✅ PR #171`.

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

## P2 — Advanced features

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
  is **1921 insertions across eight backend files as of `59e96d68`**, the
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
- ✅ Scheduled CMS group sync (2026-08-06, open PR #171, branch
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
  multi-API-key ingestion scaling.
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
  unchanged; only the silent drop is now observable.
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
3. ⏳ Payment gap explanation — remaining: gap value + comparison ARE computed
   (payment_gap_usd via /revenue/months/{month}/payment-match, bank variance
   via /bank-reconciliation, high-gap smart alerts) and PR #127 now surfaces the
   AdSense-payment / bank-received / unresolved-gap cards in the Command Center,
   so the gap is visible to finance rather than API-only; remaining is a
   dedicated reconciling explanation/narrative pass tying gaps to
   receipts/fees/currency effects.
4. ⏳ Bank/transfer/local-currency variance evidence — remaining: bank recon
   repo (PR #29) is the substrate; variance explanations must reconcile
   Google/AdSense reported money, bank receipts, transfer fees, and bank-side
   currency effects without treating public FX rates as official revenue.
5. ⏳ Confidence labels that finance trusts — remaining: computation rules
   exist (net-revenue B_RECONCILED/D_ESTIMATED/E_MISSING + explain confidence
   label), PR #69 surfaces the explain confidence label + score in the
   Trace/Explain screen, and Track C (`feat/audit-track-c`) surfaces human
   confidence badges on CommandView; remaining is finance adoption/validation
   of those labels.
6. ✅ Flexible grouping without hardcoded UMS structure — channel group
   registry (PR #25 + tests in PR #30).
