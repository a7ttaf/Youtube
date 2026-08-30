import { useQuery } from "@tanstack/react-query";

import { useApiClient } from "@/lib/api/client";
import type { SessionMe } from "@/lib/api/types";

export const SESSION_ME_QUERY_KEY = ["session", "me"] as const;

// ============================================================================
// Purpose: Expose the backend session contract through the shared query cache.
// Database/ORM: None; GET /session/me reads the trusted gateway principal.
// Standards: Disable retries for fail-closed bootstrap and allow callers to
//            disable the request when a seeded session is already authoritative.
// Blast Radius: Authorization (session identity and capability gating).
// Connections:
//   - File: frontend/src/contexts/SessionContext.tsx -> hydrates/fails from
//     this query rather than maintaining a divergent fetch path.
//   - File: backend/ums_smart_revenue/api/session.py -> GET /session/me.
// ============================================================================
/** Read the authoritative session once through TanStack Query. */
export const useSessionMeQuery = (enabled = true) => {
  const api = useApiClient();
  return useQuery({
    queryKey: SESSION_ME_QUERY_KEY,
    enabled,
    retry: false,
    queryFn: () => api.get<SessionMe>("/session/me"),
  });
};
