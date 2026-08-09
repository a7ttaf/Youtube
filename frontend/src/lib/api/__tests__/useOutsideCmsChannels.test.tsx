import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OutsideCmsResponse } from "@/lib/api/types";
import { useOutsideCmsChannels } from "@/lib/api/useOutsideCmsChannels";
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

const OUTSIDE_CMS: OutsideCmsResponse = {
  items: [
    {
      youtube_channel_id: "UC-OUT-01",
      channel_name: "Outside Drama",
      primary_company_id: "united-studios",
      cms_status: "OUTSIDE_CMS",
      content_owner_id: null,
      revenue_required: true,
      revenue_source_status: "MISSING_REVENUE_SOURCE",
      missing_official_revenue: true,
      recommended_action: "Link channel to CMS or import official manual revenue.",
    },
    {
      youtube_channel_id: "UC-OUT-02",
      channel_name: "Outside Covered",
      primary_company_id: "united-studios",
      cms_status: "OUTSIDE_CMS",
      content_owner_id: null,
      revenue_required: true,
      revenue_source_status: "OFFICIAL_MANUAL_IMPORT",
      missing_official_revenue: false,
      recommended_action: "Keep manual official revenue import current.",
    },
  ],
  summary: {
    outside_cms_channel_count: 2,
    revenue_required_count: 2,
    missing_official_revenue_count: 1,
  },
};

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

describe("useOutsideCmsChannels", () => {
  it("auto-fetches GET /channels/outside-cms on mount and returns the {items, summary} shape", async () => {
    fetchMock().mockResolvedValue(jsonResponse(OUTSIDE_CMS));
    const { result } = renderHook(() => useOutsideCmsChannels(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(urlOf(requireFetchArgs()[0])).toBe("/channels/outside-cms");
    expect(result.current.data?.items).toHaveLength(2);
    expect(result.current.data?.items[0]?.youtube_channel_id).toBe("UC-OUT-01");
    expect(result.current.data?.summary.missing_official_revenue_count).toBe(1);
    expect(result.current.error).toBeNull();
    expect(typeof result.current.reload).toBe("function");
  });

  it("captures a typed ApiError (403) and clears data (never masks authz)", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: analytics.view" }, 403),
    );
    const { result } = renderHook(() => useOutsideCmsChannels(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse(OUTSIDE_CMS));
    const { result } = renderHook(() => useOutsideCmsChannels(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });
});
