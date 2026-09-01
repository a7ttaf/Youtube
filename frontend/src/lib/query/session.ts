import { useQuery } from "@tanstack/react-query";
import { useState } from "react";

import { useTenant } from "@/contexts/TenantContext";
import { useApiClient } from "@/lib/api/client";
import type { SessionMe } from "@/lib/api/types";

export const SESSION_ME_QUERY_KEY = ["session", "me"] as const;

export type SessionQueryScope = number;

let nextSessionQueryScope = 0;

// ============================================================================
// Purpose: Allocate a process-local namespace for one authenticated provider
//          lifetime so a remounted/login-boundary session cannot reuse a prior
//          principal's query entry.
// Database/ORM: None (frontend query-cache identity only).
// Standards: The counter is opaque and never an authorization claim; it only
//            separates cache entries. A provider cleanup removes its entry.
// Blast Radius: Authorization cache isolation; no API or finance mutation.
// Connections:
//   - File: frontend/src/contexts/SessionContext.tsx -> owns one scope per
//     SessionProvider lifetime and clears it on explicit session reset.
//   - File: frontend/tests/lib/query/session.test.tsx -> pins cross-principal
//     and cross-tenant cache isolation.
// ============================================================================
/** Allocate an opaque cache namespace for one session-provider lifetime. */
export const createSessionQueryScope = (): SessionQueryScope => {
  nextSessionQueryScope += 1;
  return nextSessionQueryScope;
};

/** Build the exact cache key for one authenticated provider lifetime. */
export const sessionMeQueryKey = (
  scope: SessionQueryScope,
  tenantSlug: string,
): readonly ["session", "me", SessionQueryScope, string] => [
  ...SESSION_ME_QUERY_KEY,
  scope,
  tenantSlug,
];

// ============================================================================
// Purpose: Expose the backend session contract through the shared query cache.
// Database/ORM: None; GET /session/me reads the trusted gateway principal.
// Standards: Namespace cache data by provider lifetime and tenant, override the
//            application stale window, disable retries, and revalidate on a
//            production focus/reconnect auth boundary. A seeded authoritative
//            test/storybook session may still disable the request.
// Blast Radius: Authorization (session identity and capability gating).
// Connections:
//   - File: frontend/src/contexts/SessionContext.tsx -> hydrates/fails from
//     this query rather than maintaining a divergent fetch path.
//   - File: backend/ums_smart_revenue/api/session.py -> GET /session/me.
// ============================================================================
/** Read and boundary-revalidate the authoritative session through TanStack Query. */
export const useSessionMeQuery = (
  enabled = true,
  scope?: SessionQueryScope,
) => {
  const { tenantSlug } = useTenant();
  const api = useApiClient();
  // Direct hook consumers still receive a fresh boundary. The provider passes
  // its stable scope so AppShell route remounts can reuse only that provider's
  // own authenticated result.
  const [localScope] = useState(createSessionQueryScope);
  const queryScope = scope ?? localScope;
  return useQuery({
    // FIX: The former constant key could synchronously replay a still-fresh
    // principal after remount/login. Scope + tenant make that cache unreachable.
    queryKey: sessionMeQueryKey(queryScope, tenantSlug),
    enabled,
    // Never inherit main.tsx's 30-second freshness window for identity data.
    // The opaque scope prevents reuse across provider lifetimes; zero GC drops
    // the scoped entry as soon as its final consumer unmounts.
    staleTime: 0,
    gcTime: 0,
    // A route transition may remount the bootstrap consumer. Reusing the result
    // is safe inside one opaque provider scope; a new provider scope always
    // fetches afresh. Focus/reconnect are different: a gateway login may have
    // changed while the SPA stayed mounted, so revalidate and let
    // useSessionBootstrap hide the prior principal until that request settles.
    refetchOnMount: false,
    refetchOnWindowFocus: "always",
    refetchOnReconnect: "always",
    retry: false,
    queryFn: () => api.get<SessionMe>("/session/me"),
  });
};
