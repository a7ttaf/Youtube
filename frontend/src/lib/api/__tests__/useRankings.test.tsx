import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { MonthRankingsResponse } from "@/lib/api/types";
import { useRankings } from "@/lib/api/useRankings";
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

const RANKINGS: MonthRankingsResponse = {
  month: "2026-03",
  metric: "gross",
  channels: [
    {
      rank: 1,
      entity_id: "UC-DRAMA-01",
      entity_name: "UMS Drama",
      gross_revenue_usd: "900.00",
      net_revenue_usd: "800.00",
      deduction_amount_usd: "100.00",
    },
    {
      rank: 2,
      entity_id: "UC-MUSIC-31",
      entity_name: "Music Stage",
      gross_revenue_usd: "100.00",
      net_revenue_usd: null,
      deduction_amount_usd: null,
    },
  ],
  companies: [
    {
      rank: 1,
      entity_id: "united-studios",
      entity_name: "United Studios",
      gross_revenue_usd: "1000.00",
      net_revenue_usd: "800.00",
      deduction_amount_usd: "100.00",
    },
  ],
  sectors: [],
  allocation_source: "live_compute",
  committed_run: null,
};

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

describe("useRankings", () => {
  it("requests the rankings endpoint with the month path and query params", async () => {
    fetchMock().mockResolvedValue(jsonResponse(RANKINGS));
    const { result } = renderHook(
      () =>
        useRankings({
          month: "2026-03",
          scopeType: "global",
          metric: "gross",
          limit: 10,
        }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(urlOf(requireFetchArgs()[0])).toBe(
      "/revenue/months/2026-03/rankings?scope_type=global&metric=gross&limit=10",
    );
    expect(result.current.error).toBeNull();
  });

  it("returns the parsed real-shaped data and preserves a null metric (None)", async () => {
    fetchMock().mockResolvedValue(jsonResponse(RANKINGS));
    const { result } = renderHook(() => useRankings({ month: "2026-03" }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.metric).toBe("gross");
    expect(result.current.data?.channels[0]?.gross_revenue_usd).toBe("900.00");
    expect(result.current.data?.channels[1]?.net_revenue_usd).toBeNull();
    expect(result.current.data?.allocation_source).toBe("live_compute");
  });

  it("omits query params when only the month is given (route defaults apply)", async () => {
    fetchMock().mockResolvedValue(jsonResponse(RANKINGS));
    const { result } = renderHook(() => useRankings({ month: "2026-03" }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(urlOf(requireFetchArgs()[0])).toBe("/revenue/months/2026-03/rankings");
  });

  it("encodes the scope_id query param when a scoped read is requested", async () => {
    fetchMock().mockResolvedValue(jsonResponse(RANKINGS));
    const { result } = renderHook(
      () =>
        useRankings({
          month: "2026-03",
          scopeType: "company",
          scopeId: "co 1",
          metric: "net",
        }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(urlOf(requireFetchArgs()[0])).toBe(
      "/revenue/months/2026-03/rankings?scope_type=company&scope_id=co+1&metric=net",
    );
  });

  it("surfaces a typed ApiError (403) with no data on permission failure", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "missing permission view:revenue" }, 403),
    );
    const { result } = renderHook(() => useRankings({ month: "2026-03" }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse(RANKINGS));
    const { result } = renderHook(() => useRankings({ month: "2026-03" }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });
});
