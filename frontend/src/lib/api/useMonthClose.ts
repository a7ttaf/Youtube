import { useCallback, useMemo } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type {
  FinanceCloseReadinessResponse,
  FinanceMonthCloseMutationResponse,
  FinanceMonthCloseStatus,
} from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type MonthCloseQuery = {
  month: string;
};

/**
 * The status GET's 404: this month has no finance_month_close row yet. Close
 * rows are created by finance writes, so an untouched month legitimately has
 * none — that is an ABSENCE, not a failure.
 */
const CLOSE_RECORD_NOT_FOUND = 404;

/**
 * The exact detail string get_finance_month_close raises with that 404 — the
 * machine-readable contract that distinguishes "no close row" from every other
 * 404 the SPA can receive.
 */
const CLOSE_RECORD_NOT_FOUND_DETAIL = "Finance month close record not found";

// ============================================================================
// Purpose: The one predicate that decides whether a 404 from the close status
//   GET means "this month has no close row yet" — the machine-readable
//   discrimination between the finance-close endpoint's documented
//   missing-record response and every other 404 the SPA can receive.
// Database/ORM: None (frontend) — matches the detail string raised by
//   backend get_finance_month_close (finance_close.py).
// Standards: Fail-closed by string contract: status AND parsed body detail must
//   both match exactly; anything else (tenant 404s, routing 404s, malformed
//   bodies) falls through and stays a typed error. The exact string is pinned
//   by tests on both sides of the remap.
// Blast Radius: Finance-close display semantics — deciding between the honest
//   not-started state and an error tile. No write path; the backend stays the
//   authority on the close row.
// Connections:
//   - File: backend/ums_smart_revenue/api/finance_close.py -> raises the 404
//     with detail "Finance month close record not found" (single definition).
//   - File: frontend/tests/lib/api/useMonthClose.test.tsx -> pins both the
//     remapped close-record detail and the tenant-404 error path.
// ============================================================================
/** True ONLY for the finance-close status GET's documented missing-record 404. */
const isMissingCloseRecord = (error: ApiError): boolean => {
  return (
    error.status === CLOSE_RECORD_NOT_FOUND &&
    typeof error.body === "object" &&
    error.body !== null &&
    (error.body as { detail?: unknown }).detail === CLOSE_RECORD_NOT_FOUND_DETAIL
  );
};

// ============================================================================
// Purpose: Typed fetch hook for a finance month's close STATUS (OPEN/LOCKED +
//   lock/unlock actor/timestamps). Builds GET /finance-close/{month} on the
//   production useApiClient and returns the {data, loading, error, reload}
//   async-state contract every wired screen uses.
// Database/ORM: None (frontend) — reads the backend finance-close GET endpoint.
// Standards: month is path-encoded; the request closure is memoized so useAsync
//   does not refetch on every render. Timestamps stay ISO strings.
//   ONLY the documented missing-record 404 IS NOT AN ERROR. get_finance_month_close
//   raises that 404 with detail "Finance month close record not found" whenever
//   the month has no close row, and close rows only ever appear once a finance
//   write touches the month — so the rolling CURRENT-month default the views
//   open on is exactly such a month. Surfacing that as an error made the Close
//   screen replace its whole summary with "Request failed (404)" on its own
//   default month. This hook resolves that ONE response to `null` data with a
//   null error, so the settled (data=null, error=null) pair means "no close
//   record yet" and the view renders its honest not-started state. EVERY other
//   response — 403, 5xx, network, AND any 404 whose detail is not the close
//   contract (tenant 404s, routing 404s) — still rejects and still reaches
//   `error` untouched, and the sibling readiness read is not remapped at all.
// Blast Radius: None detected (read-only finance close display). It never
//   invents a close status: the caller still sees no record, and lock/unlock
//   remain gated by the backend.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: backend/ums_smart_revenue/api/finance_close.py:104
//     get_finance_month_close -> raises the 404 this maps to "no record".
//   - File: frontend/src/components/srcc/views/CloseView.tsx ->
//     CloseStatusSummary renders the (data=null, error=null) not-started state.
// ============================================================================
export const useMonthClose = (
  query: MonthCloseQuery,
  // The `| null` on the data type is deliberate: a SETTLED read with data null
  // and error null is the "no close record yet" verdict, and the type says so.
): AsyncState<FinanceMonthCloseStatus | null> => {
  const client = useApiClient();
  const { month } = query;

  const run = useCallback(
    (): Promise<FinanceMonthCloseStatus | null> =>
      client
        .get<FinanceMonthCloseStatus>(`/finance-close/${encodeURIComponent(month)}`)
        .catch((caught: unknown) => {
          if (caught instanceof ApiError && isMissingCloseRecord(caught)) {
            return null;
          }
          throw caught;
        }),
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
