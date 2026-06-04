import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import CommandView from "@/components/srcc/views/CommandView";
import type { NetRevenueResponse } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

const NET_REVENUE_BODY: NetRevenueResponse = {
  month: "2026-03",
  status: "CALCULATED",
  channel_count: 1,
  calculated_channel_count: 1,
  missing_net_source_count: 0,
  pending_manual_override_count: 0,
  total_adjusted_gross_revenue_usd: "1234.56",
  total_net_revenue_usd: "1000",
  total_deduction_amount_usd: "234.56",
  total_channel_direct_deduction_amount_usd: "200",
  total_account_allocated_deduction_amount_usd: "34.56",
  unallocated_account_deduction_total_usd: null,
  unallocated_account_issues: null,
  channels: [
    {
      month: "2026-03",
      youtube_channel_id: "UC-DRAMA-01",
      status: "CALCULATED",
      primary_source_kind: "youtube_cms",
      baseline_gross_revenue_usd: "1234.56",
      baseline_net_revenue_usd: "1000",
      approved_manual_override_total_usd: "0",
      adjusted_gross_revenue_usd: "1234.56",
      net_revenue_usd: "1000",
      deduction_amount_usd: "234.56",
      channel_direct_deduction_amount_usd: "200",
      account_allocated_deduction_amount_usd: "34.56",
      deduction_percentage: "19",
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

function renderCommandView(canViewFinance: boolean) {
  return render(
    <TenantProvider initialSlug="ums">
      <CommandView canViewFinance={canViewFinance} />
    </TenantProvider>,
  );
}

describe("CommandView wired to net-revenue", () => {
  it("renders real-shaped totals formatted as USD when finance is visible", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(NET_REVENUE_BODY),
    );
    renderCommandView(true);

    // Net total formatted from the "1000" string into USD. The same total
    // surfaces in both the status strip and the explain card, hence getAllByText.
    await waitFor(() =>
      expect(screen.getAllByText("$1,000.00").length).toBeGreaterThan(0),
    );
    // Adjusted gross from "1234.56".
    expect(screen.getAllByText("$1,234.56").length).toBeGreaterThan(0);
    // Real channel id rendered (table + explain head).
    expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0);
    // Allocation source surfaced from live_compute.
    expect(screen.getByText("Live compute")).toBeInTheDocument();
  });

  it("withholds money cells as Restricted when finance is not visible", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(NET_REVENUE_BODY),
    );
    renderCommandView(false);

    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // No formatted money should leak.
    expect(screen.queryByText("$1,000.00")).not.toBeInTheDocument();
    expect(screen.getAllByText("Restricted").length).toBeGreaterThan(0);
  });

  it("shows a no-permission message on a 403 ApiError", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "missing permission view:revenue" }, 403),
    );
    renderCommandView(true);

    // "No permission" renders in the status strip and the channel table error.
    await waitFor(() =>
      expect(screen.getAllByText("No permission").length).toBeGreaterThan(0),
    );
    expect(
      screen.getAllByText(/cannot view net revenue/i).length,
    ).toBeGreaterThan(0);
  });

  it("shows a loading state before the response resolves", async () => {
    let resolveFetch: ((value: Response) => void) | undefined;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockReturnValue(
      new Promise<Response>((resolve) => {
        resolveFetch = resolve;
      }),
    );
    renderCommandView(true);

    // Loading badges render while the request is in flight.
    expect(screen.getAllByText("Loading").length).toBeGreaterThan(0);

    resolveFetch?.(jsonResponse(NET_REVENUE_BODY));
    await waitFor(() =>
      expect(screen.getAllByText("$1,000.00").length).toBeGreaterThan(0),
    );
  });
});
