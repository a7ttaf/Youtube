import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

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

// CommandView now fires a SECOND request for the smart-alerts panel. Default it
// to a CLEAR (no-alert) summary so it never interferes with the net-revenue
// assertions below; the panel's own behaviour is covered in SmartAlertsPanel.test.
const SMART_ALERTS_CLEAR: SmartAlertsSummary = {
  month: "2026-03",
  status: "CLEAR",
  highest_severity: null,
  alert_count: 0,
  alerts: [],
  audit_events: [],
};

// CommandView also mounts a finance-gated rankings panel (canViewFinance=true in
// these tests), so it fires a THIRD request to /rankings. Default it to an empty
// rankings body with NO allocation_source so it never collides with the
// net-revenue assertions below; the rankings panel's own behaviour is covered in
// RankingsPanel.test. (The analytics monitor stays unmounted — canViewAnalytics
// defaults to false here — so no /channels/* monitor request fires.)
const RANKINGS_EMPTY: MonthRankingsResponse = {
  month: "2026-03",
  metric: "gross",
  channels: [],
  companies: [],
  sectors: [],
  committed_run: null,
};

function jsonResponse(body: unknown, status = 200) { // skipcq: JS-0067
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Route each fetch by URL and return a FRESH Response per call (a Response body
// can only be read once, so the net-revenue + smart-alerts requests cannot share
// one). net-revenue is driven by the test; smart-alerts + rankings default to a
// quiet body so this suite isolates net-revenue behaviour.
function routeFetch(netRevenue: () => Response, smartAlerts?: () => Response) { // skipcq: JS-0067
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
    (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/smart-alerts")) {
        return Promise.resolve(
          (smartAlerts ?? (() => jsonResponse(SMART_ALERTS_CLEAR)))(),
        );
      }
      if (url.includes("/rankings")) {
        return Promise.resolve(jsonResponse(RANKINGS_EMPTY));
      }
      return Promise.resolve(netRevenue());
    },
  );
}

function renderCommandView(canViewFinance: boolean) { // skipcq: JS-0067
  return render(
    <TenantProvider initialSlug="ums">
      <CommandView canViewFinance={canViewFinance} />
    </TenantProvider>,
  );
}

describe("CommandView wired to net-revenue", () => {
  it("renders real-shaped totals formatted as USD when finance is visible", async () => {
    routeFetch(() => jsonResponse(NET_REVENUE_BODY));
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
    routeFetch(() => jsonResponse(NET_REVENUE_BODY));
    renderCommandView(false);

    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // No formatted money should leak.
    expect(screen.queryByText("$1,000.00")).not.toBeInTheDocument();
    expect(screen.getAllByText("Restricted").length).toBeGreaterThan(0);
  });

  it("renders the human confidence label with the raw code in title/aria", async () => {
    routeFetch(() => jsonResponse(NET_REVENUE_BODY));
    renderCommandView(true);

    // Code "B" maps to the human label "Reconciled" (table + explain card).
    await waitFor(() =>
      expect(screen.getAllByText("Reconciled").length).toBeGreaterThan(0),
    );
    // The raw code is never shown as the visible label.
    expect(screen.queryByText(/^B$/)).not.toBeInTheDocument();
    // The raw code is preserved for power users / assistive tech.
    expect(screen.getAllByTitle("B").length).toBeGreaterThan(0);
    expect(screen.getAllByLabelText("Confidence: B").length).toBeGreaterThan(0);
  });

  it("shows a no-permission message on a 403 ApiError", async () => {
    // Net-revenue 403; smart-alerts stays CLEAR so this asserts net-revenue's
    // own no-permission rendering (not the panel's).
    routeFetch(() =>
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
    let resolveNetRevenue: ((value: Response) => void) | undefined;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/smart-alerts")) {
          return Promise.resolve(jsonResponse(SMART_ALERTS_CLEAR));
        }
        return new Promise<Response>((resolve) => {
          resolveNetRevenue = resolve;
        });
      },
    );
    renderCommandView(true);

    // Loading badges render while the net-revenue request is in flight.
    expect(screen.getAllByText("Loading").length).toBeGreaterThan(0);

    resolveNetRevenue?.(jsonResponse(NET_REVENUE_BODY));
    await waitFor(() =>
      expect(screen.getAllByText("$1,000.00").length).toBeGreaterThan(0),
    );
  });
});
