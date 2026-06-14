import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type {
  ConnectorCredentialHealth,
  ConnectorCredentialHealthResponse,
} from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

export type ConnectorCredentialHealthQuery = {
  // Page size + offset map straight to the backend's limit/offset query params
  // (1..100 / >=0). Both optional; the backend defaults to limit=50, offset=0.
  limit?: number;
  offset?: number;
};

// ============================================================================
// Purpose: Typed auto-fetch hook for the read-only connector credential HEALTH
//   surface. Builds GET /connectors/credentials/health (with optional
//   limit/offset) on the production useApiClient and returns the shared
//   {data, loading, error, reload} async-state contract, UNWRAPPING the backend
//   {credentials: [...]} envelope to the credential array. Each row carries the
//   four persisted OAuth refresh-telemetry fields plus the server-derived
//   health_state; the secret itself is never returned (only has_secret_ref).
// Database/ORM: None (frontend) — reads the backend credential-health page.
// Standards: query params are URL-encoded; the request closure is memoized so
//   useAsync does NOT refetch on every render and never polls. No client-side
//   authorization is invented — the backend VIEW_CONNECTOR_HEALTH gate is
//   authoritative; a 403 surfaces as the typed ApiError for the view to degrade
//   (the view fail-closes and renders nothing without the capability, so this
//   hook is only mounted when permitted). health_state is server-derived and
//   never recomputed client-side. This read does NOT self-audit (operational
//   metadata, consistent with the credential-list + run-history routes).
// Blast Radius: Connector operational-metadata read only. No finance number, no
//   source-of-truth mutation. No graph projection impact detected.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ConnectorCredentialHealthResponse.
//   - File: backend/ums_smart_revenue/api/connectors.py
//       list_connector_credential_health() -> {credentials: [...]}.
// ============================================================================
/**
 * Typed auto-fetch hook for the read-only connector credential health surface.
 *
 * @param query - Optional pagination: limit (1..100) and offset (>=0).
 * @returns Shared async state with the credential health array.
 */
export function useConnectorCredentialHealth( // skipcq: JS-0067
  query: ConnectorCredentialHealthQuery = {},
): AsyncState<ConnectorCredentialHealth[]> {
  const client = useApiClient();
  const { limit, offset } = query;

  const run = useCallback(() => {
    const params = new URLSearchParams();
    if (limit !== undefined && limit !== null) {
      params.set("limit", String(limit));
    }
    if (offset !== undefined && offset !== null) {
      params.set("offset", String(offset));
    }
    const qs = params.toString();
    return client
      .get<ConnectorCredentialHealthResponse>(
        `/connectors/credentials/health${qs ? `?${qs}` : ""}`,
      )
      .then((response) => response.credentials);
  }, [client, limit, offset]);

  return useAsync(run);
}
