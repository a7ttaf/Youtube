import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";

import CommandView from "@/components/srcc/views/CommandView";
import type {
  MonthBankReconciliationSummary,
  MonthRankingsResponse,
  NetRevenueResponse,
  RevenueScopeOption,
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

// CommandView fires an independent request for the smart-alerts panel. Default
// it to a CLEAR (no-alert) summary so it never interferes with the net-revenue
// assertions below; the panel's own behaviour is covered in SmartAlertsPanel.test.
const SMART_ALERTS_CLEAR: SmartAlertsSummary = {
  month: "2026-03",
  status: "CLEAR",
  highest_severity: null,
  alert_count: 0,
  alerts: [],
  audit_events: [],
};

const BANK_RECONCILIATION_SUMMARY: MonthBankReconciliationSummary = {
  month: "2026-03",
  currency: "USD",
  status: "BANK_VARIANCE",
  adsense_paid_amount_usd: "930",
  bank_received_amount_usd: "928.5",
  bank_gap_usd: "1.5",
  transfer_fee_usd: "0.5",
  fx_difference_usd: "0",
  payment_count: 1,
  paid_payment_count: 1,
  non_paid_payment_count: 0,
  unsupported_payment_currency_count: 0,
  entry_count: 1,
  tolerance_usd: "0.01",
  issues: [
    {
      issue_type: "BANK_VARIANCE",
      severity: "HIGH",
      message: "Bank received amount differs from AdSense payment.",
    },
  ],
  entries: [
    {
      id: "bank-1",
      month: "2026-03",
      bank_reference: "bank-transfer-2026-04-22",
      bank_received_date: "2026-04-22",
      bank_received_amount: "928.5",
      bank_received_currency: "USD",
      bank_received_amount_usd: "928.5",
      transfer_fee_usd: "0.5",
      fx_difference_usd: "0",
      notes: null,
      source_report_id: "bank-report-1",
      recorded_by: "finance-user",
    },
  ],
  audit_events: [],
};

// CommandView also mounts finance-gated bank-reconciliation and rankings panels
// (canViewFinance=true in these tests). Default the bank summary to a backend
// shaped payment/bank body and rankings to empty rows so neither panel collides
// with the net-revenue assertions below. (The analytics monitor stays unmounted
// — canViewAnalytics defaults to false here — so no /channels/* monitor request
// fires.)
const RANKINGS_EMPTY: MonthRankingsResponse = {
  month: "2026-03",
  metric: "gross",
  channels: [],
  companies: [],
  sectors: [],
  committed_run: null,
};

// Authorized rollup scopes for the selector: the guaranteed global option plus a
// single company so the dynamic-selector tests can drive a scoped read.
const SCOPES_GLOBAL_AND_COMPANY: RevenueScopeOption[] = [
  { scope_type: "global", scope_id: null, label: "Global" },
  { scope_type: "company", scope_id: "company-a", label: "Company Alpha" },
];

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

// Route each fetch by URL and return a FRESH Response per call (a Response body
// can only be read once, so the mounted panels cannot share one). net-revenue is
// driven by the test; smart-alerts + bank reconciliation + rankings + scopes
// default to a quiet body so this suite isolates net-revenue behaviour. The
// scope selector defaults to global-only so the existing assertions are unchanged.
// Dispatch is a URL-substring -> responder table (data-driven) rather than a
// chain of if/else branches to keep the routing function's cyclomatic complexity
// below DeepSource's medium-risk threshold.
const routeFetch = (
  netRevenue: () => Response,
  smartAlerts?: () => Response,
  scopes?: () => Response,
  bankReconciliation?: () => Response,
) => {
  const routes: ReadonlyArray<{ match: string; respond: () => Response }> = [
    {
      match: "/revenue/scopes",
      respond: scopes ?? (() => jsonResponse({ scopes: [] })),
    },
    {
      match: "/smart-alerts",
      respond: smartAlerts ?? (() => jsonResponse(SMART_ALERTS_CLEAR)),
    },
    {
      match: "/bank-reconciliation",
      respond: bankReconciliation ?? (() => jsonResponse(BANK_RECONCILIATION_SUMMARY)),
    },
    { match: "/rankings", respond: () => jsonResponse(RANKINGS_EMPTY) },
  ];
  const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
  const defaultRoute = { respond: netRevenue };
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    const route = routes.find((entry) => url.includes(entry.match)) ?? defaultRoute;
    return Promise.resolve(route.respond());
  });
  return fetchMock;
};

