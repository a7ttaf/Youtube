import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ChannelRegistryEntry } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// ============================================================================
// Purpose: Typed auto-fetch hook for the channel registry list. Fetches
//   GET /channels on mount via the production useApiClient and returns the
//   shared {data, loading, error, reload} async-state contract.
// Database/ORM: None (frontend) — reads the backend channel registry.
// Standards: The request closure is memoized on `client` (stable ref) so
//   useAsync does NOT refetch on every render. The backend requires
//   VIEW_ANALYTICS permission; a 403 surfaces as a typed ApiError for the
//   view to translate. No client-side authorization is invented here.
// Blast Radius: Read-only. No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ChannelRegistryEntry.
//   - File: backend/ums_smart_revenue/api/channels.py -> list_channels.
// ============================================================================
export const useChannels = (): AsyncState<ChannelRegistryEntry[]> => {
  const client = useApiClient();
  const run = useCallback(
    () => client.get<ChannelRegistryEntry[]>("/channels"),
    [client],
  );
  return useAsync(run);
};
