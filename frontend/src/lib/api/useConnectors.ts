import { useCallback, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type {
  ConnectorCredentialListResponse,
  ConnectorJobRequestBody,
  ConnectorJobResponse,
} from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type ConnectorCredentialsQuery = {
  // Page size + offset map straight to the backend's limit/offset query params
  // (1..100 / >=0). Both optional; the backend defaults to limit=50, offset=0.
  limit?: number;
  offset?: number;
};

// ============================================================================
// Purpose: Typed auto-fetch hook for the configured connector credentials (the
//   "data sources configured" list). Builds GET /connectors/credentials (with
//   optional limit/offset) on the production useApiClient and returns the shared
//   {data, loading, error, reload} async-state contract. The secret itself is
//   never returned by the backend — each row only exposes has_secret_ref.
// Database/ORM: None (frontend) — reads the backend connector-credentials list.
// Standards: query params are URL-encoded; the request closure is memoized so
//   useAsync does not refetch on every render. No money values on these rows; no
//   client-side authorization is invented — the list route already filters to
//   the connector keys the caller may manage (or 403s on no manage scope).
// Blast Radius: None detected (read-only data-source status display).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ConnectorCredentialListResponse.
//   - File: backend/ums_smart_revenue/api/connectors.py:58 list_connector_credentials.
// ============================================================================
export const useConnectorCredentials = (
  query: ConnectorCredentialsQuery = {},
): AsyncState<ConnectorCredentialListResponse> => {
  const client = useApiClient();
  const { limit, offset } = query;

  const run = useCallback(() => {
    const params = new URLSearchParams();
    if (limit != null) params.set("limit", String(limit));
    if (offset != null) params.set("offset", String(offset));
    const qs = params.toString();
    return client.get<ConnectorCredentialListResponse>(
      `/connectors/credentials${qs ? `?${qs}` : ""}`,
    );
  }, [client, limit, offset]);

  return useAsync(run);
};

export type UseConnectorJobActionsState = {
  // The most recent job-request result (cleared while a new request is in flight).
  data: ConnectorJobResponse | null;
  loading: boolean;
  error: ApiError | Error | null;
  // Trigger the POST on user action; resolves with the job-request result
  // (execution_status === "submitted" on the executing path), or with `null` when
  // a same-tick duplicate submit is dropped by the in-flight guard (no POST fired),
  // or rejects with the typed error already captured in `error`.
  requestJob: (
    body: ConnectorJobRequestBody,
  ) => Promise<ConnectorJobResponse | null>;
};

// ============================================================================
// Purpose: Imperative action hook for requesting a connector job run. Unlike the
//   read hook this is a POST that records (and audits) a job-run intent, so it is
//   triggered by a user action ("Request sync") rather than auto-fetched. It
//   POSTs the ConnectorJobRequestBody to /connectors/jobs (202) on the production
//   useApiClient and tracks {data, loading, error} while exposing a stable
//   requestJob(body). A superseded request is discarded so a slow earlier submit
//   cannot overwrite a newer one (rapid re-submits).
// Database/ORM: None (frontend) — calls the backend connector-job endpoint, which
//   submits the pull to the in-process executor and records a CONNECTOR_JOB_RUN
//   audit event server-side (execution_status === "submitted"); progress shows in
//   the GET /connectors/runs run-history feed.
// Standards: the body is JSON-encoded by useApiClient; reason is required by the
//   backend (min_length=1). Errors propagate as the typed ApiError (403 =
//   missing RUN_CONNECTOR_JOBS @connector, 422 = blank field) for the caller to
//   translate. No client-side authorization is invented.
// Blast Radius: Audit write only (no finance number, no connector execution) via
//   the backend's own guarded, audited route.
// Supersession: each requestJob() reads a fresh token from a useRef counter
//   incremented synchronously inside the callback, so two same-render submits get
//   distinct tokens (a useState read off the render closure would hand both the
//   same token). State writes are guarded by requestIdRef.current === token, so
//   only the most recently started request can commit {data,loading,error}.
// Dedupe: a synchronous inFlightRef drops a same-tick duplicate submit (e.g. a
//   double-click before re-render) BEFORE the POST fires, so a single click can
//   never record two CONNECTOR_JOB_RUN audit events. The state-based `loading`
//   guard cannot catch this (both clicks read the same stale loading=false). The
//   dropped call resolves with null (it is dropped, NOT queued); the ref clears
//   in finally so the next user-initiated submit proceeds.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ConnectorJobRequestBody / ConnectorJobResponse.
//   - File: backend/ums_smart_revenue/api/connectors.py:122 request_connector_job.
// ============================================================================
export const useConnectorJobActions = (): UseConnectorJobActionsState => {
  const client = useApiClient();
  const [data, setData] = useState<ConnectorJobResponse | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  // Monotonic counter so only the latest requestJob() is allowed to commit.
  const requestIdRef = useRef(0);
  // Synchronous in-flight latch so a same-tick duplicate submit is dropped
  // before its POST fires (the state `loading` guard cannot see it in time).
  const inFlightRef = useRef(false);

  const requestJob = useCallback(
    (body: ConnectorJobRequestBody): Promise<ConnectorJobResponse | null> => {
      // FIX: drop a same-tick duplicate submit before the POST fires; a
      // double-click before re-render would otherwise pass the stale state
      // `loading` guard twice and record duplicate CONNECTOR_JOB_RUN events.
      if (inFlightRef.current) return Promise.resolve(null);
      inFlightRef.current = true;
      // FIX: take the token from a ref incremented synchronously so two submits
      // in the same render get distinct tokens; the old `requestId + 1` read off
      // the render closure handed both the same token, letting a slow earlier
      // response overwrite a newer one.
      const token = ++requestIdRef.current;
      // FIX: clear any stale job result so a pending second request does not
      // show the previous run's success banner while loading.
      setData(null);
      setLoading(true);
      setError(null);
      return client
        .post<ConnectorJobResponse>("/connectors/jobs", body)
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

  return { data, loading, error, requestJob };
};
