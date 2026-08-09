import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ChannelMappingResponse } from "@/lib/api/types";

// ============================================================================
// Purpose: Imperative action hook for changing a channel's primary-company
//   mapping. Returns one stable callback that PATCHes
//   /channels/{id}/mapping with {primary_company_id, reason} and resolves to the
//   updated mapping + audit event; the Registry Map panel invokes it from its
//   submit flow and reloads channels on success.
// Database/ORM: None (frontend) — calls the backend mapping write endpoint.
// Standards: channelId is path-encoded; reason is sent in the JSON body (backend
//   requires a non-blank reason). Errors propagate as the typed ApiError (403 =
//   missing MANAGE_ORG_MAPPING incl. the unmapped-channel auth dead-zone, 404 =
//   unknown channel, 409 = conflict, 422 = validation) for the caller to
//   translate to inline UI copy. No client-side authorization is invented here;
//   the backend dual MANAGE_ORG_MAPPING check stays the authority.
// Blast Radius: Org mapping (write path) — but only via the backend's own
//   guarded, audited mapping route; this hook adds no client-side authorization
//   and never mutates finance numbers directly.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() PATCH + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> ChannelMappingResponse.
//   - File: backend/ums_smart_revenue/api/channels.py -> update_channel_mapping.
// ============================================================================
export const useChannelMappingAction = (): ((
  channelId: string,
  primaryCompanyId: string,
  reason: string,
) => Promise<ChannelMappingResponse>) => {
  const client = useApiClient();

  return useCallback(
    (channelId: string, primaryCompanyId: string, reason: string) =>
      client.patch<ChannelMappingResponse>(
        `/channels/${encodeURIComponent(channelId)}/mapping`,
        { primary_company_id: primaryCompanyId, reason },
      ),
    [client],
  );
};
