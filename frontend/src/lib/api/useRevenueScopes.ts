import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { RevenueScopeOption, RevenueScopesResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// ============================================================================
// Purpose: Typed auto-fetch hook for the viewer's authorized rollup scope
//   options. Fetches GET /revenue/scopes on mount via the production
//   useApiClient and returns the shared {data, loading, error, reload}
//   async-state contract, unwrapping the response envelope to the .scopes array.
//   The Command Center calls this once at its root to populate the scope
//   selector with ONLY the scopes the viewer is VIEW_REVENUE-authorized for, so
//   the selector cannot offer an out-of-scope org unit (org-structure leak) or a
//   dead option that 403s on the rollup read.
// Database/ORM: None (frontend) — reads the backend authorized-scopes listing.
// Standards: The request closure is memoized on `client` (stable ref) so
//   useAsync does NOT refetch on every render. The backend requires an active
//   VIEW_REVENUE grant; a 403 surfaces as a typed ApiError for the view to
//   translate (it degrades to a guaranteed global-only fallback, never an
//   invented scope). No client-side authorization is invented here.
// Blast Radius: Authorization (the selector's anti-scope-leak option source).
//   Read-only — no finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> RevenueScopeOption/RevenueScopesResponse.
//   - File: backend/ums_smart_revenue/api/revenue.py -> GET /revenue/scopes.
// ============================================================================
export const useRevenueScopes = (): AsyncState<RevenueScopeOption[]> => {
  const client = useApiClient();
  const run = useCallback(
    async () => {
      const response = await client.get<RevenueScopesResponse>("/revenue/scopes");
      return response.scopes;
    },
    [client],
  );
  return useAsync(run);
};
