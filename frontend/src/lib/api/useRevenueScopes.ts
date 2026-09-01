import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { RevenueScopeOption, RevenueScopesResponse } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

const REVENUE_SCOPE_TYPES = new Set(["global", "sector", "company", "group"]);

// FIX: TypeScript response types do not validate JSON at runtime. Reject every
// malformed scope row here so an object with missing/falsey scope fields cannot
// reach useNetRevenue and silently omit its query parameters, which the backend
// would otherwise interpret as its global-scope defaults.
const isRevenueScopeOption = (value: unknown): value is RevenueScopeOption => {
  if (typeof value !== "object" || value === null) return false;
  const candidate = value as Record<string, unknown>;
  if (
    typeof candidate.scope_type !== "string" ||
    !REVENUE_SCOPE_TYPES.has(candidate.scope_type) ||
    typeof candidate.label !== "string" ||
    candidate.label.trim().length === 0
  ) {
    return false;
  }
  return candidate.scope_type === "global"
    ? candidate.scope_id === null
    : typeof candidate.scope_id === "string" && candidate.scope_id.trim().length > 0;
};

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
//   translate. A failed, malformed, or empty response authorizes nothing; the
//   caller withholds finance queries instead of inventing a global fallback.
// Blast Radius: Authorization (the selector's anti-scope-leak option source).
//   Read-only — no finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> RevenueScopeOption/RevenueScopesResponse.
//   - File: backend/ums_smart_revenue/api/revenue.py -> GET /revenue/scopes.
// ============================================================================
export const useRevenueScopes = (enabled = true): AsyncState<RevenueScopeOption[]> => {
  const client = useApiClient();
  const run = useCallback(
    async () => {
      const response = await client.get<RevenueScopesResponse>("/revenue/scopes");
      if (
        !Array.isArray(response.scopes) ||
        !response.scopes.every(isRevenueScopeOption)
      ) {
        // FIX: A malformed 200 envelope or entry is not a permission grant and
        // must not reach CommandView as trusted scope data.
        throw new Error("Authorized revenue scopes response was malformed.");
      }
      return response.scopes;
    },
    [client],
  );
  return useAsync(run, enabled);
};
