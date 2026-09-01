import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { SmartAlertsSummary } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type SmartAlertsQuery = {
  month: string;
  /** False withholds the request until every endpoint grant can be proven. */
  enabled?: boolean;
};

// ============================================================================
// Purpose: Typed fetch hook for the monthly smart-alerts / problem panel. Builds
//   GET /revenue/months/{month}/smart-alerts on the production useApiClient and
//   returns the {data, loading, error, reload} async-state contract every wired
//   screen uses. The endpoint is finance-month scoped behind four permissions
//   (VIEW_REVENUE + VIEW_CONFIDENCE + VIEW_FINALIZED_PAYMENTS +
//   VIEW_BANK_RECONCILIATION), so a role lacking any of them gets a typed 403
//   that the panel renders as a no-permission message — independently of the
//   rest of the Command Center.
// Database/ORM: None (frontend) — reads the backend smart-alerts read endpoint.
// Standards: month is path-encoded; the request closure is memoized so useAsync
//   does not refetch on every render. Money values inside alert details stay
//   strings (see SmartAlertsSummary). Read-only — no mutation.
// Blast Radius: None detected (read-only finance health display).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> SmartAlertsSummary contract.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_smart_alerts.
// ============================================================================
export const useSmartAlerts = (
  query: SmartAlertsQuery,
): AsyncState<SmartAlertsSummary> => {
  const client = useApiClient();
  const { month, enabled = true } = query;

  const run = useCallback(
    () =>
      client.get<SmartAlertsSummary>(
        `/revenue/months/${encodeURIComponent(month)}/smart-alerts`,
      ),
    [client, month],
  );

  return useAsync(run, enabled);
};
