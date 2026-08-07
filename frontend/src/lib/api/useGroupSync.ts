import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type {
  ClearOwnerStampResponse,
  GroupSyncResult,
  GroupUpdateResponse,
} from "@/lib/api/types";

// ============================================================================
// Purpose: Imperative action hooks for the three channel-group mutations the
//   Groups view triggers: CMS group sync (preview/apply), clearing a
//   mis-stamped group's content-owner stamp, and archiving (active toggle) a
//   group. Each returns one stable callback over the production useApiClient;
//   camelCase hook args map to the backend's snake_case wire bodies at this
//   boundary — the deliberate frontend/backend casing seam.
// Database/ORM: None (frontend) — calls the backend group-write endpoints.
// Standards: groupId path segments are encodeURIComponent'd, as is the DELETE
//   route's `reason` query param. Errors propagate as the typed ApiError (403 =
//   missing MANAGE_GROUPS, 404 = unknown group, 409 = conflict / synced-group
//   edit / no-owner-stamp-to-clear, 422 = validation, 502/503 = CMS fetch or
//   credential failure on sync) for the caller to translate to inline UI copy.
//   No client-side authorization is invented here; the backend's own guarded,
//   audited routes stay the authority. These hooks add NO error handling of
//   their own — the calling view owns presentation.
// Blast Radius: Channel-group membership/ownership/active-state (write path) —
//   but only via the backend's own guarded, audited routes. No revenue math,
//   no allocation, no month-close.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST/DELETE/PATCH
//       + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> GroupSyncResult,
//       ClearOwnerStampResponse, GroupUpdateResponse.
//   - File: backend/ums_smart_revenue/api/channels.py:926 sync_channel_groups
//       -> POST /channels/groups/sync.
//   - File: backend/ums_smart_revenue/api/groups.py:395
//       clear_group_content_owner -> DELETE /groups/{id}/content-owner.
//   - File: backend/ums_smart_revenue/api/groups.py:245
//       update_group -> PATCH /groups/{id}.
// ============================================================================

export function useGroupSyncAction(): (
  args: {
    contentOwnerId: string;
    dryRun: boolean;
    reason: string;
  },
) => Promise<GroupSyncResult> {
  const client = useApiClient();
  return useCallback(
    ({ contentOwnerId, dryRun, reason }) =>
      client.post<GroupSyncResult>("/channels/groups/sync", {
        content_owner_id: contentOwnerId,
        dry_run: dryRun,
        reason,
      }),
    [client],
  );
}

export function useClearOwnerStampAction(): (
  args: {
    groupId: string;
    reason: string;
  },
) => Promise<ClearOwnerStampResponse> {
  const client = useApiClient();
  return useCallback(
    ({ groupId, reason }) =>
      client.delete<ClearOwnerStampResponse>(
        `/groups/${encodeURIComponent(groupId)}/content-owner?reason=${encodeURIComponent(reason)}`,
      ),
    [client],
  );
}

export function useGroupArchiveAction(): (
  args: {
    groupId: string;
    active: boolean;
    reason: string;
  },
) => Promise<GroupUpdateResponse> {
  const client = useApiClient();
  return useCallback(
    ({ groupId, active, reason }) =>
      client.patch<GroupUpdateResponse>(
        `/groups/${encodeURIComponent(groupId)}`,
        { active, reason },
      ),
    [client],
  );
}
