import { useCallback, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { ExplanationMetric, NumberExplanation } from "@/lib/api/types";

export type ExplanationParams = {
  channelId: string;
  month: string;
  metric: ExplanationMetric;
};

export type UseExplanationState = {
  data: NumberExplanation | null;
  loading: boolean;
  error: ApiError | Error | null;
  // Trigger the POST on user action; resolves with the explanation, or with
  // `null` when a same-tick duplicate Explain is dropped by the in-flight guard
  // (no POST fired), or rejects with the typed error already captured in `error`.
  run: (params: ExplanationParams) => Promise<NumberExplanation | null>;
  // Clear {data, error} and abandon any in-flight request so a result cannot
  // commit under filters it no longer matches; the next run() is not blocked.
  reset: () => void;
};

// ============================================================================
// Purpose: Imperative action hook for the number-explanation flow. Unlike the
//   read hooks (useNetRevenue/useMonthClose) this is a POST that GENERATES,
//   persists, and audits an explanation, so it is triggered by a user action
//   ("Explain") rather than auto-fetched on mount. It builds POST /revenue/
//   channels/{channel_id}/months/{month}/explain?metric={metric} on the
//   production useApiClient and tracks {data, loading, error} while exposing a
//   stable run(params). A superseded request is discarded so a slow earlier
//   Explain cannot overwrite a newer one (rapid channel/metric switching).
// Database/ORM: None (frontend) — calls the backend explain write endpoint,
//   which upserts NumberExplanationORM server-side.
// Standards: channel id + month are path-encoded; metric is query-encoded; the
//   request closure is memoized. Decimal money values stay strings (see
//   NumberExplanation). Errors propagate as the typed ApiError (403 = missing
//   permission / finalized-payment gate on net, 404 = no revenue facts, 422 =
//   indeterminate net / unsupported metric) for the caller to translate.
// Blast Radius: Finance number explanations (write path) — but only via the
//   backend's own guarded, audited route; this hook adds no client-side
//   authorization and never computes a finance number directly.
// Supersession: each run() reads a fresh token from a useRef counter incremented
//   synchronously inside the callback, so two same-render Explains get distinct
//   tokens (a useState read off the render closure would hand both the same
//   token). State writes are guarded by requestIdRef.current === token, so only
//   the most recently started run can commit {data,loading,error}. Supersession
//   governs SEQUENTIAL calls whose params changed mid-flight (latest wins).
// Dedupe: the supersession token alone does NOT prevent a same-tick double-click
//   from firing two POSTs — and each explain POST upserts the explanation AND
//   writes a REVENUE_VIEWED (net: PAYMENT_VIEWED) audit event, so two POSTs mean
//   duplicate audit records. A synchronous inFlightRef latch (the same pattern as
//   useExportActions / useConnectors / useAdsense) drops a same-tick duplicate
//   BEFORE its POST fires; the state-based `loading` guard cannot catch it (both
//   clicks read the same stale loading=false). The dropped call resolves with
//   null (it is dropped, NOT queued); the ref clears in finally so the next
//   user-initiated Explain proceeds.
// Reset: supersession only discards a request superseded by a LATER run(); it
//   does NOT cover the case where the operator changes month/channel/metric while
//   an explain is in flight WITHOUT re-running, leaving the old response to commit
//   and render under the new filters. reset() closes that gap: it clears
//   {data,error}, bumps requestIdRef.current (so an in-flight resolution then
//   fails its `requestIdRef.current === token` guard and is discarded, never
//   committing), and clears the inFlightRef latch (so the bump does not strand
//   the latch and block the next run()). Callers invoke it from an effect keyed
//   on the explain inputs.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> NumberExplanation contract.
//   - File: backend/ums_smart_revenue/api/revenue.py:1358 explain endpoint.
// ============================================================================
export function useExplanation(): UseExplanationState {
  const client = useApiClient();
  const [data, setData] = useState<NumberExplanation | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  // Monotonic counter so only the latest run() is allowed to commit state.
  const requestIdRef = useRef(0);
  // Synchronous in-flight latch so a same-tick duplicate Explain is dropped
  // before its POST fires (the state `loading` guard cannot see it in time).
  const inFlightRef = useRef(false);

  const run = useCallback(
    (params: ExplanationParams): Promise<NumberExplanation | null> => {
      // FIX: drop a same-tick duplicate Explain before the POST fires; a
      // double-click before re-render would otherwise pass the stale state
      // `loading` guard twice and write duplicate REVENUE_VIEWED / PAYMENT_VIEWED
      // audit events (each explain POST upserts + audits server-side).
      if (inFlightRef.current) return Promise.resolve(null);
      inFlightRef.current = true;
      const { channelId, month, metric } = params;
      const path =
        `/revenue/channels/${encodeURIComponent(channelId)}` +
        `/months/${encodeURIComponent(month)}/explain` +
        `?metric=${encodeURIComponent(metric)}`;
      // FIX: take the token from a ref incremented synchronously so two Explains
      // in the same render get distinct tokens; the old `requestId + 1` read off
      // the render closure handed both the same token, letting a slow earlier
      // response overwrite a newer one.
      const token = ++requestIdRef.current;
      setLoading(true);
      setError(null);
      return client
        .post<NumberExplanation>(path)
        .then((result) => {
          // Supersede: ignore a stale run whose params changed mid-flight.
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

  // ==========================================================================
  // Purpose: Abandon any in-flight explanation and clear the rendered result so
  //   a response cannot commit under filters it no longer matches. Bumping
  //   requestIdRef.current makes an in-flight resolution fail its token guard
  //   (discarded, never setData), and clearing inFlightRef releases the latch so
  //   the bump does not block the next run(). Stable identity (no deps) so it can
  //   sit in an effect dependency list without re-firing.
  // Standards: state setters only; no fetch, no auth, no finance math.
  // ==========================================================================
  const reset = useCallback(() => {
    requestIdRef.current += 1;
    inFlightRef.current = false;
    setData(null);
    setError(null);
    setLoading(false);
  }, []);

  return { data, loading, error, run, reset };
}
