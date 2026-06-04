import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { NetRevenueResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type NetRevenueQuery = {
  month: string;
  // Defaults to a global read when omitted (matches the backend route default).
  scopeType?: string;
  scopeId?: string | null;
  currency?: string;
};

// ============================================================================
// Purpose: Typed fetch hook for the monthly net-revenue summary. Builds the
//   GET /revenue/months/{month}/net-revenue request (with scope_type/scope_id/
//   currency query params) on the production useApiClient and returns the
//   {data, loading, error, reload} async-state contract every wired screen uses.
// Database/ORM: None (frontend) — reads the backend net-revenue read endpoint.
// Standards: month is path-encoded; scope params are query-encoded; the request
//   closure is memoized so useAsync does not refetch on every render. Decimal
//   money values stay strings (see NetRevenueResponse).
// Blast Radius: None detected (read-only finance display).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: backend/ums_smart_revenue/api/revenue.py:1088 get_month_net_revenue.
// ============================================================================
export function useNetRevenue(query: NetRevenueQuery): AsyncState<NetRevenueResponse> { // skipcq: JS-0067
  const client = useApiClient();
  const { month, scopeType, scopeId, currency } = query;

  const run = useCallback(() => {
    const params = new URLSearchParams();
    if (scopeType) params.set("scope_type", scopeType);
    if (scopeId) params.set("scope_id", scopeId);
    if (currency) params.set("currency", currency);
    const qs = params.toString();
    const path = `/revenue/months/${encodeURIComponent(month)}/net-revenue${
      qs ? `?${qs}` : ""
    }`;
    return client.get<NetRevenueResponse>(path);
  }, [client, month, scopeType, scopeId, currency]);

  return useAsync(run);
}
