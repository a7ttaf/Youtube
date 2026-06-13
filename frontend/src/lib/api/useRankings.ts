import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { MonthRankingsResponse, RankingMetric } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type RankingsQuery = {
  month: string;
  // Defaults to a global read when omitted (matches the backend route default).
  scopeType?: string;
  scopeId?: string | null;
  // Defaults to "gross" on the backend when omitted.
  metric?: RankingMetric;
  // Top-N per dimension; defaults to 10 on the backend when omitted (max 100).
  limit?: number;
};

// ============================================================================
// Purpose: Typed fetch hook for the monthly company/sector/channel rankings.
//   Builds GET /revenue/months/{month}/rankings (with scope_type/scope_id/
//   metric/limit query params) on the production useApiClient and returns the
//   {data, loading, error, reload} async-state contract every wired screen uses.
// Database/ORM: None (frontend) — reads the backend finance rankings endpoint.
// Standards: month is path-encoded; scope/metric/limit are query-encoded; the
//   request closure is memoized so useAsync does not refetch on every render.
//   Decimal money values stay STRINGS and may be null (None preserved — see
//   MonthRankingsResponse); the panel formats them via financeDisplay and is
//   gated on canViewFinance. The backend is finance-gated (VIEW_REVENUE +
//   VIEW_CONFIDENCE + VIEW_FINALIZED_PAYMENTS); a 403 surfaces as a typed
//   ApiError. No client-side authorization is invented here.
// Blast Radius: None detected (read-only finance display).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> MonthRankingsResponse.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_rankings.
// ============================================================================
export function useRankings( // skipcq: JS-0067
  query: RankingsQuery,
): AsyncState<MonthRankingsResponse> {
  const client = useApiClient();
  const { month, scopeType, scopeId, metric, limit } = query;

  const run = useCallback(() => {
    const params = new URLSearchParams();
    if (scopeType) params.set("scope_type", scopeType);
    if (scopeId) params.set("scope_id", scopeId);
    if (metric) params.set("metric", metric);
    if (limit !== undefined) params.set("limit", String(limit));
    const qs = params.toString();
    const path = `/revenue/months/${encodeURIComponent(month)}/rankings${
      qs ? `?${qs}` : ""
    }`;
    return client.get<MonthRankingsResponse>(path);
  }, [client, month, scopeType, scopeId, metric, limit]);

  return useAsync(run);
}
