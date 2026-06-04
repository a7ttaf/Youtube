import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CommandView from "@/components/srcc/views/CommandView";
import type { NetRevenueResponse, SmartAlertsSummary } from "@/lib/api/types";
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

const SMART_ALERTS_BODY: SmartAlertsSummary = {
  month: "2026-03",
  status: "ATTENTION_REQUIRED",
  highest_severity: "HIGH",
  alert_count: 2,
  alerts: [
    {
      code: "PAYMENT_NOT_MATCHED",
      severity: "HIGH",
      message: "AdSense payment is not matched for 2026-03.",
      source: "payment_match",
      confidence: "E_MISSING",
      details: { payment_match_status: "NO_PAYMENT" },
    },
    {
      code: "MONTH_NOT_LOCKED",
      severity: "MEDIUM",
      message: "Finance month 2026-03 is not locked.",
      source: "finance_close",
      confidence: "D_ESTIMATED",
      details: { close_status: "OPEN" },
    },
  ],
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

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

// Route each fetch by URL so the smart-alerts panel and the net-revenue content
// can be driven with different responses in the same render — the core proof
// that the panel fails independently.
const routeFetch = (opts: {
  netRevenue?: () => Response;
  smartAlerts?: () => Response;
}) => {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
    (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/smart-alerts")) {
        return Promise.resolve(
          (opts.smartAlerts ?? (() => jsonResponse(SMART_ALERTS_BODY)))(),
        );
      }
      if (url.includes("/net-revenue")) {
        return Promise.resolve(
          (opts.netRevenue ?? (() => jsonResponse(NET_REVENUE_BODY)))(),
        );
      }
      return Promise.resolve(jsonResponse({}, 404));
    },
  );
};

const renderCommandView = (canViewFinance = true) => {
  return render(
    <TenantProvider initialSlug="ums">
      <CommandView canViewFinance={canViewFinance} />
    </TenantProvider>,
  );
};

describe("SmartAlertsPanel in CommandView", () => {
  it("shows a loading state before the smart-alerts response resolves", async () => {
    let resolveAlerts: ((value: Response) => void) | undefined;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/smart-alerts")) {
          return new Promise<Response>((resolve) => {
            resolveAlerts = resolve;
          });
        }
        return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
      },
    );
    renderCommandView();

    expect(
      await screen.findByText("Loading smart alerts…"),
    ).toBeInTheDocument();

    resolveAlerts?.(jsonResponse(SMART_ALERTS_BODY));
    await waitFor(() =>
      expect(
        screen.getByText("AdSense payment is not matched for 2026-03."),
      ).toBeInTheDocument(),
    );
  });

  it("renders the overall status, highest severity, and real alert rows", async () => {
    routeFetch({});
    renderCommandView();

    // The panel title is present and the alert messages + severities render.
    expect(
      await screen.findByText("Smart Alerts / Problem Panel"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("AdSense payment is not matched for 2026-03."),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Finance month 2026-03 is not locked."),
    ).toBeInTheDocument();
    // Severity badges (HIGH from the alert row + header highest_severity).
    expect(screen.getAllByText("HIGH").length).toBeGreaterThan(0);
    expect(screen.getByText("MEDIUM")).toBeInTheDocument();
    // Source · code subline.
    expect(
      screen.getByText("payment_match · PAYMENT_NOT_MATCHED"),
    ).toBeInTheDocument();
  });

  it("renders a clear / no-active-alerts state when the month is healthy", async () => {
    routeFetch({ smartAlerts: () => jsonResponse(SMART_ALERTS_CLEAR) });
    renderCommandView();

    expect(await screen.findByText("No active alerts")).toBeInTheDocument();
    // "Clear" badge appears (header + row).
    expect(screen.getAllByText("Clear").length).toBeGreaterThan(0);
  });

  it("fails independently: a 403 on smart-alerts still lets net-revenue render", async () => {
    routeFetch({
      smartAlerts: () =>
        jsonResponse({ detail: "missing permission view:bank_reconciliation" }, 403),
      netRevenue: () => jsonResponse(NET_REVENUE_BODY),
    });
    renderCommandView();

    // Net-revenue content renders despite the panel 403 (real channel + total).
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    expect(screen.getAllByText("$1,000.00").length).toBeGreaterThan(0);

    // The panel itself shows the no-permission message (header badge + body row).
    expect(screen.getAllByText("No permission").length).toBeGreaterThan(0);
    expect(screen.getAllByText(/cannot view net revenue/i).length).toBeGreaterThan(0);
    // The panel header still renders (title present), proving the card survives.
    expect(screen.getByText("Smart Alerts / Problem Panel")).toBeInTheDocument();
  });

  it("fails independently: a smart-alerts 500 error is contained to the panel", async () => {
    routeFetch({
      smartAlerts: () => jsonResponse({ detail: "boom" }, 500),
      netRevenue: () => jsonResponse(NET_REVENUE_BODY),
    });
    renderCommandView();

    // Net-revenue still renders.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // The panel surfaces the typed request-failed copy without crashing the view.
    expect(screen.getByText("Request failed (500)")).toBeInTheDocument();
  });
});
