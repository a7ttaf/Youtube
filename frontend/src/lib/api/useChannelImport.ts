import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { ChannelImportResult } from "@/lib/api/types";

// ============================================================================
// Purpose: Imperative action hook for the Registry CSV import stepper: one
//   stable callback POSTing a roster CSV to /channels/import as multipart form
//   data, serving both the dry-run preview and the apply (the dry_run flag is
//   the only difference). camelCase hook args map to the backend's snake_case
//   wire form fields at this boundary — the deliberate frontend/backend casing
//   seam.
// Database/ORM: None (frontend) — calls the backend import endpoint.
// Standards: The FormData carries `file`, `content_owner_id`, `dry_run`
//   ("true"/"false"), and `reason`; `cms_status` is OMITTED so the backend
//   default (INSIDE_CMS) applies. useApiClient passes FormData through
//   verbatim (isRawBodyInit) with no JSON Content-Type, so fetch sets the
//   multipart boundary itself. Errors propagate as the typed ApiError (403 =
//   missing MANAGE_CHANNELS, or missing MANAGE_GROUPS on a Group_ID-bearing
//   roster; 422 = malformed upload/form OR an apply attempted while the plan
//   holds ERROR rows — that 422's `detail` is the full ChannelImportResult
//   payload; 409 = a plan-to-apply race or locked-month rejection) for the
//   calling view to translate. No client-side authorization is invented here;
//   the backend's own guarded, audited route stays the authority. This hook
//   adds NO error handling of its own — the calling view owns busy/error
//   presentation (the GroupsSyncFlow pattern).
// Blast Radius: Channel-registry inventory + row-created group membership
//   (write path) — but only via the backend's own guarded, audited route. No
//   revenue math, no allocation, no month-close (the backend's locked-month
//   guard rejects revenue_required flips at apply time).
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST +
//       X-UMS-Tenant; withJsonBody passes FormData through untouched.
//   - File: frontend/src/lib/api/types.ts -> ChannelImportResult.
//   - File: backend/ums_smart_revenue/api/channels.py:639 import_channels
//       -> POST /channels/import.
// ============================================================================

export const useChannelImport = (): ((
  args: {
    file: File;
    contentOwnerId: string;
    dryRun: boolean;
    reason: string;
    /**
     * The `plan_fingerprint` of the dry run the operator approved. The apply
     * re-plans from CURRENT state, so sending it binds the write to the plan
     * that was actually reviewed: the backend 409s on divergence and returns
     * the refreshed plan. Omitted on the dry run itself (nothing to bind to).
     */
    expectedPlanFingerprint?: string;
  },
) => Promise<ChannelImportResult>) => {
  const client = useApiClient();
  return useCallback(
    ({ file, contentOwnerId, dryRun, reason, expectedPlanFingerprint }) => {
      const form = new FormData();
      form.append("file", file);
      form.append("content_owner_id", contentOwnerId);
      form.append("dry_run", dryRun ? "true" : "false");
      form.append("reason", reason);
      if (expectedPlanFingerprint !== undefined) {
        form.append("expected_plan_fingerprint", expectedPlanFingerprint);
      }
      return client.post<ChannelImportResult>("/channels/import", form);
    },
    [client],
  );
};
