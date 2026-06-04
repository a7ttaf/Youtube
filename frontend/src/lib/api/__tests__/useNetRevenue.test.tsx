import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { useNetRevenue } from "@/lib/api/useNetRevenue";
import type { NetRevenueResponse } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

function wrapper({ children }: { children: React.ReactNode }) {
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
}

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

// Real-shaped net-revenue payload (matches MonthNetRevenueSummary.to_api()
// + the route-level currency/allocation_source/committed_run/audit_events).
const NET_REVENUE_BODY: NetRevenueResponse = {
  month: "2026-03",
  status: "CALCULATED",
  channel_count: 2,
  calculated_channel_count: 1,
  missing_net_source_count: 1,
  pending_manual_override_count: 0,
  total_adjusted_gross_revenue_usd: "1234.56",
  total_net_revenue_usd: "1000.00",
  total_deduction_amount_usd: "234.56",
  total_channel_direct_deduction_amount_usd: "200.00",
  total_account_allocated_deduction_amount_usd: "34.56",
  unallocated_account_deduction_total_usd: null,
  unallocated_account_issues: null,
  channels: [
    {
      month: "2026-03",
      youtube_channel_id: "UC-DRAMA-01",
      status: "CALCULATED",
      primary_source_kind: "youtube_cms",
      baseline_gross_revenue_usd: "900.00",
      baseline_net_revenue_usd: "800.00",
      approved_manual_override_total_usd: "0",
      adjusted_gross_revenue_usd: "900.00",
      net_revenue_usd: "800.00",
      deduction_amount_usd: "100.00",
      channel_direct_deduction_amount_usd: "80.00",
      account_allocated_deduction_amount_usd: "20.00",
      deduction_percentage: "11.11",
      confidence: "B",
      approved_manual_override_count: 0,
      pending_manual_override_count: 0,
      issues: [],
    },
  ],
  currency: "USD",
  allocation_source: "live_compute",
  committed_run: null,
  audit_events: [],
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function lastFetchArgs() {
  return (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.at(-1);
}

/** Narrow the last fetch args away from `undefined`, failing the test if none. */
function requireFetchArgs() {
  const args = lastFetchArgs();
  if (!args) throw new Error("expected fetch to have been called");
  return args;
}

describe("useNetRevenue", () => {
  it("requests the net-revenue endpoint with the month path and scope query params", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(NET_REVENUE_BODY),
    );
    const { result } = renderHook(
      () => useNetRevenue({ month: "2026-03", scopeType: "global" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe(
      "/revenue/months/2026-03/net-revenue?scope_type=global",
    );
  });

  it("returns the parsed real-shaped data on success and clears loading", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(NET_REVENUE_BODY),
    );
    const { result } = renderHook(() => useNetRevenue({ month: "2026-03" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    expect(result.current.data?.total_net_revenue_usd).toBe("1000.00");
    expect(result.current.data?.allocation_source).toBe("live_compute");
    expect(result.current.data?.channels[0]?.youtube_channel_id).toBe("UC-DRAMA-01");
  });

  it("surfaces a typed ApiError (403) with no data on permission failure", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "missing permission view:revenue" }, 403),
    );
    const { result } = renderHook(() => useNetRevenue({ month: "2026-03" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("encodes the scope_id query param when a scoped read is requested", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(NET_REVENUE_BODY),
    );
    const { result } = renderHook(
      () =>
        useNetRevenue({
          month: "2026-03",
          scopeType: "company",
          scopeId: "co 1",
        }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe(
      "/revenue/months/2026-03/net-revenue?scope_type=company&scope_id=co+1",
    );
  });
});
