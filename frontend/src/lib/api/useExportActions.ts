import { useCallback, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type { ExportJobCreated, ExportRequestBody } from "@/lib/api/types";

export type UseExportActionsState = {
  // The most recently created job (cleared while a new request is in flight).
  data: ExportJobCreated | null;
  loading: boolean;
  error: ApiError | Error | null;
  // Trigger the POST on user action; resolves with the created job (or rejects
  // with the typed error already captured in `error`).
  requestExport: (body: ExportRequestBody) => Promise<ExportJobCreated>;
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
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ExportRequestBody / ExportJobCreated.
//   - File: backend/ums_smart_revenue/api/exports.py:173 request_export.
// ============================================================================
export function useExportActions(): UseExportActionsState {
  const client = useApiClient();
  const [data, setData] = useState<ExportJobCreated | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<ApiError | Error | null>(null);
  // Monotonic token so only the latest requestExport() is allowed to commit.
  const [requestId, setRequestId] = useState(0);

  const requestExport = useCallback(
    (body: ExportRequestBody): Promise<ExportJobCreated> => {
      const token = requestId + 1;
      setRequestId(token);
      setLoading(true);
      setError(null);
      return client
        .post<ExportJobCreated>("/exports", body)
        .then((result) => {
          // Supersede: ignore a stale submit whose request changed mid-flight.
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

  return { data, loading, error, requestExport };
}
