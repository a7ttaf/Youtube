import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ExportListResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type ExportsQuery = {
  // Page size + offset map straight to the backend's limit/offset query params
  // (1..100 / >=0). Both optional; the backend defaults to limit=50, offset=0.
  limit?: number;
  offset?: number;
};

// ============================================================================
// Purpose: Typed auto-fetch hook for the caller's export jobs. Builds the
//   GET /exports request (with optional limit/offset query params) on the
//   production useApiClient and returns the {data, loading, error, reload}
//   async-state contract every wired screen uses. `reload` is the refetch the
//   request form calls after a successful POST so the new QUEUED job appears.
// Database/ORM: None (frontend) — reads the backend export-job list endpoint.
// Standards: query params are URL-encoded; the request closure is memoized so
//   useAsync does not refetch on every render. The list response wraps
//   {items, pagination}; no money values are present on export-job rows.
// Blast Radius: None detected (read-only; the list endpoint already filters to
//   jobs the caller is authorized to access — no client-side authz invented).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ExportListResponse contract.
//   - File: backend/ums_smart_revenue/api/exports.py:287 list_exports.
// ============================================================================
export function useExports( // skipcq: JS-0067
  query: ExportsQuery = {},
): AsyncState<ExportListResponse> {
  const client = useApiClient();
  const { limit, offset } = query;

  const run = useCallback(() => {
    const params = new URLSearchParams();
    if (limit != null) params.set("limit", String(limit));
    if (offset != null) params.set("offset", String(offset));
    const qs = params.toString();
    return client.get<ExportListResponse>(`/exports${qs ? `?${qs}` : ""}`);
  }, [client, limit, offset]);

  return useAsync(run);
}
