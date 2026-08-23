import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { MonthGapExplanation } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type GapExplanationQuery = {
  month: string;
  // When false, the hook issues NO request. CommandView uses this to avoid
  // calling the composed endpoint when the current UI session cannot pass its
  // four-gate set (global revenue + global confidence + month-satisfying
  // payments and bank grants) — the endpoint enforces every gate server-side.
  enabled?: boolean;
};

// ============================================================================
// Purpose: Typed fetch hook for the composed month gap explanation. Builds
//   GET /revenue/months/{month}/gap-explanation on the production useApiClient
//   and returns the {data, loading, error, reload} async-state contract. The
//   endpoint gates on the smart-alerts FOUR-gate set — global VIEW_REVENUE,
//   global VIEW_CONFIDENCE (the response carries confidence on every
//   component and residual), plus month-scoped VIEW_FINALIZED_PAYMENTS and
//   VIEW_BANK_RECONCILIATION — so a missing grant surfaces as a typed 403.
// Database/ORM: None (frontend) — reads the backend gap-explanation endpoint.
// Standards: month is path-encoded; the request closure is memoized; every
//   money value is a backend decimal string and is never calculated in the
//   browser (the legs' arithmetic lives in the backend builder only).
// Blast Radius: Finance display only. Read-only — no mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> MonthGapExplanation.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_gap_explanation().
// ============================================================================
export const useGapExplanation = (
  query: GapExplanationQuery,
): AsyncState<MonthGapExplanation> => {
  const client = useApiClient();
  const { month, enabled = true } = query;

  const run = useCallback(
    () =>
      client.get<MonthGapExplanation>(
        `/revenue/months/${encodeURIComponent(month)}/gap-explanation`,
      ),
    [client, month],
  );

  return useAsync(run, enabled);
};
