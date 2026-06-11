import { useCallback, useEffect, useRef } from "react";

import { useApiClient } from "@/lib/api/client";
import type { AuditSummaryResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// Hard upper bound on how long a single GET /audit/summary call is allowed
// to hang before the request is aborted. A stalled /audit/summary must not
// pin the Audit screen in a loading state forever — the tiles fall back to
// the honest `—` placeholder once the surface error surfaces through useAsync.
const REQUEST_TIMEOUT_MS = 30_000;

// ==========================================================================
// Purpose: Typed auto-fetch hook for the audit summary tiles. Fetches
//   GET /audit/summary on the production useApiClient and returns the shared
//   {data, loading, error, reload} async-state contract. The endpoint returns
//   tenant-scoped aggregate counts only (no per-row payload), so it is gated by
//   the SAME VIEW_AUDIT_LOG permission as GET /audit/events; window_hours
//   defaults to 24 on the backend, so the tile read sends no query params.
//   The in-flight request is bounded by REQUEST_TIMEOUT_MS via AbortController
//   and is aborted on component unmount so a stalled or rapidly unmounted
//   Audit screen cannot leak orphan requests into the browser's network stack.
// Database/ORM: None (frontend) — reads the backend audit aggregate endpoint.
// Standards: The request closure is memoized on `client` (stable ref) so
//   useAsync does NOT refetch on every render. The hook is self-auditing on
//   the backend (see backend audit route), but unlike GET /audit/events the
//   per-call self-audit row is recorded AFTER the snapshot is taken and
//   excluded from the count_summary WHERE filter, so the just-appended
//   AUDIT_LOG_VIEWED row cannot pollute the very counts this hook returns.
//   No client-side authorization is invented here — the backend
//   VIEW_AUDIT_LOG gate is authoritative; a 403 surfaces as the typed
//   ApiError for the view. Mirrors the events page's fail-closed pattern:
//   the consumer mounts this hook ONLY when the viewer may see the audit
//   log, so a restricted viewer fires no fetch.
// Blast Radius: Audit read only. No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> AuditSummaryResponse.
//   - File: backend/ums_smart_revenue/api/audit.py:123 get_audit_summary.
// ==========================================================================
export function useAuditSummary(): AsyncState<AuditSummaryResponse> { // skipcq: JS-0067
  const client = useApiClient();
  // Holds the in-flight request's AbortController so a reload can supersede
  // the prior request and an unmount can cancel it (no orphan fetch).
  const abortRef = useRef<AbortController | null>(null);

  const run = useCallback(async (): Promise<AuditSummaryResponse> => {
    // Supersede: a reload() that fires before the previous fetch resolved
    // must cancel the stale request so the new one can race it to state.
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    // Bounded request: a hanging network must not pin the Audit screen
    // in a loading state forever — abort and surface the error instead.
    const timer = setTimeout(
      () => controller.abort(),
      REQUEST_TIMEOUT_MS,
    );
    try {
      return await client.get<AuditSummaryResponse>("/audit/summary", {
        signal: controller.signal,
      });
    } finally {
      // Always clear the timer: a fast successful response would otherwise
      // leave a setTimeout dangling that aborts a later (post-unmount) call.
      clearTimeout(timer);
    }
  }, [client]);

  // Cancel any in-flight request when the component unmounts so a quick
  // navigation away from the Audit screen cannot leak an orphan fetch.
  useEffect(() => {
    return () => {
      abortRef.current?.abort();
      abortRef.current = null;
    };
  }, []);

  return useAsync(run);
}
