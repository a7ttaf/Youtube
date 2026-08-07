import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ChannelGroupApiEntry } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// ============================================================================
// Purpose: Typed auto-fetch hook for the channel-group list. Fetches GET
//   /groups on mount via the production useApiClient and returns the shared
//   {data, loading, error, reload} async-state contract; the Groups view table
//   renders from this.
// Database/ORM: None (frontend) — reads the backend channel_groups registry.
// Standards: The request closure is memoized on `client` (stable ref) so
//   useAsync does NOT refetch on every render. The backend authorizes each
//   group per-member (VIEW_ANALYTICS over every channel_id, or global
//   VIEW_ANALYTICS for an empty group) and returns only the visible subset —
//   a 403 surfaces as a typed ApiError for the view to translate. No
//   client-side authorization is invented here.
// Blast Radius: Read-only. No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ChannelGroupApiEntry.
//   - File: backend/ums_smart_revenue/api/groups.py:185 list_groups.
// ============================================================================
export function useGroups(): AsyncState<ChannelGroupApiEntry[]> { // skipcq: JS-0067
  const client = useApiClient();
  const run = useCallback(
    () => client.get<ChannelGroupApiEntry[]>("/groups"),
    [client],
  );
  return useAsync(run);
}
