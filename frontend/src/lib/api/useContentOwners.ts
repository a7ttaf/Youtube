import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ContentOwnersResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// ============================================================================
// Purpose: Typed auto-fetch hook for the group-sync content-owner picker.
//   Builds GET /connectors/content-owners?connector_key=… on the production
//   useApiClient and returns the shared {data, loading, error, reload}
//   async-state contract. This is the LEAST-PRIVILEGE read surface for the
//   Groups sync flow: the backend gates it on MANAGE_GROUPS (the same
//   permission the sync write itself requires) and returns only ACTIVE
//   credential account_id strings — so a group manager without
//   MANAGE_CONNECTORS can populate the picker (Qodo PR #174 correctness fix),
//   and no credential metadata beyond the owner id ever reaches the browser.
// Database/ORM: None (frontend) — reads the backend connector-credential
//   account ids through the dedicated route.
// Standards: The request closure is memoized on `client` + `connectorKey` so
//   useAsync does NOT refetch on every render. The connector key is URL-encoded
//   via URLSearchParams. No client-side authorization is invented: a 403
//   (missing MANAGE_GROUPS) or 422 (unknown connector key) surfaces as the
//   typed ApiError for the caller to translate.
// Blast Radius: None detected (read-only picker metadata).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ContentOwnersResponse.
//   - File: frontend/src/components/srcc/views/GroupsView.tsx -> the picker.
//   - File: backend/ums_smart_revenue/api/connectors.py -> list_content_owners.
// ============================================================================
export const useContentOwners = (
  connectorKey: string,
): AsyncState<ContentOwnersResponse> => {
  const client = useApiClient();
  const run = useCallback(() => {
    const params = new URLSearchParams({ connector_key: connectorKey });
    return client.get<ContentOwnersResponse>(`/connectors/content-owners?${params}`);
  }, [client, connectorKey]);
  return useAsync(run);
};
