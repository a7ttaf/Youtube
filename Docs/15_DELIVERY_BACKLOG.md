# Delivery Backlog

## Status (2026-06-05)

Reconciled through PR #71 (31a7641). Marker conventions
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
    both paths take) — a close-path change outside this PR's scope that also needs
    owner approval as a finance-close refactor. Risk bounded: no production
    consumer of `list_verified_adsense_account_channels` until Spec 2b; both
    verify/reject and close are dual-gated admin actions. File:
    `backend/ums_smart_revenue/finance/channel_account_links.py`
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
  403 → no-permission copy. Renders the FIRST page only (no Load More via
  `next_cursor` yet). The severity-filter and "Download Audit View" controls are
  disabled placeholders (no facet / no audit-export route exists yet). Summary
  tiles + coverage panel stay static context (no aggregate-count route).
  Frontend-only; backend unchanged.

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
- ✅ June-14 MVP dashboard wiring: six screens wired to live APIs
  (Command Center + smart-alerts problem panel, Close, Trace/Explain,
  Exports, Connectors; Registry/Audit stay mock-labelled), demo-month seed
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
  Registry stays mock-only; live ingestion needs real connector credentials.
- ✅ Production Audit view wiring — merged to main (PR #71, 31a7641): the
  dashboard Audit page now reads the real `GET /audit/events` feed (was mock
  `AUDIT_EVENTS`). Distinct cursor-pagination types
  (`AuditLogEntry`/`AuditEventCursor`/`AuditEventPagination`), a memoized
  `useAuditEvents` hook (one self-auditing fetch per mount, no loop), and an
  extracted `views/AuditView.tsx`. Registry is now the only mock-labelled page.
- ✅ Connector credential test-connection probe — `POST /connectors/credentials/{connector_key}/{account_id}/test`
  (branch `docs/plan-hygiene-post-71`): wraps `resolve_connector_credentials()` (load
  credential row → resolve secret URI → OAuth token refresh, no live data pull).
  Surfaces `CredentialNotFoundError` as 404; `InactiveCredentialError` / `OAuthRefreshError` /
  other `GoogleConnectorError` as 200 with machine-readable `status` field (`inactive_credential` /
  `auth_failed` / `error`) and a string `detail`. Every probe is audited (`CONNECTOR_TESTED`
  event, `MANAGE_CONNECTORS@connector(connector_key)` gate, reason required).
  5 TDD tests (ok, not-found, inactive, oauth-error, 403). Backend only; no migration.
  Merged to main as PR #72 (28da1a6).
- ✅ Channel Registry Phase 1 wiring — merged to main as PR #73 (56bf9a8): the
  Registry table is wired to `GET /channels` (replacing the `REGISTRY_ROWS`
  mock). All display fields derived client-side (avatar, CMS badge, source
  label, state per Option A, trace key). Extracted to `views/RegistryView.tsx`;
  16 new Vitest tests. All six dashboard pages off mock data.
- ✅ Channel Registry Phase 2 — on `feat/registry-phase2`: `GET /org-units`
  (read-only, tenant-scoped, active-only, fail-closed VIEW_ANALYTICS; no
  migration) resolves Company/Sector display names with an honest raw-id
  fallback and supplies the Map modal's company options. Live write paths:
  Map → `PATCH /channels/{id}/mapping` (audited reason, in-flight latch,
  reload-on-success, typed inline errors incl. the unmapped-channel
  global-grant dead-zone); Assign → `POST /revenue/channel-account-links`
  (UNVERIFIED OPERATOR_ASSERTED proposal; verify/reject stays the admin API
  flow); Review → Trace navigation preselected on the channel. Backend +5 TDD
  org-units tests; frontend 189 Vitest green (10 new RegistryView + 5 hook).
  Remaining (definition-blocked): bulk inventory import format; "Scoped
  changes" tile; mapping-route month-lock enforcement (pre-existing gap,
  named follow-up).
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
   label) and PR #69 surfaces the explain confidence label + score in the
   Trace/Explain screen; remaining is finance adoption/validation of those
   labels across the rest of the dashboard.
6. ✅ Flexible grouping without hardcoded UMS structure — channel group
   registry (PR #25 + tests in PR #30).
