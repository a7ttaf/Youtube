export type TenantRead = {
  id: string;
  slug: string;
  display_name: string;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /session/me JSON contract. The
//   SPA hydrates the authenticated principal's identity + backend-DERIVED
//   capability booleans from this endpoint and gates every UI surface on them,
//   so the browser never guesses authorization from a role string. Fields are
//   matched 1:1 against the backend serializers (not guessed); nullable fields
//   serialize as null. Capability keys are camelCase on the wire (the backend
//   SessionCapabilities model uses an alias generator).
// Standards: Read-only typed boundary at the API surface; no logic here. The
//   capabilities are AUTHORITATIVE — the UI must never fabricate a capability
//   the session did not grant.
// Connections:
//   - File: backend/ums_smart_revenue/api/session.py
//       SessionTenant            (lines 21-26) -> SessionTenantRef
//       SessionScopeAssignment   (lines 29-34) -> SessionRoleAssignment
//       SessionPermissionGrant   (lines 37-42) -> SessionPermissionGrant
//       SessionCapabilities      (lines 45-64) -> SessionCapabilities
//       SessionMe                (lines 67-77) -> SessionMe
//       get_current_session_endpoint (lines 163-199) -> GET /session/me
//   - File: frontend/src/contexts/SessionContext.tsx -> hydrates from this shape.
// ============================================================================

// The resolved tenant context on the session, or null when unresolved.
// Source: SessionTenant (session.py:21-26).
export type SessionTenantRef = {
  id: string;
  slug: string;
  display_name: string;
};

// One active role assignment flattened for the SPA.
// Source: SessionScopeAssignment (session.py:29-34).
export type SessionRoleAssignment = {
  role: string;
  scope_type: string;
  scope_id: string | null;
};

// One active direct permission grant flattened for the SPA.
// Source: SessionPermissionGrant (session.py:37-42).
export type SessionPermissionGrant = {
  permission: string;
  scope_type: string;
  scope_id: string | null;
};

// Derived global-scope capability booleans the SPA uses to render UI. Every key
// is camelCase (backend alias generator). These are AUTHORITATIVE — the UI gates
// every surface on them and never fabricates one the backend did not grant.
// Source: SessionCapabilities (session.py:45-64).
export type SessionCapabilities = {
  canViewRevenue: boolean;
  canViewConfidence: boolean;
  canViewPayments: boolean;
  canViewBankReconciliation: boolean;
  canCloseMonth: boolean;
  canUnlockMonth: boolean;
  canChangeAllocation: boolean;
  canExportRevenue: boolean;
  canExportAnalyticsReports: boolean;
  canManageRegistry: boolean;
  canManageConnectors: boolean;
  canViewConnectorHealth: boolean;
  canRunConnectorJobs: boolean;
  canViewAudit: boolean;
  // True when the principal holds ANY active VIEW_ANALYTICS grant (direct or via
  // role) at ANY scope — scope-aware, not global-only — so a legitimately
  // company-scoped analytics user still sees the analytics surface. Fail-closed
  // (disabled -> false). Source: SessionCapabilities.can_view_analytics.
  canViewAnalytics: boolean;
};

// GET /session/me — the authenticated principal's identity, optional tenant,
// active roles/permissions, service-account/disabled flags, and the derived
// capabilities. Source: SessionMe (session.py:67-77).
export type SessionMe = {
  user_id: string;
  email: string;
  tenant: SessionTenantRef | null;
  roles: SessionRoleAssignment[];
  permissions: SessionPermissionGrant[];
  is_service_account: boolean;
  disabled: boolean;
  capabilities: SessionCapabilities;
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
//   CANCELLED), and its artifact is downloaded over a plain browser anchor
//   (binary, NOT through the JSON-strict useApiClient). The download routes
//   generate-on-demand: a QUEUED job builds + persists + streams its bytes on
//   first request, so QUEUED and COMPLETED jobs are both downloadable (QUEUED
//   triggers generation; COMPLETED serves the cached artifact).
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

// Lifecycle status of an export job. Created as QUEUED (downloadable: the GET
// route generates the artifact on demand); terminal states are COMPLETED
// (cached artifact ready to download), FAILED, and CANCELLED.
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
// Source: ConnectorCredentialEntry.to_api() (credentials.py:40-59). The secret
// itself is NEVER serialized — only a has_secret_ref boolean is exposed.
// The four refresh-telemetry columns are OPTIONAL (null until the OAuth refresh
// path records them) and are surfaced together by the credential-HEALTH route
// (GET /connectors/credentials/health); the MANAGE-gated credential LIST route
// returns the same shape. ISO-8601 timestamps; nullable fields serialize as null.
export type ConnectorCredential = {
  id: string;
  connector_key: string;
  account_id: string;
  status: string;
  has_secret_ref: boolean;
  // OAuth refresh telemetry (credentials.py to_api()); null until a refresh runs.
  last_refresh_attempt_at?: string | null; // ISO-8601
  token_expiry_at?: string | null; // ISO-8601
  last_refresh_status?: string | null;
  last_refresh_error_class?: string | null;
};

// Coarse, server-derived credential health label. Source: the fixed literal set
// returned by derive_credential_health_state() (credentials.py). Read-only — the
// UI never derives or overrides it (no client-side finance/auth logic).
export type ConnectorCredentialHealthState =
  | "healthy"
  | "expiring"
  | "auth_failed"
  | "missing"
  | "unknown";

// One credential-health row: the ConnectorCredential telemetry fields plus the
// server-derived health_state. Source: list_connector_credential_health()
// (connectors.py) appends health_state to each credential's to_api() shape.
export type ConnectorCredentialHealth = ConnectorCredential & {
  health_state: ConnectorCredentialHealthState;
};

// GET /connectors/credentials/health. VIEW_CONNECTOR_HEALTH-gated (a 403 surfaces
// as the typed ApiError for the view to translate). Connector-scoped callers are
// narrowed server-side to their granted connector ids (no foreign-credential
// leak). Source: list_connector_credential_health() (connectors.py).
export type ConnectorCredentialHealthResponse = {
  credentials: ConnectorCredentialHealth[];
  pagination: PaginationMeta;
};

// GET /connectors/credentials. Source: list_connector_credentials() (connectors.py:73-81).
export type ConnectorCredentialListResponse = {
  items: ConnectorCredential[];
  pagination: PaginationMeta;
};

// POST /connectors/jobs request body. Source: ConnectorJobRequest (connectors.py:46-49).
// reason is REQUIRED (min_length=1) — recorded on the CONNECTOR_JOB_RUN audit event.
// report_month is REQUIRED on the executing path (the month the pull targets);
// dry_run (default false) runs a validate-only pass that writes no facts.
export type ConnectorJobRequestBody = {
  connector_key: string;
  account_id: string;
  report_month: string;
  dry_run?: boolean;
  reason: string;
};

// POST /connectors/jobs response (202). Source: request_connector_job() (connectors.py).
// execution_status is "submitted" on the executing path (the request is audited AND
// handed to the executor). A disabled executor returns 503 (not a 202), surfaced as an
// error. dry_run echoes the request flag so the success banner can branch its copy.
// The `string` type stays forward-compatible with future statuses.
export type ConnectorJobResponse = {
  connector_key: string;
  account_id: string;
  report_month: string;
  dry_run: boolean;
  execution_status: string;
  audit_event: Record<string, unknown>;
};

// ============================================================================
// Purpose: TypeScript mirror of the connector run-history read contract
//   (GET /connectors/runs) and the test-connection probe result
//   (POST /connectors/credentials/{key}/{account}/test). Connector run rows are
//   operational metadata only — NOT finance numbers. Counts are integers; the
//   cursor/pagination shape mirrors the audit log's cursor pagination
//   (both-or-neither {started_at, id}), distinct from the offset PaginationMeta.
// Standards: Read-only typed boundary at the API surface; no logic here. Fields
//   are matched 1:1 against the backend serializers (ConnectorRunEntry.to_api()
//   and the test-connection route), not guessed.
// Connections:
//   - File: backend/ums_smart_revenue/connectors/runs/repository.py
//       ConnectorRunEntry.to_api() -> ConnectorRun
//   - File: backend/ums_smart_revenue/api/connectors.py
//       list_connector_runs() -> ConnectorRunListResponse
//       test_connector_connection() -> ConnectorTestResult
// ============================================================================

// Per-run upsert/report counters. All integers (no float / money math here).
// Source: ConnectorRunEntry counts (connectors/runs/repository.py).
export type ConnectorRunCounts = {
  reports_attempted: number;
  reports_succeeded: number;
  reports_failed: number;
  rows_upserted_total: number;
  rows_upserted_created: number;
  rows_upserted_updated: number;
  rows_upserted_unchanged: number;
};

// One connector run-history row. status reflects the run lifecycle; timestamps
// are ISO-8601; error_summary is present only on a failed/partial run.
// Source: ConnectorRunEntry.to_api() (connectors/runs/repository.py).
export type ConnectorRun = {
  id: string;
  connector_key: string;
  account_id: string;
  report_month: string;
  triggered_by_user_id: string | null;
  started_at: string; // ISO-8601
  finished_at: string | null; // ISO-8601, null while RUNNING
  status: "RUNNING" | "SUCCEEDED" | "PARTIAL" | "FAILED";
  counts: ConnectorRunCounts;
  error_summary: string | null;
};

// Cursor pagination for /connectors/runs (both-or-neither {started_at, id}).
// Mirrors AuditEventCursor; distinct from the offset PaginationMeta.
// Source: ConnectorRunPage.next_cursor (connectors/runs/repository.py).
export type ConnectorRunCursor = { started_at: string; id: string };
export type ConnectorRunPagination = {
  limit: number;
  returned: number;
  has_more: boolean;
  next_cursor: ConnectorRunCursor | null;
};

// GET /connectors/runs. Source: list_connector_runs() (api/connectors.py).
export type ConnectorRunListResponse = {
  items: ConnectorRun[];
  pagination: ConnectorRunPagination;
};

// POST /connectors/credentials/{key}/{account}/test result. status drives the
// result badge tone. not_found arrives as HTTP 404 (mapped client-side); the
// other statuses arrive as HTTP 200. Source: test_connector_connection().
export type ConnectorTestStatus =
  | "ok"
  | "inactive_credential"
  | "auth_failed"
  | "error"
  | "not_found";
export type ConnectorTestResult = {
  connector_key: string;
  account_id: string;
  status: ConnectorTestStatus;
  detail: string;
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

// ============================================================================
// Purpose: TypeScript mirror of the backend monthly smart-alerts JSON contract
//   consumed by the Command Center problem panel. Fields are matched 1:1 against
//   the backend serializers (not guessed); nullable fields serialize as null.
//   The endpoint aggregates cross-domain finance health signals (payment match,
//   bank reconciliation, month lock, manual overrides, revenue trend) into a
//   prioritized alert list with an overall status + highest severity. The panel
//   is read-only and fails independently of the rest of the Command Center.
// Standards: Read-only typed boundary at the API surface; no logic here. The
//   alert `details` object is intentionally loosely typed (Record) because its
//   keys vary per alert code; the panel renders the typed fields and does not
//   depend on details shape.
// Connections:
//   - File: backend/ums_smart_revenue/finance/smart_alerts.py
//       MonthlySmartAlert.to_api()         (lines 30-39) -> SmartAlert
//       MonthlySmartAlertSummary.to_api()  (lines 51-59) -> SmartAlertsSummary
//   - File: backend/ums_smart_revenue/api/revenue.py
//       get_month_smart_alerts()           (lines 844-955) -> GET endpoint;
//         four-permission finance-month auth; adds audit_events[] to to_api().
//       audit_record_to_api()              (lines 1850-1860) -> NetRevenueAuditEvent
// ============================================================================

// Overall status of the smart-alert summary.
// Source: MonthlySmartAlertSummary.status ("ATTENTION_REQUIRED" if alerts else
// "CLEAR", smart_alerts.py:209).
export type SmartAlertStatus = "ATTENTION_REQUIRED" | "CLEAR";

// Severity of a single alert, ordered LOW < MEDIUM < HIGH (_SEVERITY_RANK).
// highest_severity is one of these or null when there are no alerts.
export type SmartAlertSeverity = "LOW" | "MEDIUM" | "HIGH";

// One smart alert row. Source: MonthlySmartAlert.to_api() (smart_alerts.py:30-39).
// `details` keys vary per code (e.g. payment_gap_usd, close_status, channels[]);
// money values inside details are decimal-as-STRINGS (decimal_to_api).
export type SmartAlert = {
  code: string;
  severity: SmartAlertSeverity;
  message: string;
  source: string;
  confidence: string;
  details: Record<string, unknown>;
};

// GET /revenue/months/{month}/smart-alerts
// Source: MonthlySmartAlertSummary.to_api() (smart_alerts.py:51-59) plus the
// route-level audit addition (revenue.py:950-954). audit_events[] is kept typed
// for completeness but not rendered by the panel.
export type SmartAlertsSummary = {
  month: string;
  status: SmartAlertStatus;
  highest_severity: SmartAlertSeverity | null;
  alert_count: number;
  alerts: SmartAlert[];
  audit_events?: NetRevenueAuditEvent[];
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /audit/events JSON contract
//   consumed by the Audit view. Fields are matched 1:1 against the backend
//   serializers (not guessed); nullable fields serialize as null. Redaction is
//   SERVER-DRIVEN: when details_redacted is true the backend has already replaced
//   details with {} — the UI renders the redacted state and NEVER attempts a
//   client-side reveal of a sensitive payload. Pagination is CURSOR-based, so it
//   uses a distinct type, NOT the offset PaginationMeta used by the other lists.
// Standards: Read-only typed boundary at the API surface; no logic here. No
//   client-side authorization is invented — the backend VIEW_AUDIT_LOG gate (and
//   the separate VIEW_SENSITIVE_AUDIT_PAYLOADS gate that drives redaction) is
//   authoritative.
// Connections:
//   - File: backend/ums_smart_revenue/auth/audit_log.py
//       AuditLogEntry.to_api()  (lines 35-52) -> AuditLogEntry
//       AuditLogPage             (lines 55-62) -> AuditEventPagination
//       _next_cursor()           (lines 207-215) -> AuditEventCursor
//   - File: backend/ums_smart_revenue/api/audit.py
//       list_audit_events()      (lines 85-97) -> AuditEventListResponse
// ============================================================================

// GET /audit/events item. Source: AuditLogEntry.to_api() (auth/audit_log.py:35-52).
// Distinct from NetRevenueAuditEvent (the route-echo sub-object). details is {} when
// details_redacted is true (server-driven redaction; never reveal client-side).
export type AuditLogEntry = {
  id: string;
  user_id: string | null;
  event_type: string;
  entity_type: string | null;
  entity_id: string | null;
  scope_type: string | null;
  scope_id: string | null;
  request_id: string | null;
  reason: string | null;
  details: Record<string, unknown>;
  details_redacted: boolean;
  sensitive: boolean;
  created_at: string; // ISO-8601
};

// Cursor pagination for /audit/events (NOT the offset PaginationMeta).
// Source: AuditLogPage + _next_cursor() (auth/audit_log.py:55-62, 207-215).
export type AuditEventCursor = { created_at: string; id: string };
export type AuditEventPagination = {
  limit: number;
  returned: number;
  has_more: boolean;
  next_cursor: AuditEventCursor | null;
};

// GET /audit/events. Source: list_audit_events() (api/audit.py:85-97).
// audit_event is the self-audit echo (loosely typed; not rendered).
export type AuditEventListResponse = {
  items: AuditLogEntry[];
  pagination: AuditEventPagination;
  audit_event: Record<string, unknown>;
};

// GET /audit/summary — tenant-scoped aggregate counts for the audit summary
// tiles. Counts only (no per-row payload), so it is redaction-safe behind the
// same VIEW_AUDIT_LOG gate as /audit/events and does NOT self-audit.
// All four fields are integers. Source: AuditSummaryResponse (api/audit.py:38-44)
// backed by AuditSummaryCounts (auth/audit_log.py:67-72).
export type AuditSummaryResponse = {
  total_events: number;
  sensitive_events: number;
  recent_count: number;
  window_hours: number;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /channels JSON contract consumed
//   by the Registry view. Fields are matched 1:1 against
//   ChannelRegistryEntry.to_api() (org/channel_registry.py:19-29). The view
//   enriches this data client-side: avatar initials, cms badge tone, source label,
//   state derivation (Option A: from existing fields, no migration), and trace key.
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/org/channel_registry.py
//       ChannelRegistryEntry.to_api() (lines 19-29) -> ChannelRegistryEntry
//   - File: backend/ums_smart_revenue/api/channels.py
//       list_channels() (lines 116-130) -> GET /channels
// ============================================================================

// GET /channels item. Source: ChannelRegistryEntry.to_api() (channel_registry.py:19-29).
export type ChannelRegistryEntry = {
  youtube_channel_id: string;
  channel_name: string;
  primary_company_id: string | null;
  // DB constraint (ck_youtube_channels_cms_status): "INSIDE_CMS" | "OUTSIDE_CMS" | "UNKNOWN"
  cms_status: string;
  content_owner_id: string | null;
  revenue_required: boolean;
  // DB constraint (ck_youtube_channels_revenue_source_status):
  // "OFFICIAL_CMS_REVENUE" | "OFFICIAL_MANUAL_IMPORT" | "ALLOCATED_FROM_PAYMENT_POOL"
  // | "PERFORMANCE_ONLY" | "MISSING_REVENUE_SOURCE"
  revenue_source_status: string;
  active: boolean;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /org-units JSON contract used by
//   the Registry view to resolve Company/Sector names from the org-unit ids
//   already carried on ChannelRegistryEntry. `primary_company_id` IS the
//   org-unit UUID, so a name is a direct id lookup; `parent_id` walks one hop
//   up (COMPANY -> SECTOR) for the Sector column.
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/org/org_units_read.py
//       OrgUnitEntry.to_api() -> GET /org-units item
//   - File: backend/ums_smart_revenue/api/org_units.py -> GET /org-units
// ============================================================================

// GET /org-units item. Source: OrgUnitEntry.to_api() (org/org_units_read.py).
export type OrgUnit = {
  id: string;
  parent_id: string | null;
  // org_units.type: "HOLDING" | "SECTOR" | "COMPANY"
  type: string;
  name: string;
  active: boolean;
};

// PATCH /channels/{id}/mapping response. The route returns the updated mapping
// plus the audit event it recorded; the view only needs the audit_event for the
// success confirmation, so the rest stays an opaque record.
export type ChannelMappingResponse = Record<string, unknown> & {
  audit_event: Record<string, unknown>;
};

// POST /revenue/channel-account-links (propose) response. Returns the created
// UNVERIFIED link row plus the audit event; both are opaque records the view
// surfaces as a "proposed" confirmation rather than parsing field-by-field.
export type AccountLinkProposalResponse = {
  link: Record<string, unknown>;
  audit_event: Record<string, unknown>;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /channels/outside-cms JSON
//   contract consumed by the Command Center monitor panel. The endpoint is
//   VIEW_ANALYTICS-gated and scope-filtered (visible channels only); it returns
//   {items, summary} with NO money and NO audit. Fields are matched 1:1 against
//   the backend serializer (not guessed); nullable fields serialize as null.
//   `missing_official_revenue` distinguishes an outside-CMS channel that still
//   has an official source (e.g. OFFICIAL_MANUAL_IMPORT) from one that is
//   genuinely missing its revenue source.
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/api/channels.py
//       _outside_cms_channel_to_api() (lines 230-249) -> OutsideCmsItem
//       list_outside_cms_channels()   (lines 135-163) -> OutsideCmsResponse
// ============================================================================

// One outside-CMS channel row. Source: _outside_cms_channel_to_api()
// (api/channels.py:230-249).
export type OutsideCmsItem = {
  youtube_channel_id: string;
  channel_name: string;
  primary_company_id: string | null;
  cms_status: string;
  content_owner_id: string | null;
  revenue_required: boolean;
  revenue_source_status: string;
  missing_official_revenue: boolean;
  recommended_action: string;
};

// GET /channels/outside-cms. Source: list_outside_cms_channels()
// (api/channels.py:152-163).
export type OutsideCmsResponse = {
  items: OutsideCmsItem[];
  summary: {
    outside_cms_channel_count: number;
    revenue_required_count: number;
    missing_official_revenue_count: number;
  };
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /channels/issues JSON contract
//   consumed by the Command Center monitor panel. The endpoint is
//   VIEW_ANALYTICS-gated and scope-filtered; it returns {items, summary} with NO
//   money and NO audit. Fields are matched 1:1 against the backend serializers
//   (not guessed); nullable fields serialize as null. `issue_type`/`severity`/
//   `recommended_action` are stable strings the panel renders directly.
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/org/channel_issues.py
//       ChannelRegistryIssue.to_api()        (lines 26-35) -> ChannelIssue
//       summarize_channel_registry_issues()  (lines 62-76) -> ChannelIssuesSummary
//   - File: backend/ums_smart_revenue/api/channels.py
//       list_channel_issues()                (lines 166-188) -> ChannelIssuesResponse
// ============================================================================

// One channel registry issue row. Source: ChannelRegistryIssue.to_api()
// (org/channel_issues.py:26-35). issue_type is one of MISSING_COMPANY |
// MISSING_SECTOR | OUTSIDE_CMS_REVENUE_REQUIRED | REVENUE_REQUIRED_NO_GROUP;
// severity is one of "high" | "medium" (kept as `string` for forward-compat).
export type ChannelIssue = {
  youtube_channel_id: string;
  channel_name: string;
  primary_company_id: string | null;
  issue_type: string;
  severity: string;
  message: string;
  recommended_action: string;
};

// GET /channels/issues. Source: list_channel_issues() (api/channels.py:185-188)
// with summary from summarize_channel_registry_issues() (channel_issues.py:62-76).
export type ChannelIssuesResponse = {
  items: ChannelIssue[];
  summary: {
    total_issue_count: number;
    channel_count: number;
    // Per-issue-type counts keyed by ChannelIssueType value (counts are ints).
    issue_type_counts: Record<string, number>;
  };
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /revenue/months/{month}/rankings
//   JSON contract consumed by the Command Center rankings panel. All money values
//   are serialized as STRINGS by the backend (decimal_to_api) and may be null
//   when a value is unknown (None is preserved, never coalesced to 0) — the UI
//   formats them for display via financeDisplay and never performs float math.
//   The panel is finance-gated (it shows money) and surfaces allocation_source so
//   a `live_fallback` is not read as authoritative. Fields are matched 1:1
//   against the backend service serializer (build_month_rankings / RankedEntry),
//   not guessed.
// Standards: Read-only typed boundary at the API surface; no logic here.
// Connections:
//   - File: backend/ums_smart_revenue/finance/rankings.py
//       RankedEntry.to_api()       -> RankedEntry
//       MonthRankingsSummary.to_api() -> MonthRankingsResponse
//   - File: backend/ums_smart_revenue/api/revenue.py
//       get_month_rankings()       -> GET /revenue/months/{month}/rankings;
//         adds allocation_source/committed_run (committed snapshot for LOCKED).
// ============================================================================

// The ranking metric the endpoint ranks by. The query param defaults to "gross".
export type RankingMetric = "gross" | "net" | "deduction";

// One ranked entity (channel, company, or sector). rank is 1-based; entity_name
// falls back to the raw id when no org-unit name resolves. Money values are
// decimal-as-STRINGS and may be null (None preserved). Source: RankedEntry.to_api().
export type RankedEntry = {
  rank: number;
  entity_id: string;
  entity_name: string;
  // Backend RankedEntry.gross_revenue_usd is a non-optional Decimal that is
  // never None (channels carry a non-None Decimal; rolled-up groups start at 0),
  // so it is a non-null MoneyString. Net/deduction stay nullable (None preserved).
  gross_revenue_usd: MoneyString;
  net_revenue_usd: MoneyString | null;
  deduction_amount_usd: MoneyString | null;
};

// ============================================================================
// Purpose: TypeScript mirror of the backend GET /revenue/scopes JSON contract
//   consumed by the Command Center scope selector. The endpoint returns ONLY the
//   rollup scopes the viewer is VIEW_REVENUE-authorized for (global / their
//   sectors / their companies), so the selector cannot offer an out-of-scope org
//   unit (org-structure leak) or a dead option that 403s on the rollup read.
//   `scope_id` is null for the global option and the org-unit id otherwise; the
//   id/label pair threads straight into the net-revenue + rankings reads. Fields
//   are matched 1:1 against RevenueScopeOption.to_api() (not guessed).
// Standards: Read-only typed boundary at the API surface; no logic here. No
//   client-side authorization is invented — the backend VIEW_REVENUE gate is
//   authoritative and the option set is the fail-closed source of truth.
// Connections:
//   - File: backend/ums_smart_revenue/finance/revenue_scopes.py
//       RevenueScopeOption.to_api() -> RevenueScopeOption
//   - File: backend/ums_smart_revenue/api/revenue.py
//       list_authorized_revenue_scopes() -> GET /revenue/scopes {scopes}
// ============================================================================

// One authorized rollup scope option. scope_type is "global" | "sector" |
// "company"; scope_id is null only for the global option (the org-unit id
// otherwise). label resolves to the org-unit name with a raw-id fallback.
// Source: RevenueScopeOption.to_api() (finance/revenue_scopes.py).
export type RevenueScopeOption = {
  scope_type: string;
  scope_id: string | null;
  label: string;
};

// GET /revenue/scopes. Source: list_authorized_revenue_scopes() (api/revenue.py).
export type RevenueScopesResponse = {
  scopes: RevenueScopeOption[];
};

// GET /revenue/months/{month}/rankings. Source: MonthRankingsSummary.to_api()
// plus the route-level allocation_source/committed_run additions.
export type MonthRankingsResponse = {
  month: string;
  metric: string;
  channels: RankedEntry[];
  companies: RankedEntry[];
  sectors: RankedEntry[];
  // Route-level additions (committed snapshot provenance for LOCKED months).
  allocation_source?: AllocationSource;
  committed_run?: CommittedRun | null;
  // Route emits the dual REVENUE_VIEWED/PAYMENT_VIEWED echo like every other
  // finance response (revenue.py get_month_rankings audit_events assignment).
  audit_events?: NetRevenueAuditEvent[];
};
