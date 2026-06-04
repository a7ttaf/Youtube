import { useCallback, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type {
  AdsensePaymentListResponse,
  AdsenseSyncRequestBody,
  AdsenseSyncResponse,
} from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type AdsensePaymentsQuery = {
  // Optional YYYY-MM month filter; the backend 422s a malformed month and
  // requires VIEW_FINALIZED_PAYMENTS @finance_month(month) (or @global when
  // omitted). Page size + offset map to the backend's limit/offset (1..100/>=0).
  month?: string;
  limit?: number;
  offset?: number;
};

// ============================================================================
// Purpose: Typed auto-fetch hook for the synced AdSense payments that have flowed
//   in from the connector. Builds GET /adsense/payments (with optional month +
//   limit/offset) on the production useApiClient and returns the shared
//   {data, loading, error, reload} async-state contract. `reload` is the refetch
//   the sync action calls after a successful POST so new rows appear.
// Database/ORM: None (frontend) — reads the backend AdSense payment list.
// Standards: query params are URL-encoded; the request closure is memoized so
//   useAsync does not refetch on every render. payment_amount stays a STRING
//   (decimal_to_api) and is formatted for display only — never float math. The
//   read is gated by VIEW_FINALIZED_PAYMENTS @finance_month, so a non-finance
//   viewer gets a typed 403 the view renders as a no-permission message.
// Blast Radius: None detected (read-only finance payment display).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> AdsensePaymentListResponse.
//   - File: backend/ums_smart_revenue/api/adsense.py:204 list_adsense_payments.
// ============================================================================
export function useAdsensePayments(
  query: AdsensePaymentsQuery = {},
): AsyncState<AdsensePaymentListResponse> {
  const client = useApiClient();
  const { month, limit, offset } = query;

  const run = useCallback(() => {
    const params = new URLSearchParams();
    if (month) params.set("month", month);
    if (limit != null) params.set("limit", String(limit));
    if (offset != null) params.set("offset", String(offset));
    const qs = params.toString();
    return client.get<AdsensePaymentListResponse>(
      `/adsense/payments${qs ? `?${qs}` : ""}`,
    );
  }, [client, month, limit, offset]);

  return useAsync(run);
}

export type UseAdsenseSyncActionsState = {
  // The most recent sync result (cleared while a new sync is in flight).
  data: AdsenseSyncResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
  // Trigger the POST on user action; resolves with the synced batch result, or
  // with `null` when a same-tick duplicate submit is dropped by the in-flight
  // guard (no POST fired), or rejects with the typed error already captured in
  // `error`.
  syncPayments: (
    body: AdsenseSyncRequestBody,
  ) => Promise<AdsenseSyncResponse | null>;
};

// ============================================================================
// Purpose: Imperative action hook for syncing a manually supplied AdSense payment
//   batch from the connector. Unlike the read hook this is a POST that WRITES
//   payment rows (idempotent upsert by source identity) + audits the batch, so
//   it is triggered by a user action ("Sync payments") rather than auto-fetched.
//   It POSTs the AdsenseSyncRequestBody to /adsense/sync-payments on the
//   production useApiClient and tracks {data, loading, error} while exposing a
//   stable syncPayments(body). A superseded request is discarded so a slow
//   earlier submit cannot overwrite a newer one.
// Database/ORM: None (frontend) — calls the backend AdSense sync endpoint, which
//   upserts AdSensePaymentORM rows + records an ADSENSE_PAYMENT_SYNCED audit
//   event server-side. The backend rejects a locked finance month (409).
// Standards: the body is JSON-encoded by useApiClient; reason + at least one
//   payment are required by the backend. Errors propagate as the typed ApiError
//   (403 = missing RUN_CONNECTOR_JOBS @connector, 409 = locked month, 422 =
//   blank/malformed field) for the caller to translate. No client-side
//   authorization is invented and no finance number is computed here.
// Blast Radius: Finance payment write (source-of-truth rows) — but only via the
//   backend's own guarded, audited route.
// Supersession: each syncPayments() reads a fresh token from a useRef counter
//   incremented synchronously inside the callback, so two same-render submits get
//   distinct tokens (a useState read off the render closure would hand both the
//   same token). State writes are guarded by requestIdRef.current === token, so
//   only the most recently started request can commit {data,loading,error}.
// Dedupe: a synchronous inFlightRef drops a same-tick duplicate submit (e.g. a
//   double-click before re-render) BEFORE the POST fires, so one click can never
//   trigger two payment-upsert batches + ADSENSE_PAYMENT_SYNCED audit events.
//   The state-based `loading` guard cannot catch this (both clicks read the same
//   stale loading=false). The dropped call resolves with null (it is dropped,
//   NOT queued); the ref clears in finally so the next submit proceeds.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> AdsenseSyncRequestBody / AdsenseSyncResponse.
//   - File: backend/ums_smart_revenue/api/adsense.py:133 sync_adsense_payments.
// ============================================================================
export function useAdsenseSyncActions(): UseAdsenseSyncActionsState {
  const client = useApiClient();
  const [data, setData] = useState<AdsenseSyncResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  // Monotonic counter so only the latest syncPayments() is allowed to commit.
  const requestIdRef = useRef(0);
  // Synchronous in-flight latch so a same-tick duplicate submit is dropped
  // before its POST fires (the state `loading` guard cannot see it in time).
  const inFlightRef = useRef(false);

  const syncPayments = useCallback(
    (body: AdsenseSyncRequestBody): Promise<AdsenseSyncResponse | null> => {
      // FIX: drop a same-tick duplicate submit before the POST fires; a
      // double-click before re-render would otherwise pass the stale state
      // `loading` guard twice and upsert the same payment batch twice.
      if (inFlightRef.current) return Promise.resolve(null);
      inFlightRef.current = true;
      // FIX: take the token from a ref incremented synchronously so two submits
      // in the same render get distinct tokens; the old `requestId + 1` read off
      // the render closure handed both the same token, letting a slow earlier
      // response overwrite a newer one.
      const token = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      return client
        .post<AdsenseSyncResponse>("/adsense/sync-payments", body)
        .then((result) => {
          // Supersede: ignore a stale submit whose request changed mid-flight.
          if (requestIdRef.current === token) {
            setData(result);
            setLoading(false);
          }
          return result;
        })
        .catch((caught: unknown) => {
          const typed =
            caught instanceof Error ? caught : new Error(String(caught));
          if (requestIdRef.current === token) {
            setData(null);
            setError(typed);
            setLoading(false);
          }
          throw typed;
        })
        .finally(() => {
          inFlightRef.current = false;
        });
    },
    [client],
  );

  return { data, loading, error, syncPayments };
}
