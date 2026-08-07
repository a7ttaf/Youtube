import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ClearOwnerStampResponse,
  GroupSyncResult,
  GroupUpdateResponse,
} from "@/lib/api/types";
import {
  useClearOwnerStampAction,
  useGroupArchiveAction,
  useGroupSyncAction,
} from "@/lib/api/useGroupSync";
import { TenantProvider } from "@/contexts/TenantContext";

const wrapper = ({ children }: { children: React.ReactNode }) => {
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
};

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

const requireFetchArgs = () => {
  const args = fetchMock().mock.calls.at(-1);
  if (!args) throw new Error("expected fetch to have been called");
  return args;
};

const SYNC_RESULT: GroupSyncResult = {
  dry_run: true,
  content_owner_id: "COabc",
  counts: { CREATE: 1 },
  unknown_channel_total: 0,
  non_channel_member_count: 0,
  groups: [
    {
      cms_group_id: "deWjsA",
      outcome: "CREATE",
      title: "TV Sector",
      local_group_id: null,
      name_change: null,
      active_change: null,
      members_added: ["UCa"],
      members_removed: [],
      unknown_channel_ids: [],
      unknown_channel_count: 0,
      will_adopt_content_owner: true,
    },
  ],
};

const CLEAR_RESULT: ClearOwnerStampResponse = {
  id: "g1",
  name: "TV Sector",
  group_type: "CUSTOM",
  active: true,
  channel_ids: ["UCa"],
  cms_group_id: "deWjsA",
  content_owner_id: null,
  audit_event: {
    event_type: "GROUP_UPDATED",
    entity_type: "channel_group",
    entity_id: "g1",
    scope_type: "global",
    scope_id: null,
    reason: "wrong stamp",
    sensitive: false,
  },
};

const ARCHIVE_RESULT: GroupUpdateResponse = {
  id: "g1",
  name: "TV Sector",
  group_type: "CUSTOM",
  active: false,
  channel_ids: ["UCa"],
  cms_group_id: "deWjsA",
  content_owner_id: "COabc",
  audit_event: { event_type: "GROUP_UPDATED" },
};

describe("useGroupSyncAction", () => {
  it("POSTs /channels/groups/sync with the mapped body and resolves to the typed result", async () => {
    fetchMock().mockResolvedValue(jsonResponse(SYNC_RESULT));
    const { result } = renderHook(() => useGroupSyncAction(), { wrapper });

    const response = await result.current({
      contentOwnerId: "COabc",
      dryRun: true,
      reason: "monthly check",
    });

    const [url, init] = requireFetchArgs();
    expect(urlOf(url)).toBe("/channels/groups/sync");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      content_owner_id: "COabc",
      dry_run: true,
      reason: "monthly check",
    });
    expect(response).toEqual(SYNC_RESULT);
  });
});

describe("useClearOwnerStampAction", () => {
  it("DELETEs /groups/{id}/content-owner with the encoded reason query param", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CLEAR_RESULT));
    const { result } = renderHook(() => useClearOwnerStampAction(), { wrapper });

    const response = await result.current({ groupId: "g1", reason: "wrong stamp" });

    const [url, init] = requireFetchArgs();
    expect(urlOf(url)).toBe("/groups/g1/content-owner?reason=wrong%20stamp");
    expect((init as RequestInit).method).toBe("DELETE");
    expect(response).toEqual(CLEAR_RESULT);
  });
});

describe("useGroupArchiveAction", () => {
  it("PATCHes /groups/{id} with {active, reason} and resolves to the wrapped group+audit response", async () => {
    fetchMock().mockResolvedValue(jsonResponse(ARCHIVE_RESULT));
    const { result } = renderHook(() => useGroupArchiveAction(), { wrapper });

    const response = await result.current({
      groupId: "g1",
      active: false,
      reason: "stale group",
    });

    const [url, init] = requireFetchArgs();
    expect(urlOf(url)).toBe("/groups/g1");
    expect((init as RequestInit).method).toBe("PATCH");
    expect(JSON.parse(String((init as RequestInit).body))).toEqual({
      active: false,
      reason: "stale group",
    });
    expect(response).toEqual(ARCHIVE_RESULT);
  });
});
