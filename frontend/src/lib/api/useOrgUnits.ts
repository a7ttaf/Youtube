import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { OrgUnit } from "@/lib/api/types";
import { useAsync, type AsyncState } from "@/lib/api/useAsync";

// ============================================================================
// Purpose: Typed auto-fetch hook for the active org-unit list. Fetches
//   GET /org-units on mount via the production useApiClient and returns the
//   shared {data, loading, error, reload} async-state contract. The Registry
//   view uses this once at its root to resolve Company/Sector NAMES from the
//   org-unit ids already on each channel (primary_company_id IS the org-unit
//   UUID; parent_id walks one hop up to the Sector).
// Database/ORM: None (frontend) — reads the backend org-units listing.
// Standards: The request closure is memoized on `client` (stable ref) so
//   useAsync does NOT refetch on every render. The backend requires
//   VIEW_ANALYTICS permission; a 403 surfaces as a typed ApiError for the view
//   to translate (it degrades to raw ids, never an invented name). No
//   client-side authorization is invented here.
// Blast Radius: Read-only. No finance number, no source-of-truth mutation.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() GET + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> OrgUnit.
//   - File: backend/ums_smart_revenue/api/org_units.py -> GET /org-units.
// ============================================================================
export const useOrgUnits = (): AsyncState<OrgUnit[]> => {
  const client = useApiClient();
  const run = useCallback(
    () => client.get<OrgUnit[]>("/org-units"),
    [client],
  );
  return useAsync(run);
};
