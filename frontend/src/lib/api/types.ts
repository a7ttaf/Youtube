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
