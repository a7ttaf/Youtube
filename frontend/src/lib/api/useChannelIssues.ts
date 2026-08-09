import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ChannelIssuesResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// ============================================================================
// Purpose: Typed auto-fetch hook for the channel-issues monitor. Fetches
//   GET /channels/issues on mount via the production useApiClient and returns
//   the shared {data, loading, error, reload} async-state contract.
// Database/ORM: None (frontend) — reads the backend analytics monitor endpoint.
// Standards: The request closure is memoized on `client` (stable ref) so
//   useAsync does NOT refetch on every render. The backend requires
//   VIEW_ANALYTICS permission (scope-filtered); a 403 surfaces as a typed
//   ApiError the panel renders as a denied state — it must NEVER be masked as
//   "no issues". No client-side authorization is invented here.
// Blast Radius: Read-only. No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ChannelIssuesResponse.
//   - File: backend/ums_smart_revenue/api/channels.py -> list_channel_issues.
// ============================================================================
export const useChannelIssues = (): AsyncState<ChannelIssuesResponse> => {
  const client = useApiClient();
  const run = useCallback(
    () => client.get<ChannelIssuesResponse>("/channels/issues"),
    [client],
  );
  return useAsync(run);
};
