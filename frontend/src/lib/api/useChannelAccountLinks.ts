import { useCallback } from "react";

import { useApiClient } from "@/lib/api/client";
import type { AccountLinkProposalResponse } from "@/lib/api/types";

// Caller-facing input for proposing an account<->content-owner link. The fixed
// provenance fields (kind/payload) and the open-ended effective_month_end are
// NOT exposed here — they are constants the action hook injects so the Registry
// Assign panel can only ever PROPOSE an OPERATOR_ASSERTED, unverified link.
export type AccountLinkProposalInput = {
  adsenseAccountId: string;
  contentOwnerId: string;
  effectiveMonthStart: string; // "YYYY-MM"
  reason: string;
};

// ============================================================================
// Purpose: Imperative action hook for PROPOSING an AdSense-account <-> content-
//   owner link. Returns one stable callback that POSTs
//   /revenue/channel-account-links and resolves to the created (UNVERIFIED) link
//   + audit event; the Registry Assign panel invokes it from its submit flow and
//   reloads channels on success.
// Database/ORM: None (frontend) — calls the backend channel-account-link write
//   endpoint.
// Standards: provenance_kind is FIXED to "OPERATOR_ASSERTED" and provenance_payload
//   to {} here (not user-editable); effective_month_end is sent null (open-ended).
//   This hook only PROPOSES an unverified link — verification is the existing
//   dual-gated admin flow, not this path. Errors propagate as the typed ApiError
//   (403 = missing MANAGE_ORG_MAPPING@global, 409 = conflict, 422 = validation)
//   for the caller to translate to inline UI copy. No client-side authorization
//   is invented here; the backend MANAGE_ORG_MAPPING check stays the authority.
// Blast Radius: Account-link proposal (write path) — but only via the backend's
//   own guarded, audited propose route; this hook adds no client-side
//   authorization and never mutates finance numbers directly.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> useApiClient() POST + X-UMS-Tenant.
//   - File: frontend/src/lib/api/types.ts -> AccountLinkProposalResponse.
//   - File: backend/ums_smart_revenue/api/channel_account_links.py -> propose route.
// ============================================================================
export function useProposeAccountLinkAction(): ( // skipcq: JS-0067
  input: AccountLinkProposalInput,
) => Promise<AccountLinkProposalResponse> {
  const client = useApiClient();

  return useCallback(
    (input: AccountLinkProposalInput) =>
      client.post<AccountLinkProposalResponse>(
        "/revenue/channel-account-links",
        {
          adsense_account_id: input.adsenseAccountId,
          content_owner_id: input.contentOwnerId,
          effective_month_start: input.effectiveMonthStart,
          effective_month_end: null,
          provenance_kind: "OPERATOR_ASSERTED",
          provenance_payload: {},
          reason: input.reason,
        },
      ),
    [client],
  );
}