const renderCommandView = (canViewFinance: boolean) => {
  return render(
    <TenantProvider initialSlug="ums">
      <CommandView canViewFinance={canViewFinance} />
    </TenantProvider>,
  );
};

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

  it("renders payment reconciliation cards from the backend month summary", async () => {
    routeFetch(() => jsonResponse(NET_REVENUE_BODY));
    renderCommandView(true);

    await screen.findByText("$930.00");
    const reconciliation = screen.getByRole("region", {
      name: "Payment reconciliation summary",
    });
    expect(within(reconciliation).getByText("AdSense payment")).toBeInTheDocument();
    expect(within(reconciliation).getByText("Bank received")).toBeInTheDocument();
    expect(within(reconciliation).getByText("Unresolved gap")).toBeInTheDocument();
    expect(within(reconciliation).getByText("$930.00")).toBeInTheDocument();
    expect(within(reconciliation).getByText("$928.50")).toBeInTheDocument();
    expect(within(reconciliation).getByText("$1.50")).toBeInTheDocument();
    expect(within(reconciliation).getByText("Variance")).toBeInTheDocument();
  });

  it("withholds money cells as Restricted when finance is not visible", async () => {
    const fetchMock = routeFetch(() => jsonResponse(NET_REVENUE_BODY));
    renderCommandView(false);

    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // No formatted money should leak.
    expect(screen.queryByText("$1,000.00")).not.toBeInTheDocument();
    expect(screen.getAllByText("Restricted").length).toBeGreaterThan(0);
    expect(
      fetchMock.mock.calls.some((call) => String(call[0]).includes("/bank-reconciliation")),
    ).toBe(false);
  });

  it("keeps net revenue visible when bank reconciliation 403s", async () => {
    routeFetch(
      () => jsonResponse(NET_REVENUE_BODY),
      undefined,
      undefined,
      () =>
        jsonResponse(
          { detail: "Missing permission: finance.view_bank_reconciliation" },
          403,
        ),
    );
    renderCommandView(true);

    await waitFor(() =>
      expect(screen.getAllByText("$1,000.00").length).toBeGreaterThan(0),
    );
    const reconciliation = screen.getByRole("alert", {
      name: "Payment reconciliation summary",
    });
    expect(
      within(reconciliation).getByText("Payment reconciliation unavailable"),
    ).toBeInTheDocument();
    expect(within(reconciliation).getByText("No permission")).toBeInTheDocument();
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
        if (url.includes("/bank-reconciliation")) {
          return Promise.resolve(jsonResponse(BANK_RECONCILIATION_SUMMARY));
        }
        // Resolve scopes + rankings immediately so scopesReady flips and the
        // gated net-revenue read can fire — the load-under-test is net-revenue's.
        if (url.includes("/revenue/scopes")) {
          return Promise.resolve(jsonResponse({ scopes: [] }));
        }
        if (url.includes("/rankings")) {
          return Promise.resolve(jsonResponse(RANKINGS_EMPTY));
        }
        return new Promise<Response>((resolve) => {
          resolveNetRevenue = resolve;
        });
      },
    );
    renderCommandView(true);

    // Loading badges render while the net-revenue request is in flight.
    await waitFor(() =>
      expect(screen.getAllByText("Loading").length).toBeGreaterThan(0),
    );
    // The net-revenue read is gated on scopesReady; await its in-flight promise
    // (assigned once the scopes fetch resolves and flips the gate) before
    // resolving it.
    await waitFor(() => expect(resolveNetRevenue).toBeDefined());
    resolveNetRevenue?.(jsonResponse(NET_REVENUE_BODY));
    await waitFor(() =>
      expect(screen.getAllByText("$1,000.00").length).toBeGreaterThan(0),
    );
  });
});

