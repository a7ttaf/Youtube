import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelRegistryEntry } from "@/lib/api/types";
import { useChannels } from "@/lib/api/useChannels";
import { TenantProvider } from "@/contexts/TenantContext";

function wrapper({ children }: { children: React.ReactNode }) { // skipcq: JS-0067
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
}

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const CHANNELS: ChannelRegistryEntry[] = [
  {
    youtube_channel_id: "UC-DRAMA-01",
    channel_name: "UMS Drama",
    primary_company_id: "united-studios",
    cms_status: "INSIDE_CMS",
    content_owner_id: "ams/content-owner-1",
    revenue_required: true,
    revenue_source_status: "YOUTUBE_REPORTING_API",
    active: true,
  },
  {
    youtube_channel_id: "UC-MUSIC-31",
    channel_name: "Music Stage",
    primary_company_id: null,
    cms_status: "UNMAPPED",
    content_owner_id: null,
    revenue_required: true,
    revenue_source_status: "MISSING_REVENUE_SOURCE",
    active: true,
  },
];

function jsonResponse(body: unknown, status = 200) { // skipcq: JS-0067
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchMock() { // skipcq: JS-0067
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

function urlOf(input: unknown): string { // skipcq: JS-0067
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

function requireFetchArgs() { // skipcq: JS-0067
  const args = fetchMock().mock.calls.at(-1);
  if (!args) throw new Error("expected fetch to have been called");
  return args;
}

describe("useChannels", () => {
  it("auto-fetches GET /channels on mount and returns {data, loading, error, reload}", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CHANNELS));
    const { result } = renderHook(() => useChannels(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(urlOf(requireFetchArgs()[0])).toBe("/channels");
    expect(result.current.data).toHaveLength(2);
    expect(result.current.data?.[0]?.youtube_channel_id).toBe("UC-DRAMA-01");
    expect(result.current.data?.[0]?.channel_name).toBe("UMS Drama");
    expect(result.current.error).toBeNull();
    expect(typeof result.current.reload).toBe("function");
  });

  it("returns loading: true before the response arrives", async () => {
    fetchMock().mockImplementation(
      () => new Promise<Response>(() => { /* never resolves */ }),
    );
    const { result } = renderHook(() => useChannels(), { wrapper });
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
  });

  it("returns an empty array when the API returns an empty list", async () => {
    fetchMock().mockResolvedValue(jsonResponse([]));
    const { result } = renderHook(() => useChannels(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: analytics.view" }, 403),
    );
    const { result } = renderHook(() => useChannels(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CHANNELS));
    const { result } = renderHook(() => useChannels(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });
});
