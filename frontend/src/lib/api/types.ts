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
