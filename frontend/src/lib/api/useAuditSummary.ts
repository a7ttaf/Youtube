import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { AuditSummaryResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// ============================================================================
// Purpose: Typed auto-fetch hook for the audit summary tiles. Fetches
//   GET /audit/summary on the production useApiClient and returns the shared
//   {data, loading, error, reload} async-state contract. The endpoint returns
//   tenant-scoped aggregate counts only (no per-row payload), so it is gated by
//   the SAME VIEW_AUDIT_LOG permission as GET /audit/events; window_hours
//   defaults to 24 on the backend, so the tile read sends no query params.
// Database/ORM: None (frontend) — reads the backend audit aggregate endpoint.
// Standards: The request closure is memoized on `client` (stable ref) so
//   useAsync does NOT refetch on every render. Unlike GET /audit/events, this
//   aggregate does NOT write an audit row (no self-audit), so there is no
//   double-fetch hazard beyond useAsync's own StrictMode dedupe. No client-side
//   authorization is invented here — the backend VIEW_AUDIT_LOG gate is
//   authoritative; a 403 surfaces as the typed ApiError for the view. Mirrors
//   the events page's fail-closed pattern: the consumer mounts this hook ONLY
//   when the viewer may see the audit log, so a restricted viewer fires no fetch.
// Blast Radius: Audit read only. No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> AuditSummaryResponse.
//   - File: backend/ums_smart_revenue/api/audit.py:123 get_audit_summary.
// ============================================================================
export function useAuditSummary(): AsyncState<AuditSummaryResponse> { // skipcq: JS-0067
  const client = useApiClient();
  const run = useCallback(
    () => client.get<AuditSummaryResponse>("/audit/summary"),
    [client],
  );
  return useAsync(run);
}
