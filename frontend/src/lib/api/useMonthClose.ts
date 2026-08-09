import { useCallback, useMemo } from "react";

import { useApiClient } from "@/lib/api/client";
import type {
  FinanceCloseReadinessResponse,
  FinanceMonthCloseMutationResponse,
  FinanceMonthCloseStatus,
} from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type MonthCloseQuery = {
  month: string;
};

// ============================================================================
// Purpose: Typed fetch hook for a finance month's close STATUS (OPEN/LOCKED +
//   lock/unlock actor/timestamps). Builds GET /finance-close/{month} on the
//   production useApiClient and returns the {data, loading, error, reload}
//   async-state contract every wired screen uses.
// Database/ORM: None (frontend) — reads the backend finance-close GET endpoint.
// Standards: month is path-encoded; the request closure is memoized so useAsync
//   does not refetch on every render. Timestamps stay ISO strings.
// Blast Radius: None detected (read-only finance close display).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: backend/ums_smart_revenue/api/finance_close.py -> get_finance_month_close.
// ============================================================================
export const useMonthClose = (
  query: MonthCloseQuery,
): AsyncState<FinanceMonthCloseStatus> => {
  const client = useApiClient();
  const { month } = query;

  const run = useCallback(
    () =>
      client.get<FinanceMonthCloseStatus>(
        `/finance-close/${encodeURIComponent(month)}`,
      ),
    [client, month],
  );

  return useAsync(run);
};

// ============================================================================
// Purpose: Typed fetch hook for a finance month's close READINESS (ready flag +
//   blocker checklist). Builds GET /finance-close/{month}/readiness on the same
//   useApiClient and returns the shared async-state contract.
// Database/ORM: None (frontend) — reads the backend readiness GET endpoint.
// Standards: month is path-encoded; the request closure is memoized. The
//   readiness gate requires LOCK_FINANCE_MONTH, so non-finance viewers get a
//   typed 403 here that the view renders as a no-permission message.
// Blast Radius: None detected (read-only finance close display).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: backend/ums_smart_revenue/api/finance_close.py -> get_finance_close_readiness.
// ============================================================================
export const useMonthCloseReadiness = (
  query: MonthCloseQuery,
): AsyncState<FinanceCloseReadinessResponse> => {
  const client = useApiClient();
  const { month } = query;

  const run = useCallback(
    () =>
      client.get<FinanceCloseReadinessResponse>(
        `/finance-close/${encodeURIComponent(month)}/readiness`,
      ),
    [client, month],
  );

  return useAsync(run);
};

// ============================================================================
// Purpose: Imperative lock/unlock actions for a finance month close. Each is a
//   simple POST {reason} that returns the updated close status; callers invoke
//   them from a confirm flow and refetch status on success. Returned as stable
//   callbacks so the view can wire them to buttons without re-creating handlers.
// Database/ORM: None (frontend) — calls the backend lock/unlock write endpoints.
// Standards: reason is sent in the JSON body (backend requires a non-blank
//   reason). Errors propagate as the typed ApiError (409 = blockers / wrong
//   state, 403 = missing permission) for the caller to translate to UI copy.
// Blast Radius: Finance month locks (write path) — but only via the backend's
//   own guarded, audited lock/unlock routes; this hook adds no client-side
//   authorization and never mutates finance numbers directly.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + X-UMS-Tenant.
//   - File: backend/ums_smart_revenue/api/finance_close.py -> lock_finance_month.
//   - File: backend/ums_smart_revenue/api/finance_close.py -> unlock_finance_month.
// ============================================================================
export const useMonthCloseActions = (query: MonthCloseQuery) => {
  const client = useApiClient();
  const { month } = query;
  const encoded = encodeURIComponent(month);

  const lock = useCallback(
    (reason: string) =>
      client.post<FinanceMonthCloseMutationResponse>(
        `/finance-close/${encoded}/lock`,
        { reason },
      ),
    [client, encoded],
  );

  const unlock = useCallback(
    (reason: string) =>
      client.post<FinanceMonthCloseMutationResponse>(
        `/finance-close/${encoded}/unlock`,
        { reason },
      ),
    [client, encoded],
  );

  return useMemo(() => ({ lock, unlock }), [lock, unlock]);
};
