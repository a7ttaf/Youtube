import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CommandView from "@/components/srcc/views/CommandView";
import type {
  ChannelIssuesResponse,
  NetRevenueResponse,
  OutsideCmsResponse,
  SmartAlertsSummary,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

// No-op function used to suppress console.error / fetch during a specific test
// while preserving the spy for assertion. Using a named function (rather than
// `() => {}`) keeps the JS-0321 / arrow-empty lint family happy and the call
// site readable.
function noop() {
  return undefined;
}

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

const OUTSIDE_CMS_BODY: OutsideCmsResponse = {
  items: [
    {
      youtube_channel_id: "UC-OUTSIDE-01",
      channel_name: "Outside Channel One",
      primary_company_id: "company-a",
      cms_status: "OUTSIDE_CMS",
      content_owner_id: null,
      revenue_required: true,
      revenue_source_status: "MISSING",
      missing_official_revenue: true,
      recommended_action: "Add an official revenue source",
    },
    {
      youtube_channel_id: "UC-OUTSIDE-02",
      channel_name: "Outside Channel Two",
      primary_company_id: "company-b",
      cms_status: "OUTSIDE_CMS",
      content_owner_id: null,
      revenue_required: true,
      revenue_source_status: "OFFICIAL_MANUAL_IMPORT",
      missing_official_revenue: false,
      recommended_action: "No action needed",
    },
  ],
  summary: {
    outside_cms_channel_count: 2,
    revenue_required_count: 2,
    missing_official_revenue_count: 1,
  },
};

const CHANNEL_ISSUES_BODY: ChannelIssuesResponse = {
  items: [
    {
      youtube_channel_id: "UC-ISSUE-01",
      channel_name: "Issue Channel One",
      primary_company_id: null,
      issue_type: "MISSING_COMPANY",
      severity: "high",
      message: "Channel has no company assignment",
      recommended_action: "Assign a company",
    },
    {
      youtube_channel_id: "UC-ISSUE-02",
      channel_name: "Issue Channel Two",
      primary_company_id: "company-b",
      issue_type: "MISSING_SECTOR",
      severity: "medium",
      message: "Channel has no sector assignment",
      recommended_action: "Assign a sector",
    },
  ],
  summary: {
    total_issue_count: 2,
    channel_count: 2,
    issue_type_counts: { MISSING_COMPANY: 1, MISSING_SECTOR: 1 },
  },
};

// Same channel id with two distinct issue types. The render key MUST include
// the issue type so React does not log a duplicate-key warning and does not
// reuse the wrong row across issue-type / refresh changes (review #98 T3).
const CHANNEL_ISSUES_MULTI: ChannelIssuesResponse = {
  items: [
    {
      youtube_channel_id: "UC-ISSUE-01",
      channel_name: "Issue Channel One",
      primary_company_id: null,
      issue_type: "MISSING_SECTOR",
      severity: "medium",
      message: "Channel has no sector assignment",
      recommended_action: "Assign a sector",
    },
    {
      youtube_channel_id: "UC-ISSUE-01",
      channel_name: "Issue Channel One",
      primary_company_id: null,
      issue_type: "OUTSIDE_CMS_REVENUE_REQUIRED",
      severity: "high",
      message: "Channel is outside CMS and revenue-required",
      recommended_action: "Move to a managed CMS or mark revenue optional",
    },
  ],
  summary: {
    total_issue_count: 2,
    channel_count: 1,
    issue_type_counts: { MISSING_SECTOR: 1, OUTSIDE_CMS_REVENUE_REQUIRED: 1 },
  },
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

type RouteOptions = {
  outsideCms?: () => Response;
  issues?: () => Response;
};

const DEFAULT_OUTSIDE_CMS: () => Response = () => jsonResponse(OUTSIDE_CMS_BODY);
const DEFAULT_ISSUES: () => Response = () => jsonResponse(CHANNEL_ISSUES_BODY);
const DEFAULT_SMART_ALERTS: () => Response = () => jsonResponse(SMART_ALERTS_CLEAR);
const DEFAULT_NET_REVENUE: () => Response = () => jsonResponse(NET_REVENUE_BODY);

// Per-URL response strategy. Keyed by the URL substring to match, value is
// the response builder. Keeping the lookup in a table (rather than chained
// `if`s in `routeFetch`) drops the function's cyclomatic complexity below the
// JS-R1005 medium-risk threshold.
const URL_RESPONDERS: Array<{
  match: (url: string) => boolean;
  build: (opts: RouteOptions) => () => Response;
}> = [
  { match: (url) => url.includes("/channels/outside-cms"),
    build: (o) => o.outsideCms ?? DEFAULT_OUTSIDE_CMS },
  { match: (url) => url.includes("/channels/issues"),
    build: (o) => o.issues ?? DEFAULT_ISSUES },
  { match: (url) => url.includes("/smart-alerts"),
    build: () => DEFAULT_SMART_ALERTS },
  // Resolve the authorized-scope listing so CommandView's scopesReady gate
  // flips promptly and the gated net-revenue read fires (review #102 Qodo #3).
  { match: (url) => url.includes("/revenue/scopes"),
    build: () =>
      () =>
        jsonResponse({
          scopes: [{ scope_type: "global", scope_id: null, label: "Global" }],
        }) },
  { match: (url) => url.includes("/net-revenue"),
    build: () => DEFAULT_NET_REVENUE },
];

// Route each fetch by URL so the monitor panel's two reads (outside-cms +
// issues) can be driven independently from the net-revenue + smart-alerts reads.
function routeFetch(opts: RouteOptions = {}) {
  const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
  fetchMock.mockImplementation((input: RequestInfo | URL) => {
    const url = String(input);
    for (const responder of URL_RESPONDERS) {
      if (responder.match(url)) {
        return Promise.resolve(responder.build(opts)());
      }
    }
    return Promise.resolve(jsonResponse({}, 404));
  });
  return fetchMock;
}

function renderCommandView(opts: {
  canViewFinance?: boolean;
  canViewAnalytics?: boolean;
} = {}) {
  return render(
    <TenantProvider initialSlug="ums">
      <CommandView
        canViewFinance={opts.canViewFinance ?? false}
        canViewAnalytics={opts.canViewAnalytics ?? false}
      />
    </TenantProvider>,
  );
}

describe("OutsideCmsMonitorPanel in CommandView", () => {
  it("loads the outside-CMS + issues rows and summary when analytics is allowed", async () => {
    routeFetch({});
    renderCommandView({ canViewAnalytics: true });

    // Outside-CMS missing-source row + its recommended action.
    expect(
      await screen.findByText("Outside Channel One"),
    ).toBeInTheDocument();
    // The covered (OFFICIAL_MANUAL_IMPORT) channel is distinguished, not masked.
    expect(screen.getByText("Outside Channel Two")).toBeInTheDocument();
    // A channel-issue row also renders from the second hook.
    expect(screen.getByText("Channel has no company assignment")).toBeInTheDocument();
    // The panel title is present.
    expect(
      screen.getByText("Outside-CMS & Channel Issues"),
    ).toBeInTheDocument();
  });

  it("no-fetch-when-restricted: fires zero monitor requests without analytics", async () => {
    const fetchMock = routeFetch({});
    renderCommandView({ canViewAnalytics: false, canViewFinance: false });

    // Wait for the net-revenue read so the render settles before asserting.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );

    const calledUrls = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(calledUrls.some((u) => u.includes("/channels/outside-cms"))).toBe(false);
    expect(calledUrls.some((u) => u.includes("/channels/issues"))).toBe(false);

    // The restricted placeholder copy is shown (NOT a "no issues" masking).
    expect(screen.getByText(/analytics access is required/i)).toBeInTheDocument();
  });

  it("403 renders a denied state, NOT a 'no issues' message", async () => {
    routeFetch({
      outsideCms: () =>
        jsonResponse({ detail: "missing permission view:analytics" }, 403),
      issues: () =>
        jsonResponse({ detail: "missing permission view:analytics" }, 403),
    });
    renderCommandView({ canViewAnalytics: true });

    // The panel surfaces a denied/no-permission state.
    await waitFor(() =>
      expect(screen.getAllByText(/no permission/i).length).toBeGreaterThan(0),
    );
    // Masking authz as "no issues" / "all clear" is forbidden.
    expect(screen.queryByText(/no issues/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/all clear/i)).not.toBeInTheDocument();
  });

  it("503 renders an unavailable state contained to the panel", async () => {
    routeFetch({
      outsideCms: () => jsonResponse({ detail: "unavailable" }, 503),
      issues: () => jsonResponse(CHANNEL_ISSUES_BODY),
    });
    renderCommandView({ canViewAnalytics: true });

    // Net-revenue + the issues half still render despite the outside-cms 503.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    expect(
      screen.getByText("Channel has no company assignment"),
    ).toBeInTheDocument();
    // The outside-cms half surfaces the typed request-failed copy.
    expect(screen.getByText(/Request failed \(503\)/)).toBeInTheDocument();
  });

  it("fails independently: an issues 500 stays contained to its half", async () => {
    routeFetch({
      outsideCms: () => jsonResponse(OUTSIDE_CMS_BODY),
      issues: () => jsonResponse({ detail: "boom" }, 500),
    });
    renderCommandView({ canViewAnalytics: true });

    // Outside-cms half renders despite the issues 500.
    expect(await screen.findByText("Outside Channel One")).toBeInTheDocument();
    // Net-revenue keeps rendering (await: the read is gated on scopesReady).
    expect((await screen.findAllByText("UC-DRAMA-01")).length).toBeGreaterThan(0);
    // The issues half surfaces the typed request-failed copy.
    expect(screen.getByText(/Request failed \(500\)/)).toBeInTheDocument();
  });

  it("renders two issues for the same channel with distinct keys (no duplicate-key warning)", async () => {
    // Capture React's duplicate-key warnings so the regression guard catches
    // a future revert back to keying on youtube_channel_id alone.
    const consoleErrorSpy = vi.spyOn(console, "error");
    consoleErrorSpy.mockImplementation(noop);
    try {
      routeFetch({ issues: () => jsonResponse(CHANNEL_ISSUES_MULTI) });
      renderCommandView({ canViewAnalytics: true });

      // Both issue rows render for the same channel id, distinguished by type.
      expect(
        await screen.findByText("Channel has no sector assignment"),
      ).toBeInTheDocument();
      expect(
        screen.getByText("Channel is outside CMS and revenue-required"),
      ).toBeInTheDocument();

      const duplicateKeyCalls = consoleErrorSpy.mock.calls.filter((args) => {
        const joined = args
          .map((a) => (typeof a === "string" ? a : ""))
          .join(" ");
        return /Encountered two children with the same key/i.test(joined);
      });
      expect(duplicateKeyCalls).toEqual([]);
    } finally {
      consoleErrorSpy.mockRestore();
    }
  });
});
