import { ApiError } from "@/lib/api/client";

// ============================================================================
// Purpose: Map an API failure to operator-facing copy WITHOUT leaking backend
//   internals. Only a fixed set of statuses whose FastAPI `detail` strings are
//   CANNED operator messages by server-side contract (validation, permission,
//   not-found, conflict, and the sync route's canned credential 503) may show
//   their detail verbatim. Everything else — 5xx diagnostics, proxy/upstream
//   HTML, non-string detail payloads (e.g. FastAPI's 422 array shape when a
//   route forgets the canned contract), and raw client-side exception text —
//   collapses to a generic fallback plus the numeric status, so stack traces,
//   hostnames, tokens, and SQL fragments can never reach the UI (Qodo rule
//   1052052: no sensitive details in client-visible error copy).
// Database/ORM: None (frontend error-copy mapping only).
// Standards: No backend string is rendered unless it arrives on a canned-copy
//   status as a plain string. The raw err.message of a non-ApiError (network
//   failures, TypeErrors) is NEVER rendered — it is meaningless to operators
//   and uncontrolled as a leak surface.
// Blast Radius: UI error text only; no request behavior, no authorization, no
//   finance numbers.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> ApiError (status + parsed body).
//   - File: frontend/src/components/srcc/views/GroupsView.tsx -> load/mutation
//     error copy.
//   - File: frontend/src/components/srcc/views/GroupsSyncFlow.tsx -> sync error
//     copy (appends its 503 Connectors pointer on top of this).
// ============================================================================

// Statuses whose backend `detail` strings are canned operator-facing copy by
// server-side contract: boundary validation (400/422), permission and tenancy
// refusals (403/404), domain conflicts (409), and the sync route's canned
// credential-unavailable message (503). Anything not on this list is treated
// as internal diagnostics and is never rendered verbatim.
const OPERATOR_DETAIL_STATUSES: ReadonlySet<number> = new Set([
  400, 403, 404, 409, 422, 503,
]);

/**
 * Return the backend's canned detail for operator-copy statuses, otherwise a
 * generic fallback. `fallback` is a full clause WITHOUT a trailing period; the
 * numeric status is appended for diagnosability (e.g. "… (HTTP 500).").
 */
export const describeApiError = (err: unknown, fallback: string): string => {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | null;
    if (
      typeof body?.detail === "string" &&
      OPERATOR_DETAIL_STATUSES.has(err.status)
    ) {
      return body.detail;
    }
    return `${fallback} (HTTP ${err.status}).`;
  }
  return `${fallback}.`;
};
