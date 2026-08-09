import { useCallback, useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { ExportJobCreated, ExportRequestBody } from "@/lib/api/types";

export type UseExportActionsState = {
  // The most recently created job (cleared while a new request is in flight).
  data: ExportJobCreated | null;
  loading: boolean;
  error: ApiError | Error | null;
  // Trigger the POST on user action; resolves with the created job, or with
  // `null` when a same-tick duplicate submit is dropped by the in-flight guard
  // (no POST fired), or rejects with the typed error already captured in `error`.
  requestExport: (body: ExportRequestBody) => Promise<ExportJobCreated | null>;
};

// ============================================================================
// Purpose: Imperative action hook for requesting a new export job. Unlike the
//   read hook (useExports) this is a POST that CREATES, snapshots scope, and
//   audits an export, so it is triggered by a user action ("Generate") rather
//   than auto-fetched on mount. It POSTs the ExportRequestBody to /exports
//   (202 -> the created QUEUED job + audit_event) on the production
//   useApiClient and tracks {data, loading, error} while exposing a stable
//   requestExport(body). A superseded request is discarded so a slow earlier
//   submit cannot overwrite a newer one (rapid re-submits).
// Database/ORM: None (frontend) — calls the backend export-create endpoint,
//   which inserts ExportJobORM + an EXPORT_CREATED audit event server-side.
// Standards: the body is JSON-encoded by useApiClient; reason is required by the
//   backend (min_length=1). Errors propagate as the typed ApiError (403 =
//   missing export/view permission or finalized-payment gate, 404 = unknown
//   scope, 422 = unknown export_type / blank field) for the caller to translate.
// Blast Radius: Export create (write path) — but only via the backend's own
//   guarded, audited route; this hook adds no client-side authorization and
//   never computes a finance number.
// Supersession: each requestExport() reads a fresh token from a useRef counter
//   incremented synchronously inside the callback, so two same-render submits get
//   distinct tokens (a useState read off the render closure would hand both the
//   same token). State writes are guarded by requestIdRef.current === token, so
//   only the most recently started request can commit {data,loading,error}.
// Dedupe: a synchronous inFlightRef drops a same-tick duplicate submit (e.g. a
//   double-click before re-render) BEFORE the POST fires. The state-based
//   `loading` guard in the view cannot catch this — both clicks read the stale
//   loading=false from the same render — so without this ref BOTH would POST,
//   creating duplicate export jobs + EXPORT_CREATED audit events. The dropped
//   call resolves with null (it is dropped, NOT queued); the ref is cleared in
//   finally so the next user-initiated submit proceeds.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ExportRequestBody / ExportJobCreated.
//   - File: backend/ums_smart_revenue/api/exports.py -> request_export.
// ============================================================================
export const useExportActions = (): UseExportActionsState => {
  const client = useApiClient();
  const [data, setData] = useState<ExportJobCreated | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  // Monotonic counter so only the latest requestExport() is allowed to commit.
  const requestIdRef = useRef(0);
  // Synchronous in-flight latch so a same-tick duplicate submit is dropped
  // before its POST fires (the state `loading` guard cannot see it in time).
  const inFlightRef = useRef(false);

  const requestExport = useCallback(
    (body: ExportRequestBody): Promise<ExportJobCreated | null> => {
      // FIX: drop a same-tick duplicate submit before the POST fires; a
      // double-click before re-render would otherwise pass the stale state
      // `loading` guard twice and create duplicate jobs + audit events.
      if (inFlightRef.current) return Promise.resolve(null);
      inFlightRef.current = true;
      // FIX: take the token from a ref incremented synchronously so two submits
      // in the same render get distinct tokens; the old `requestId + 1` read off
      // the render closure handed both the same token, letting a slow earlier
      // response overwrite a newer one.
      const token = ++requestIdRef.current;
      // FIX: clear any stale success result so a pending second request does
      // not show the previous job's RequestSuccess banner while loading.
      setData(null);
      setLoading(true);
      setError(null);
      return client
        .post<ExportJobCreated>("/exports", body)
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

  return { data, loading, error, requestExport };
};
