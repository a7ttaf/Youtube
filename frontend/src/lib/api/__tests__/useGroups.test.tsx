import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelGroupApiEntry } from "@/lib/api/types";
import { useGroups } from "@/lib/api/useGroups";
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

const GROUPS: ChannelGroupApiEntry[] = [
  {
    id: "g1",
    name: "TV Sector",
    group_type: "CUSTOM",
    active: true,
    channel_ids: ["UCa"],
    cms_group_id: "deWjsA",
    content_owner_id: "COabc",
  },
];

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

describe("useGroups", () => {
  it("fetches GET /groups and returns entries", async () => {
    fetchMock().mockResolvedValue(jsonResponse(GROUPS));
    const { result } = renderHook(() => useGroups(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual(GROUPS);
    expect(urlOf(requireFetchArgs()[0])).toContain("/groups");
    expect(result.current.error).toBeNull();
  });

  it("captures a typed ApiError (403) and clears data (never masks authz)", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission" }, 403),
    );
    const { result } = renderHook(() => useGroups(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});
