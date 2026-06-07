import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { AuditEventListResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type AuditEventsQuery = {
  // Optional filters mapped straight to the backend query params. All optional;
  // the backend defaults to limit=50 and no filters.
  event_type?: string;
  entity_type?: string;
  entity_id?: string;
  // Cursor pagination: created_at + id must be supplied TOGETHER (the backend
  // 422s on a half-cursor), so the hook only appends them when BOTH are present.
  cursor_created_at?: string;
  cursor_id?: string;
  limit?: number;
};

/** Append a URLSearchParams entry only when the value is defined. */
function appendParam(params: URLSearchParams, key: string, value: string | number | undefined): void { // skipcq: JS-0067
  if (value != null) params.append(key, String(value));
}

/** Build a shared URLSearchParams object for /audit/events and /audit/events/export. */
function buildAuditEventSearchParams(
  event_type: string | undefined,
  entity_type: string | undefined,
  entity_id: string | undefined,
  cursor_created_at: string | undefined,
  cursor_id: string | undefined,
  limit: number | undefined,
): URLSearchParams {
  const params = new URLSearchParams();
  appendParam(params, "event_type", event_type);
  appendParam(params, "entity_type", entity_type);
  appendParam(params, "entity_id", entity_id);
  appendParam(params, "limit", limit);
  if (cursor_created_at != null && cursor_id != null) {
    params.set("cursor_created_at", cursor_created_at);
    params.set("cursor_id", cursor_id);
  }
  return params;
}

/**
 * Build the GET /audit/events URL with optional filter and pagination params.
 * Cursor params are both-or-neither — a half-cursor 422s on the backend.
 * Extracted as reusable helper so /audit/events/export can share the exact
 * filter behavior.
 */
export function buildAuditEventsUrl(
  event_type: string | undefined,
  entity_type: string | undefined,
  entity_id: string | undefined,
  cursor_created_at: string | undefined,
  cursor_id: string | undefined,
  limit: number | undefined,
): string {
  const params = buildAuditEventSearchParams(
    event_type,
    entity_type,
    entity_id,
    cursor_created_at,
    cursor_id,
    limit,
  );
  const qs = params.toString();
  return qs ? `/audit/events?${qs}` : "/audit/events";
}

/**
 * Build the GET /audit/events/export URL with the same event/type/entity filters
 * currently supported on /audit/events (no cursor). This is used by the audit
 * view download action.
 */
export function buildAuditEventsExportUrl( // skipcq: JS-0067
  event_type: string | undefined,
  entity_type: string | undefined,
  entity_id: string | undefined,
): string {
  const params = buildAuditEventSearchParams(
    event_type,
    entity_type,
    entity_id,
    undefined,
    undefined,
    undefined,
  );
  const qs = params.toString();
  return qs ? `/audit/events/export?${qs}` : "/audit/events/export";
}

// ============================================================================
// Purpose: Typed auto-fetch hook for the audit-event log. Builds GET
//   /audit/events (with optional event_type/entity_type/entity_id filters,
//   CURSOR pagination, and limit) on the production useApiClient and returns the
//   shared {data, loading, error, reload} async-state contract.
// Database/ORM: None (frontend) — reads the backend audit-event page.
// Standards: query params are URL-encoded; the request closure is memoized on the
//   primitive query fields so useAsync does NOT refetch on every render. This
//   matters here because each /audit/events call itself writes one
//   AUDIT_LOG_VIEWED row server-side — an un-memoized closure would refetch every
//   render and self-audit in a loop. The hook fetches once per mount / filter
//   change and never polls. Pagination is CURSOR-based (next_cursor.{created_at,
//   id}), distinct from the offset PaginationMeta the other lists use; the cursor
//   params are sent both-or-neither (via buildAuditEventsUrl) so a half-cursor
//   never 422s. No client-side authorization is invented — the backend
//   VIEW_AUDIT_LOG gate (and the separate sensitive-payload gate that drives
//   redaction) is authoritative; a 403 surfaces as the typed ApiError for the
//   view to translate.
// Blast Radius: Audit read (each call self-audits server-side via the backend's
//   own guarded route). No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> AuditEventListResponse.
//   - File: backend/ums_smart_revenue/api/audit.py:85 list_audit_events.
// ============================================================================
export function useAuditEvents( // skipcq: JS-0067
  query: AuditEventsQuery = {},
): AsyncState<AuditEventListResponse> {
  const client = useApiClient();
  const {
    event_type,
    entity_type,
    entity_id,
    cursor_created_at,
    cursor_id,
    limit,
  } = query;

  const run = useCallback(
    () =>
      client.get<AuditEventListResponse>(
        buildAuditEventsUrl(
          event_type,
          entity_type,
          entity_id,
          cursor_created_at,
          cursor_id,
          limit,
        ),
      ),
    [client, event_type, entity_type, entity_id, cursor_created_at, cursor_id, limit],
  );

  return useAsync(run);
}
