import { useCallback, useState } from "react";

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
  // Trigger the POST on user action; resolves with the explanation (or rejects
  // with the typed error already captured in `error`).
  run: (params: ExplanationParams) => Promise<NumberExplanation>;
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
  // Monotonic token so only the latest run() is allowed to commit state.
  const [requestId, setRequestId] = useState(0);

  const run = useCallback(
    (params: ExplanationParams): Promise<NumberExplanation> => {
      const { channelId, month, metric } = params;
      const path =
        `/revenue/channels/${encodeURIComponent(channelId)}` +
        `/months/${encodeURIComponent(month)}/explain` +
        `?metric=${encodeURIComponent(metric)}`;
      const token = requestId + 1;
      setRequestId(token);
      setLoading(true);
      setError(null);
      return client
        .post<NumberExplanation>(path)
        .then((result) => {
          // Supersede: ignore a stale run whose params changed mid-flight.
          setRequestId((current) => {
            if (current === token) {
              setData(result);
              setLoading(false);
            }
            return current;
          });
          return result;
        })
        .catch((caught: unknown) => {
          const typed =
            caught instanceof Error ? caught : new Error(String(caught));
          setRequestId((current) => {
            if (current === token) {
              setData(null);
              setError(typed);
              setLoading(false);
            }
            return current;
          });
          throw typed;
        });
    },
    [client, requestId],
  );

  return { data, loading, error, run };
}
