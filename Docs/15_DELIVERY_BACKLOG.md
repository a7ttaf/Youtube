# Delivery Backlog

## Status (2026-05-29)

Reconciled through PR #50 (B2.6 connector row-count classification). Marker conventions
match `01_IMPLEMENTATION_PLAN.md`:

- `✅ PR #N` — shipped end-to-end at the layer being marked.
- `⏳ PR #N — remaining: <note>` — partial; concrete remaining work is
  named.
- `🗑️ removed in PR #N — <reason>` — dropped from scope.

Honesty rule: scaffolding-only items (ORM + repo + tests but no real
ingestion / UI / user-facing path) are marked `⏳`, not `✅`.

## P0 — Must build first

- ⏳ Dynamic org hierarchy — remaining: ORG models (PR #25); hierarchy
  assignment workflow not built.
- ✅ Channel registry (tenant-scoped, PR #25).
- ⏳ CMS/outside-CMS status — remaining: schema column (PR #25);
  outside-CMS revenue sourcing unresolved (Hard Problem #1).
- ✅ Group builder (tenant-scoped channel group registry, PR #25 + tests
  in PR #30).
- ⏳ YouTube report ingestion — remaining: credentials repo (PRs #33,
  #34); real ingestion not built.
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
- ⏳ Monthly revenue normalization — remaining: B2 live ingestion wiring
  (revenue facts foundation in PR #2; normalization bridge from
  google_revenue_source_rows shipped in PR #44).
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
- ⏳ Finance month-close screen — remaining: close-gate backend (PR #8);
  UI not built.
- ✅ Net revenue calculation — shipped: GET /revenue/months/{month}/net-revenue
  (build_month_net_revenue_summary; per-channel gross/net/deduction roll-up,
  scope-filtered, USD-only). Tax/deduction ingestion + allocation-rule
  application remain unbuilt (Phase 4).
- ⏳ Confidence labels — remaining: labels ARE computed in services
  (net-revenue B_RECONCILED/D_ESTIMATED/E_MISSING; explain confidence label)
  and returned by the net-revenue/explain APIs; dashboard UI surfacing not
  built.
- ✅ Explain-number API — shipped: POST
  /revenue/channels/{channel_id}/months/{month}/explain
  (build_channel_month_revenue_explanation; per-metric source/formula/
  confidence/warnings, persisted to number_explanations). Explain-number
  drawer UI remains unbuilt (Phase 5).
- ⏳ Smart issue panel — remaining: smart-alerts BACKEND ships (GET
  /revenue/months/{month}/smart-alerts, build_monthly_smart_alert_summary);
  panel UI not built.
- ✅ Excel export — shipped: GET /exports/{export_id}/finance-workbook.xlsx
  (+ preview) via build_finance_workbook_xlsx; tenant-scoped export jobs,
  persisted + audited. Final column template/branding may iterate.

## P1 — Strong beta features

- ✅ PDF export — shipped: GET /exports/{export_id}/executive.pdf
  (build_executive_pdf_bytes; executive summary, gross/net, rankings, problem
  sections, persisted + audited). Layout polish may iterate.
- ✅ Branded slide export — shipped: GET
  /exports/{export_id}/branded-slide-pack.pptx (build_branded_slide_pack_pptx;
  cover + content slides with brand bar/footer, persisted + audited). Final
  theming may iterate.
- ⏳ Outside-CMS monitor — remaining: not started.
- ✅ Recalculation by allocation method dry-run foundation — shipped: POST
  /revenue/recalculate (build_recalculation_preview; dry-run-only
  allocation-method preview with blocking-issue detection; committed writes
  intentionally rejected as not-yet-implemented).
- ⏳ Channel↔account map — shipped (this branch): two-layer canonical map
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
    month, then screens the rest of the range with a single bounded SELECT for an
    already-LOCKED close row, so an authorized far-future `effective_month_end`
    can no longer insert/advisory-lock ~95k `finance_month_close` rows in one
    transaction; `reject` now reloads the link under the per-account advisory lock
    (TOCTOU guard: a concurrent verify committing during the lock-wait is observed
    so the locked-month guard isn't skipped on a stale UNVERIFIED status); the
    duplicated finance-local `_iter_months` helper was removed (now unused).
  - ⏳ Deferred follow-up (PR #57 N9): residual concurrent-close race in
    `_require_range_open`. The covered-range scan reads already-LOCKED months but
    is not serialized against a month-close that transitions a covered month to
    LOCKED *after* the scan but before the verify/reject commits — open-ended and
    long-bounded ranges cannot acquire a per-month advisory lock for every covered
    month. Fully closing it needs a shared serialization point on the month-close
    path (e.g. close also rejecting/handling conflicting VERIFIED links, or a
    per-tenant close-epoch lock) — a close-path change outside this PR's scope.
    Risk bounded: no production consumer of `list_verified_adsense_account_channels`
    until Spec 2b; both verify/reject and close are dual-gated admin actions.
    File: `backend/ums_smart_revenue/finance/channel_account_links.py`
    (`_require_range_open`); sequence with Spec 2b / month-close hardening.
  - ⏳ Deferred follow-up (PR #57 N10): the API allocation-permission check
    `_require_allocation_permission_for_range` iterates `_iter_months(start, end)`
    for per-finance-month scope checks, so the same far-future `effective_month_end`
    drives ~95k in-memory authorization iterations (no DB rows/locks, but a CPU
    cost) for a globally-authorized caller. Shares the root cause of the finance
    materialization fix above. Recommended fix: short-circuit when the caller
    holds the global allocation grant (which already authorizes every covered
    month), and/or cap the accepted effective range at the propose/validation
    boundary. Authorization-layer change → carries authz-test obligations; kept
    out of this review-cleanup PR. File:
    `backend/ums_smart_revenue/api/channel_account_links.py`
    (`_require_allocation_permission_for_range`, `_iter_months`).
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
    (`upsert_owner_channel_links_from_source`).
- ⏳ Allocation engine (Spec 2b) — remaining: not started; consumes the verified map.
- ✅ Month lock/unlock — shipped: POST /finance-close/{month}/lock + /unlock
  (readiness-gated, audited MONTH_LOCKED/MONTH_UNLOCKED, fail-closed
  permissions). Month-close status UI remains unbuilt (Phase 5).
- ✅ Manual override approval — shipped: POST /revenue/manual-overrides +
  /manual-overrides/{id}/approve (create + approve flow, locked-month guard,
  APPROVE_MANUAL_OVERRIDE scope, audited).
- ⏳ Audit dashboard — remaining: tenant-scoped audit log backend
  (PR #22); dashboard UI not built.

## P2 — Advanced features

- ⏳ Display-only currency conversion foundation — remaining:
  official finance ingestion must first preserve Google/YouTube/AdSense
  reported amounts and currencies per `Docs/18`. Public/provider FX rates are
  not an official source for monthly revenue, tax, deduction, AdSense payment,
  or reconciliation values.
- ⏳ Anomaly detection foundation for source-backed month-over-month
  revenue movement — remaining: not started.
- ⏳ Detailed Shorts revenue handling foundation — remaining: not started.
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
- ✅ CI gate (Elite-CI vendored) + Dependabot (PRs #14, #17).
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
- ⏳ Google source-reported revenue ingestion foundation: `currencies`
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
  Remaining: live OAuth/API connector
  (B2), FX/conversion (B3). Marked ⏳ (not ✅) per the scaffolding-only honesty
  rule above — no real ingestion path yet.
- ⏳ Google source-rows -> revenue facts normalization bridge: pure
  `select_canonical_row()` rule per `source_system`, USD-only writes,
  upfront locked-month gate via `get_or_create_month_close_row(..., for_update=True)`,
  read-before-write CREATED/UPDATED/UNCHANGED classification — PR #44.
  Bridges PR #43's `google_revenue_source_rows` substrate to the existing
  `MonthlyChannelRevenueFactORM` via
  `SqlAlchemyRevenueFactRepository.record_fact()`. No schema delta, no new
  exception classes, no Alembic migration. 26 named SQLite tests + 5-test
  PostgreSQL companion (verifies the real `pg_advisory_xact_lock` + `SELECT
  ... FOR UPDATE` lock path executes against a live engine). Remaining: live
  OAuth/API connector (B2), FX/conversion (B3). Marked ⏳ (not ✅) per
  scaffolding-only honesty rule — no live data source yet. See
  `Docs/superpowers/specs/2026-05-25-spec-c1-google-source-normalizer-design.md`
  and `Docs/superpowers/plans/2026-05-25-spec-c1-google-source-normalizer.md`.

## Hard problems to solve early

1. ⏳ Revenue source for 70 outside-CMS channels — still open; not solved
   by any shipped PR.
2. ⏳ System-managed report availability and retention — remaining: raw
   report file ORM + repo (PR #32); ingestion + retention policy not
   built.
3. ⏳ Payment gap explanation — remaining: gap value + comparison ARE computed
   (payment_gap_usd via /revenue/months/{month}/payment-match, bank variance
   via /bank-reconciliation, high-gap smart alerts); remaining is a dedicated
   reconciling explanation/narrative pass tying gaps to receipts/fees/currency
   effects.
4. ⏳ Bank/transfer/local-currency variance evidence — remaining: bank recon
   repo (PR #29) is the substrate; variance explanations must reconcile
   Google/AdSense reported money, bank receipts, transfer fees, and bank-side
   currency effects without treating public FX rates as official revenue.
5. ⏳ Confidence labels that finance trusts — remaining: computation rules
   exist (net-revenue B_RECONCILED/D_ESTIMATED/E_MISSING + explain confidence
   label); remaining is finance-trusted surfacing in the dashboard UI.
6. ✅ Flexible grouping without hardcoded UMS structure — channel group
   registry (PR #25 + tests in PR #30).
