import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ConnectorRunListResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type ConnectorRunsQuery = {
  // Optional equality filters mapped straight to the backend query params. All
  // optional; the backend defaults to limit=50 and no filters.
  connector_key?: string;
  account_id?: string;
  // Cursor pagination: started_at + id must be supplied TOGETHER (the backend
  // 422s on a half-cursor), so the hook only appends them when BOTH are present.
  cursor_started_at?: string;
  cursor_id?: string;
  limit?: number;
};

/** Append a URLSearchParams entry only when the value is defined and non-empty. */
const appendParam = (
  params: URLSearchParams,
  key: string,
  value: string | number | undefined,
): void => {
  if (value != null && value !== "") params.append(key, String(value));
};

/**
 * Build the GET /connectors/runs URL with optional filter and pagination params.
 * Cursor params are both-or-neither — a half-cursor 422s on the backend, so the
 * (cursor_started_at, cursor_id) pair is appended only when BOTH are present.
 */
export const buildConnectorRunsUrl = (
  connector_key?: string,
  account_id?: string,
  cursor_started_at?: string,
  cursor_id?: string,
  limit?: number,
): string => {
  const params = new URLSearchParams();
  appendParam(params, "connector_key", connector_key);
  appendParam(params, "account_id", account_id);
  appendParam(params, "limit", limit);
  if (cursor_started_at != null && cursor_id != null) {
    params.set("cursor_started_at", cursor_started_at);
    params.set("cursor_id", cursor_id);
  }
  const qs = params.toString();
  return qs ? `/connectors/runs?${qs}` : "/connectors/runs";
};

// ============================================================================
// Purpose: Typed auto-fetch hook for the connector run-history log. Builds GET
//   /connectors/runs (with optional connector_key/account_id filters, CURSOR
//   pagination, and limit) on the production useApiClient and returns the shared
//   {data, loading, error, reload} async-state contract.
// Database/ORM: None (frontend) — reads the backend connector-run page.
// Standards: query params are URL-encoded; the request closure is memoized on
//   the primitive query fields so useAsync does NOT refetch on every render. The
//   hook fetches once per mount / filter change and never polls. Pagination is
//   CURSOR-based (next_cursor.{started_at, id}); the cursor params are sent
//   both-or-neither (via buildConnectorRunsUrl) so a half-cursor never 422s. No
//   client-side authorization is invented — the backend VIEW_CONNECTOR_HEALTH
//   gate is authoritative; a 403 surfaces as the typed ApiError for the view to
//   translate. This read does NOT self-audit (operational metadata, consistent
//   with the credential-list route).
// Blast Radius: Connector operational-metadata read only. No finance number, no
//   source-of-truth mutation. No graph projection impact detected.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ConnectorRunListResponse.
//   - File: backend/ums_smart_revenue/api/connectors.py -> list_connector_runs.
// ============================================================================
export const useConnectorRuns = (
  query: ConnectorRunsQuery = {},
): AsyncState<ConnectorRunListResponse> => {
  const client = useApiClient();
  const { connector_key, account_id, cursor_started_at, cursor_id, limit } = query;

  const run = useCallback(
    () =>
      client.get<ConnectorRunListResponse>(
        buildConnectorRunsUrl(
          connector_key,
          account_id,
          cursor_started_at,
          cursor_id,
          limit,
        ),
      ),
    [client, connector_key, account_id, cursor_started_at, cursor_id, limit],
  );

  return useAsync(run);
};
