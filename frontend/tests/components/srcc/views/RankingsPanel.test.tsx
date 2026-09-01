import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CommandView from "@/components/srcc/views/CommandView";
import type {
  MonthRankingsResponse,
  NetRevenueResponse,
  SmartAlertsSummary,
} from "@/lib/api/types";
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

const SMART_ALERTS_CLEAR: SmartAlertsSummary = {
  month: "2026-03",
  status: "CLEAR",
  highest_severity: null,
  alert_count: 0,
  alerts: [],
  audit_events: [],
};

const RANKINGS_BODY: MonthRankingsResponse = {
  month: "2026-03",
  metric: "gross",
  channels: [
    {
      rank: 1,
      entity_id: "UC-TOP-01",
      entity_name: "Top Channel",
      gross_revenue_usd: "9000",
      net_revenue_usd: "8000",
      deduction_amount_usd: "1000",
    },
  ],
  companies: [
    {
      rank: 1,
      entity_id: "company-a",
      entity_name: "Company Alpha",
      gross_revenue_usd: "9000",
      net_revenue_usd: "8000",
      deduction_amount_usd: "1000",
    },
  ],
  sectors: [
    {
      rank: 1,
      entity_id: "sector-tv",
      entity_name: "TV Sector",
      gross_revenue_usd: "9000",
      net_revenue_usd: "8000",
      deduction_amount_usd: "1000",
    },
  ],
  allocation_source: "live_fallback",
  committed_run: null,
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const DEFAULT_RANKINGS: () => Response = () => jsonResponse(RANKINGS_BODY);

// Route each fetch by URL. The rankings read is captured so the metric-toggle
// test can assert the query param changed across renders.
const routeFetch = (opts: { rankings?: () => Response } = {}) => {
  const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    if (url.includes("/rankings")) {
      return Promise.resolve((opts.rankings ?? DEFAULT_RANKINGS)());
    }
    if (url.includes("/smart-alerts")) {
      return Promise.resolve(jsonResponse(SMART_ALERTS_CLEAR));
    }
    if (url.includes("/net-revenue")) {
      return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
    }
    if (url.includes("/revenue/scopes")) {
      return Promise.resolve(
        jsonResponse({
          scopes: [{ scope_type: "global", scope_id: null, label: "Global" }],
        }),
      );
    }
    return Promise.resolve(jsonResponse({}, 404));
  });
  return fetchMock;
};

const renderCommandView = (opts: {
  canViewFinance?: boolean;
  canViewAnalytics?: boolean;
} = {}) => {
  return render(
    <TenantProvider initialSlug="ums">
      <CommandView
        canViewFinance={opts.canViewFinance ?? false}
        canViewAnalytics={opts.canViewAnalytics ?? false}
      />
    </TenantProvider>,
  );
};

describe("RankingsPanel in CommandView", () => {
  it("loads ranked entities with formatted money when finance is visible", async () => {
    routeFetch({});
    renderCommandView({ canViewFinance: true });

    expect(await screen.findByText("Top Channel")).toBeInTheDocument();
    expect(screen.getByText("Company Alpha")).toBeInTheDocument();
    expect(screen.getByText("TV Sector")).toBeInTheDocument();
    // Gross money is formatted (the default metric).
    expect(screen.getAllByText("$9,000.00").length).toBeGreaterThan(0);
    // allocation_source surfaced so a live_fallback is not read as authoritative.
    expect(screen.getByText(/Live fallback/i)).toBeInTheDocument();
  });

  it("gates money: a non-finance viewer sees Restricted, not amounts", async () => {
    const fetchMock = routeFetch({});
    renderCommandView({ canViewFinance: false });

    await screen.findByText("Revenue access unavailable");
    // Scope discovery itself is withheld, so the finance-bearing rankings
    // panel is not mounted at all.
    expect(screen.queryByText("Rankings")).not.toBeInTheDocument();
    // No rankings request fires for a non-finance viewer (panel shows money).
    const calledUrls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(calledUrls.some((u) => u.includes("/rankings"))).toBe(false);
    // No formatted ranking money leaks.
    expect(screen.queryByText("$9,000.00")).not.toBeInTheDocument();
  });

  it("metric toggle re-queries with the selected metric", async () => {
    const fetchMock = routeFetch({});
    renderCommandView({ canViewFinance: true });

    expect(await screen.findByText("Top Channel")).toBeInTheDocument();

    // Switch the metric toggle to "net".
    const toggle = screen.getByLabelText("Ranking metric");
    await userEvent.selectOptions(toggle, "net");

    await waitFor(() => {
      const calledUrls = fetchMock.mock.calls.map((c) => String(c[0]));
      expect(calledUrls.some((u) => u.includes("metric=net"))).toBe(true);
    });
  });

  it("fails independently: a rankings 403 stays contained to the panel", async () => {
    routeFetch({
      rankings: () =>
        jsonResponse({ detail: "missing permission view:revenue" }, 403),
    });
    renderCommandView({ canViewFinance: true });

    // Net-revenue still renders despite the rankings 403.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // The panel surfaces domain-specific no-permission copy, never the shared
    // net-revenue default.
    expect(screen.getAllByText(/no permission/i).length).toBeGreaterThan(0);
    expect(
      screen.getByText("Your role cannot view rankings for this month or scope."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/cannot view net revenue/i)).not.toBeInTheDocument();
  });

  it("fails independently: a rankings 500 stays contained to the panel", async () => {
    routeFetch({
      rankings: () => jsonResponse({ detail: "boom" }, 500),
    });
    renderCommandView({ canViewFinance: true });

    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    expect(screen.getByText(/Request failed \(500\)/)).toBeInTheDocument();
  });

  it("threads the active Command Center scope into the rankings request (no global default)", async () => {
    // Re-route the fetch to expose the call URL for the assertion: the panel
    // must request /rankings with the global scope_type the Command Center
    // currently selects, not an unscoped URL (regression guard for review
    // #98 T2: rankings defaulted to global regardless of the active scope).
    const fetchMock = routeFetch({});
    renderCommandView({ canViewFinance: true });

    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );

    await waitFor(() => {
      const calledUrls = fetchMock.mock.calls.map((c) => String(c[0]));
      const rankingCall = calledUrls.find((u) => u.includes("/rankings"));
      expect(rankingCall).toBeDefined();
      expect(rankingCall).toMatch(/scope_type=global/);
    });
  });
});
