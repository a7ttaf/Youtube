export type TenantRead = {
  id: string;
  slug: string;
  display_name: string;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend net-revenue JSON contract. All
//   money values are serialized as STRINGS by the backend (decimal_to_api) and
//   may be null when a value is unknown — the UI formats them for display and
//   never performs float math on them. Fields are matched 1:1 against the
//   backend serializers, not guessed.
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/finance/net_revenue.py
//       MonthNetRevenueSummary.to_api()  (lines 103-130) -> NetRevenueResponse
//       ChannelNetRevenueSummary.to_api() (lines 46-81)  -> ChannelNetRevenue
//   - File: backend/ums_smart_revenue/finance/account_allocation_read.py
//       allocation_provenance_to_api()    (lines 178-189) -> allocation_source/committed_run
//   - File: backend/ums_smart_revenue/api/revenue.py
//       get_month_net_revenue()           (lines 1088-1225) -> adds currency + audit_events
//       audit_record_to_api()             (lines 1850-1860) -> AuditEvent
// ============================================================================

// Decimal money values arrive as strings from the backend (decimal_to_api).
// A field that may be absent serializes as null.
export type MoneyString = string;

export type AllocationSource =
  | "committed_snapshot"
  | "live_compute"
  | "live_fallback";

// Present (non-null) only when allocation_source === "committed_snapshot".
export type CommittedRun = {
  commit_version: number | null;
  committed_at: string | null; // ISO-8601 datetime
  run_id: string | null;
};

export type NetRevenueAuditEvent = {
  event_type: string;
  entity_type: string;
  entity_id: string;
  scope_type: string;
  scope_id: string | null;
  reason: string | null;
  sensitive: boolean;
};

// One channel row inside the monthly net-revenue summary.
// Source: ChannelNetRevenueSummary.to_api() (net_revenue.py:46-81).
export type ChannelNetRevenue = {
  month: string;
  youtube_channel_id: string;
  status: string;
  primary_source_kind: string | null;
  baseline_gross_revenue_usd: MoneyString;
  baseline_net_revenue_usd: MoneyString | null;
  approved_manual_override_total_usd: MoneyString;
  adjusted_gross_revenue_usd: MoneyString;
  net_revenue_usd: MoneyString | null;
  deduction_amount_usd: MoneyString | null;
  channel_direct_deduction_amount_usd: MoneyString | null;
  account_allocated_deduction_amount_usd: MoneyString | null;
  deduction_percentage: MoneyString | null;
  confidence: string;
  approved_manual_override_count: number;
  pending_manual_override_count: number;
  issues: Array<Record<string, string>>;
};

// GET /revenue/months/{month}/net-revenue
// Source: MonthNetRevenueSummary.to_api() (net_revenue.py:103-130) merged with
// the route-level additions currency + allocation_source/committed_run +
// audit_events (revenue.py:1192-1224).
export type NetRevenueResponse = {
  month: string;
  status: string;
  channel_count: number;
  calculated_channel_count: number;
  missing_net_source_count: number;
  pending_manual_override_count: number;
  total_adjusted_gross_revenue_usd: MoneyString;
  total_net_revenue_usd: MoneyString;
  total_deduction_amount_usd: MoneyString;
  total_channel_direct_deduction_amount_usd: MoneyString;
  total_account_allocated_deduction_amount_usd: MoneyString;
  // Global scope only; null for scoped reads.
  unallocated_account_deduction_total_usd: MoneyString | null;
  unallocated_account_issues: Array<Record<string, string>> | null;
  channels: ChannelNetRevenue[];
  // Route-level additions (revenue.py:1192-1224):
  currency: string;
  allocation_source: AllocationSource;
  committed_run: CommittedRun | null;
  audit_events: NetRevenueAuditEvent[];
};

// ============================================================================
// Purpose: TypeScript mirror of the backend finance month-close JSON contract
//   consumed by the Month-Close screen. Fields are matched 1:1 against the
//   backend serializers (not guessed); nullable fields serialize as null.
// Standards: Read-only typed boundary at the API surface; no logic here. The
//   lock/unlock POST responses reuse FinanceMonthCloseStatus and additionally
//   carry an `audit_event` object (kept loosely typed — the screen does not
//   render it).
// Connections:
//   - File: backend/ums_smart_revenue/finance/month_close.py
//       FinanceMonthCloseEntry.to_api()        (lines 31-42) -> FinanceMonthCloseStatus
//   - File: backend/ums_smart_revenue/finance/month_close_readiness.py
//       FinanceCloseReadiness.to_api()          (lines 56-62) -> FinanceCloseReadinessResponse
//       FinanceCloseBlocker.to_api()            (lines 34-41) -> FinanceCloseBlocker
//       FinanceCloseReadiness.to_lock_error_detail() (lines 64-69) -> FinanceCloseLockErrorDetail
//   - File: backend/ums_smart_revenue/api/finance_close.py
//       get_finance_month_close()               (lines 75-98)   -> GET /finance-close/{month}
//       get_finance_close_readiness()           (lines 101-129) -> GET /finance-close/{month}/readiness
//       lock_finance_month()/unlock_finance_month() (lines 132-194) -> POST lock/unlock (+ audit_event)
// ============================================================================

// GET /finance-close/{month}
// Source: FinanceMonthCloseEntry.to_api() (month_close.py:31-42).
export type FinanceMonthCloseStatus = {
  month: string;
  status: string; // "OPEN" | "LOCKED"
  allocation_method: string | null;
  allocation_rule_payload: Record<string, unknown>;
  locked_by: string | null;
  locked_at: string | null; // ISO-8601 datetime
  unlocked_by: string | null;
  unlocked_at: string | null; // ISO-8601 datetime
};

// One unresolved condition blocking a month close.
// Source: FinanceCloseBlocker.to_api() (month_close_readiness.py:34-41).
export type FinanceCloseBlocker = {
  blocker_type: string;
  severity: string;
  count: number;
  message: string;
};

// GET /finance-close/{month}/readiness
// Source: FinanceCloseReadiness.to_api() (month_close_readiness.py:56-62).
export type FinanceCloseReadinessResponse = {
  month: string;
  ready: boolean;
  blockers: FinanceCloseBlocker[];
};

// HTTP 409 detail returned by POST /finance-close/{month}/lock when blockers
// remain. Source: FinanceCloseReadiness.to_lock_error_detail() (lines 64-69).
export type FinanceCloseLockErrorDetail = {
  message: string;
  blockers: FinanceCloseBlocker[];
};

// POST /finance-close/{month}/lock and /unlock response: the close status plus
// the recorded audit event. Source: _with_audit_event() (finance_close.py:313-316).
export type FinanceMonthCloseMutationResponse = FinanceMonthCloseStatus & {
  audit_event: Record<string, unknown>;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend number-explanation JSON contract
//   consumed by the Trace / Explain-Number screen. Fields are matched 1:1
//   against NumberExplanationEntry.to_api() and the route-level audit additions
//   (not guessed); money values stay STRINGS (decimal_to_api) and nullable
//   fields serialize as null. Component shapes differ by metric: the gross
//   metric emits baseline + override rows; the net metric emits those plus a
//   deduction breakdown (channel-direct + account-allocated, OR a single
//   source-reported deduction row) — so component fields beyond {key,label,
//   value} are modelled as optional and read defensively by the view.
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/finance/explanations.py
//       NumberExplanationEntry.to_api()              (lines 71-84)   -> NumberExplanation
//       SUPPORTED_METRICS                            (lines 29-31)   -> ExplanationMetric
//       build_channel_month_revenue_explanation()    (lines 144-232) -> gross components
//       _build_net_revenue_explanation()             (lines 235-400) -> net components
//   - File: backend/ums_smart_revenue/api/revenue.py
//       explain_channel_month_revenue_metric()       (lines 1358-1510) -> POST endpoint;
//         adds audit_event (gross) or audit_events[] (net) to to_api().
//       audit_record_to_api()                        (lines 1850-1860) -> NetRevenueAuditEvent
// ============================================================================

// The two metrics the explain endpoint accepts (SUPPORTED_METRICS). The query
// param defaults to adjusted_gross_revenue_usd on the backend.
export type ExplanationMetric =
  | "adjusted_gross_revenue_usd"
  | "net_revenue_usd";

// Confidence block: {label: "HIGH"|"MEDIUM"|"LOW", score: decimal-as-string}.
// Source: _confidence()/map_net_confidence() (explanations.py:34-53, 413-430).
export type ExplanationConfidence = {
  label: string;
  score: string;
};

// One nested channel-direct deduction inside the net "channel_direct_deduction_usd"
// component. Source: _build_net_revenue_explanation() (explanations.py:320-328).
export type ExplanationDirectDeduction = {
  component_kind: string;
  source_system: string;
  component_key: string;
  amount_usd: MoneyString;
};

// One nested account-allocated deduction line inside the net
// "account_allocated_deduction_usd" component.
// Source: _build_net_revenue_explanation() (explanations.py:335-346).
export type ExplanationAllocatedDeduction = {
  adsense_account_id: string;
  component_kind: string;
  source_system: string;
  component_key: string;
  basis_source_kind: string | null;
  basis_share: MoneyString;
  allocated_amount_usd: MoneyString;
};

// One explanation component row. {key,label,value} are always present; the
// remaining fields appear only on the component that carries them (gross
// baseline source attribution, override/deduction counts, the net deduction
// breakdown arrays, and the optional committed-allocation provenance merged in
// via allocation_provenance_to_api). Read defensively in the view.
export type ExplanationComponent = {
  key: string;
  label: string;
  value: MoneyString;
  // Gross baseline + net source-reported deduction rows.
  source_kind?: string | null;
  source_report_id?: string | null;
  // Override + deduction breakdown row counts.
  count?: number;
  // Net channel-direct deduction breakdown.
  components?: ExplanationDirectDeduction[];
  // Net account-allocated deduction breakdown.
  allocations?: ExplanationAllocatedDeduction[];
  // Optional committed-allocation provenance (merged onto the account-allocated
  // component only). Shape mirrors allocation_provenance_to_api().
  allocation_source?: AllocationSource;
  committed_run?: CommittedRun | null;
};

// One explanation warning (pending overrides, missing facts, net issues, …).
// Source: warnings list in both builders (e.g. explanations.py:198-219, 376-387).
export type ExplanationWarning = {
  code: string;
  message: string;
};

// POST /revenue/channels/{channel_id}/months/{month}/explain?metric={metric}
// Source: NumberExplanationEntry.to_api() (explanations.py:71-84) plus the
// route-level audit addition (revenue.py:1492-1497 net / 1508-1509 gross).
export type NumberExplanation = {
  month: string;
  entity_type: string; // "channel"
  entity_id: string; // youtube_channel_id
  metric: string;
  value: MoneyString;
  currency: string;
  formula: string;
  confidence: ExplanationConfidence;
  components: ExplanationComponent[];
  warnings: ExplanationWarning[];
  // Route-level additions: the gross path returns a single audit_event; the net
  // path returns audit_events[] (REVENUE_VIEWED + PAYMENT_VIEWED). The screen
  // does not render these but keeps them typed for completeness.
  audit_event?: NetRevenueAuditEvent;
  audit_events?: NetRevenueAuditEvent[];
};

// ============================================================================
// Purpose: TypeScript mirror of the backend export-job JSON contract consumed by
//   the Exports screen. Fields are matched 1:1 against the backend serializers
//   (not guessed); nullable fields serialize as null. An export is requested
//   (POST), tracked through its lifecycle (QUEUED -> COMPLETED | FAILED |
//   CANCELLED), and — once COMPLETED — its artifact is downloaded over a plain
//   browser anchor (binary, NOT through the JSON-strict useApiClient).
// Standards: Read-only typed boundary at the API surface; no logic here. Money
//   is not a concern on these shapes (jobs carry currency code + month, not
//   amounts); artifact_byte_size is an integer.
// Connections:
//   - File: backend/ums_smart_revenue/reports/exports.py
//       ExportJobEntry.to_api()        (lines 59-87)   -> ExportJob
//       ALLOWED_EXPORT_TYPES           (lines 16-20)   -> ExportType
//       request_export() status="QUEUED" (line 175)    -> initial status
//   - File: backend/ums_smart_revenue/api/exports.py
//       request_export()               (lines 173-284) -> POST /exports (202) + audit_event
//       list_exports()                 (lines 287-324) -> GET /exports {items, pagination}
//       get_export()                   (lines 327-396) -> GET /exports/{id} + audit_event
//       download_finance_workbook()    (line 479)      -> /exports/{id}/finance-workbook.xlsx
//       download_executive_pdf()       (line 593)      -> /exports/{id}/executive.pdf
//       download_branded_slide_pack()  (line 715)      -> /exports/{id}/branded-slide-pack.pptx
//       preview_finance_workbook()     (line 399)      -> /exports/{id}/finance-workbook-preview
// ============================================================================

// The four accepted export_type enum values (ALLOWED_EXPORT_TYPES). The first
// three are finance exports (EXPORT_REVENUE_REPORT); the CSV is analytics.
export type ExportType =
  | "FINANCE_EXCEL"
  | "EXECUTIVE_PDF"
  | "BRANDED_SLIDE_PACK"
  | "ANALYTICS_SUMMARY_CSV";

// Lifecycle status of an export job. Created as QUEUED; terminal states are
// COMPLETED (artifact ready to download), FAILED, and CANCELLED.
// Source: request_export() status="QUEUED"; _TERMINAL_EXPORT_JOB_STATUSES.
export type ExportJobStatus =
  | "QUEUED"
  | "COMPLETED"
  | "FAILED"
  | "CANCELLED";

// The accepted scope_type values (ALLOWED_EXPORT_SCOPE_TYPES, exports.py:21-23).
export type ExportScopeType =
  | "global"
  | "sector"
  | "company"
  | "channel"
  | "group";

// One export-job row, returned by POST /exports (202), GET /exports items[], and
// GET /exports/{id}. Source: ExportJobEntry.to_api() (exports.py:59-87).
export type ExportJob = {
  id: string;
  export_type: string;
  scope_type: string;
  scope_id: string | null;
  scope_channel_ids: string[] | null;
  month: string;
  currency: string;
  requested_by: string;
  status: string;
  file_url: string | null;
  artifact_filename: string | null;
  artifact_content_type: string | null;
  artifact_byte_size: number | null;
  artifact_checksum_sha256: string | null;
  failure_reason: string | null;
  month_lock_status: string;
  include_confidence_notes: boolean;
  include_manual_override_notes: boolean;
  created_at: string; // ISO-8601 datetime
  completed_at: string | null; // ISO-8601 datetime
};

// POST /exports response: the created job plus the recorded audit event.
// Source: request_export() response = export_job.to_api(); response["audit_event"].
export type ExportJobCreated = ExportJob & {
  audit_event: Record<string, unknown>;
};

// GET /exports response. Source: list_exports() (exports.py:316-324).
export type ExportListResponse = {
  items: ExportJob[];
  pagination: {
    limit: number;
    offset: number;
    returned: number;
    has_more: boolean;
  };
};

// POST /exports request body. Source: ExportRequest (exports.py:124-158).
// reason is REQUIRED (min_length=1) — it is recorded on the EXPORT_CREATED audit
// event. include_* default to true on the backend and the UI sends both.
export type ExportRequestBody = {
  export_type: ExportType;
  scope_type: ExportScopeType;
  scope_id: string | null;
  month: string;
  currency: string;
  reason: string;
  include_confidence_notes: boolean;
  include_manual_override_notes: boolean;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend connector + AdSense data-source JSON
//   contracts consumed by the Connectors screen. Fields are matched 1:1 against
//   the backend serializers (not guessed); nullable fields serialize as null and
//   money values stay STRINGS (decimal_to_api). The screen reads the configured
//   connector credentials and the synced AdSense payments, and triggers two
//   write actions (request a connector job, sync AdSense payments) — all via the
//   backend's own guarded, audited routes (no client-side authorization).
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/connectors/credentials.py
//       ConnectorCredentialEntry.to_api()  (lines 35-42) -> ConnectorCredential
//   - File: backend/ums_smart_revenue/api/connectors.py
//       list_connector_credentials()       (lines 58-81) -> ConnectorCredentialListResponse
//       request_connector_job()            (lines 122-144) -> ConnectorJobRequestBody / ConnectorJobResponse
//   - File: backend/ums_smart_revenue/finance/adsense_payments.py
//       AdSensePaymentEntry.to_api()       (lines 60-74) -> AdsensePayment
//   - File: backend/ums_smart_revenue/api/adsense.py
//       list_adsense_payments()            (lines 204-264) -> AdsensePaymentListResponse
//       sync_adsense_payments()            (lines 133-201) -> AdsenseSyncRequestBody / AdsenseSyncResponse
// ============================================================================

// Shared pagination envelope returned by the connector + AdSense list routes
// (same {limit, offset, returned, has_more} shape as the export list).
export type PaginationMeta = {
  limit: number;
  offset: number;
  returned: number;
  has_more: boolean;
};

// One configured connector credential row ("data source").
// Source: ConnectorCredentialEntry.to_api() (credentials.py:35-42). The secret
// itself is NEVER serialized — only a has_secret_ref boolean is exposed.
export type ConnectorCredential = {
  id: string;
  connector_key: string;
  account_id: string;
  status: string;
  has_secret_ref: boolean;
};

// GET /connectors/credentials. Source: list_connector_credentials() (connectors.py:73-81).
export type ConnectorCredentialListResponse = {
  items: ConnectorCredential[];
  pagination: PaginationMeta;
};

// POST /connectors/jobs request body. Source: ConnectorJobRequest (connectors.py:46-49).
// reason is REQUIRED (min_length=1) — recorded on the CONNECTOR_JOB_RUN audit event.
export type ConnectorJobRequestBody = {
  connector_key: string;
  account_id: string;
  reason: string;
};

// POST /connectors/jobs response (202). Source: request_connector_job() (connectors.py:139-144).
// execution_status is "recorded_not_executed": the request is audited but the
// connector run is NOT yet executed (no execution backend wired today).
export type ConnectorJobResponse = {
  connector_key: string;
  account_id: string;
  execution_status: string;
  audit_event: Record<string, unknown>;
};

// One synced AdSense payment row. Source: AdSensePaymentEntry.to_api()
// (adsense_payments.py:60-74). payment_amount is a decimal-as-STRING; payment_date
// is an ISO date; raw_payload is the connector's opaque source payload.
export type AdsensePayment = {
  id: string;
  source_account_id: string;
  month: string;
  payment_name: string;
  payment_date: string; // ISO-8601 date
  payment_amount: MoneyString;
  payment_currency: string;
  payment_status: string; // PAID | PENDING | UNPAID | CANCELLED
  raw_payload: Record<string, unknown>;
  source_report_id: string | null;
  imported_by: string | null;
};

// GET /adsense/payments. Source: list_adsense_payments() (adsense.py:255-264).
// Carries the reused PAYMENT_VIEWED audit_event (kept loosely typed; not rendered).
export type AdsensePaymentListResponse = {
  items: AdsensePayment[];
  pagination: PaginationMeta;
  audit_event: Record<string, unknown>;
};

// POST /adsense/sync-payments request body. Source: AdSensePaymentSyncRequest
// (adsense.py:100-123). The Connectors screen surfaces only the AdSense payment
// LIST + a re-sync trigger; it does not author new payment rows, so it sends an
// empty connector-scoped resync intent? No — the backend requires >=1 payment.
// This type is the full shape so a future "add payment" form can reuse it.
export type AdsenseSyncRequestBody = {
  connector_key: string;
  source_report_id: string | null;
  reason: string;
  payments: Array<{
    source_account_id: string;
    month: string;
    payment_name: string;
    payment_date: string; // ISO-8601 date
    payment_amount: MoneyString;
    payment_currency: string;
    payment_status: string;
    raw_payload: Record<string, unknown>;
  }>;
};

// POST /adsense/sync-payments response. Source: sync_adsense_payments() (adsense.py:197-201).
export type AdsenseSyncResponse = {
  synced_count: number;
  items: AdsensePayment[];
  audit_event: Record<string, unknown>;
};