describe("CommandView dynamic scope selector", () => {
  it("populates the scope selector from GET /revenue/scopes", async () => {
    routeFetch(
      () => jsonResponse(NET_REVENUE_BODY),
      undefined,
      () => jsonResponse({ scopes: SCOPES_GLOBAL_AND_COMPANY }),
    );
    renderCommandView(true);

    const selector = await screen.findByLabelText("Scope");
    // Both the global option and the authorized company option are listed.
    await waitFor(() =>
      expect(
        within(selector).getByRole("option", { name: "Global" }),
      ).toBeInTheDocument(),
    );
    expect(
      within(selector).getByRole("option", { name: "Company Alpha" }),
    ).toBeInTheDocument();
  });

  it("threads the selected company scope into BOTH the net-revenue and rankings reads", async () => {
    const fetchMock = routeFetch(
      () => jsonResponse(NET_REVENUE_BODY),
      undefined,
      () => jsonResponse({ scopes: SCOPES_GLOBAL_AND_COMPANY }),
    );
    renderCommandView(true);

    // Wait for the options to load, then select the company scope. selectOptions
    // matches by the option's VALUE (a stable "scopeType:scopeId" key from
    // scopeOptionKey), NOT its visible label — passing the label string would be
    // a no-op against the real <option value=...>, weakening the regression guard
    // (review #102 Qodo #1). Select by the option element so the assertion stays
    // robust regardless of the key format.
    const selector = await screen.findByLabelText("Scope");
    const companyOption = await waitFor(() =>
      within(selector).getByRole("option", { name: "Company Alpha" }),
    );
    await userEvent.selectOptions(selector, companyOption);

    // The scoped read params must appear on BOTH the net-revenue and the
    // rankings request URLs (the regression guard the rankings test established
    // for global, now exercised for a real non-global scope).
    await waitFor(() => {
      const calledUrls = fetchMock.mock.calls.map((c) => String(c[0]));
      const netCall = calledUrls.find(
        (u) => u.includes("/net-revenue") && u.includes("scope_type=company"),
      );
      const rankingCall = calledUrls.find(
        (u) => u.includes("/rankings") && u.includes("scope_type=company"),
      );
      expect(netCall).toMatch(/scope_id=company-a/);
      expect(rankingCall).toMatch(/scope_id=company-a/);
    });
  });

  it("degrades to global-only and still renders when /revenue/scopes 403s", async () => {
    routeFetch(
      () => jsonResponse(NET_REVENUE_BODY),
      undefined,
      () =>
        jsonResponse({ detail: "Missing permission: finance.view_revenue" }, 403),
    );
    renderCommandView(true);

    // The screen still renders the net-revenue content despite the scopes 403.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // The selector degrades to a guaranteed global-only option (no scoped leak).
    const selector = screen.getByLabelText("Scope");
    expect(
      within(selector).getByRole("option", { name: /global/i }),
    ).toBeInTheDocument();
    expect(
      within(selector).queryByRole("option", { name: "Company Alpha" }),
    ).not.toBeInTheDocument();
    // The default (global) read sends NO scope_id.
    const netCalls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls
      .map((c) => String(c[0]))
      .filter((u) => u.includes("/net-revenue"));
    expect(netCalls.length).toBeGreaterThan(0);
    expect(netCalls.every((u) => !u.includes("scope_id="))).toBe(true);
  });

  it("holds the net-revenue + rankings reads until /revenue/scopes resolves", async () => {
    // While the authorized-scope fetch is still pending, CommandView must NOT
    // fire a net-revenue or rankings read (review #102 Qodo #3): the scope
    // resolves to the global fallback during the load window, so firing
    // immediately would trigger an unauthorized global read for a scoped viewer.
    let resolveScopes: ((value: Response) => void) | undefined;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/revenue/scopes")) {
          return new Promise<Response>((resolve) => {
            resolveScopes = resolve;
          });
        }
        if (url.includes("/bank-reconciliation")) {
          return Promise.resolve(jsonResponse(BANK_RECONCILIATION_SUMMARY));
        }
        // Any other read (net-revenue / rankings / smart-alerts) is recorded if
        // it fires; net-revenue and rankings should NOT fire until the scopes
        // promise resolves.
        return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
      },
    );
    renderCommandView(true);

    // Before the scopes verdict, NO net-revenue or rankings request has fired.
    const callsBefore = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls.map(
      (c) => String(c[0]),
    );
    expect(callsBefore.some((u) => u.includes("/net-revenue"))).toBe(false);
    expect(callsBefore.some((u) => u.includes("/rankings"))).toBe(false);

    // Resolve the scopes fetch (the authorized set arrives) — now the gated
    // reads fire against the resolved scope.
    resolveScopes?.(jsonResponse({ scopes: SCOPES_GLOBAL_AND_COMPANY }));
    await waitFor(() => {
      const callsAfter = (
        globalThis.fetch as ReturnType<typeof vi.fn>
      ).mock.calls.map((c) => String(c[0]));
      expect(callsAfter.some((u) => u.includes("/net-revenue"))).toBe(true);
      expect(callsAfter.some((u) => u.includes("/rankings"))).toBe(true);
    });
  });
});
