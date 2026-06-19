import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { MonthBankReconciliationSummary } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type BankReconciliationQuery = {
  month: string;
  // When false, the hook issues NO request. CommandView uses this to avoid
  // calling the payment/bank endpoint when the current UI session cannot render
  // finance values.
  enabled?: boolean;
};

// ============================================================================
// Purpose: Typed fetch hook for the monthly bank-reconciliation summary. Builds
//   GET /revenue/months/{month}/bank-reconciliation on the production
//   useApiClient and returns the {data, loading, error, reload} async-state
//   contract. The endpoint is finance-month scoped behind finalized-payment and
//   bank-reconciliation permissions, so a missing grant surfaces as a typed 403.
// Database/ORM: None (frontend) — reads the backend bank-reconciliation endpoint.
// Standards: month is path-encoded; the request closure is memoized; official
//   AdSense/bank/gap values are backend decimal strings and are never calculated
//   in the browser.
// Blast Radius: Finance display only. Read-only — no mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> MonthBankReconciliationSummary.
//   - File: backend/ums_smart_revenue/api/revenue.py -> get_month_bank_reconciliation().
// ============================================================================
export const useBankReconciliation = (
  query: BankReconciliationQuery,
): AsyncState<MonthBankReconciliationSummary> => {
  const client = useApiClient();
  const { month, enabled = true } = query;

  const run = useCallback(
    () =>
      client.get<MonthBankReconciliationSummary>(
        `/revenue/months/${encodeURIComponent(month)}/bank-reconciliation`,
      ),
    [client, month],
  );

  return useAsync(run, enabled);
};
