import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import TraceView from "@/components/srcc/views/TraceView";
import type { NetRevenueResponse, NumberExplanation } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

// Channel directory used to populate the dropdown (reused net-revenue summary).
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
      youtube_channel_id: "demo-channel-alpha",
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

// Real-shaped NET explanation with the channel-direct + account-allocated split.
const NET_EXPLANATION: NumberExplanation = {
  month: "2026-03",
  entity_type: "channel",
  entity_id: "demo-channel-alpha",
  metric: "net_revenue_usd",
  value: "1000",
  currency: "USD",
  formula:
    "net_revenue_usd = adjusted_gross_revenue_usd - channel_direct_deduction_amount_usd - account_allocated_deduction_amount_usd",
  confidence: { label: "HIGH", score: "0.95" },
  components: [
    {
      key: "baseline_gross_revenue_usd",
      label: "Baseline gross revenue",
      value: "1234.56",
      source_kind: "youtube_cms",
      source_report_id: "report-1",
    },
    {
      key: "approved_manual_override_total_usd",
      label: "Approved manual overrides",
      value: "0",
      count: 0,
    },
    {
      key: "channel_direct_deduction_usd",
      label: "Channel-direct deductions",
      value: "200",
      count: 1,
      components: [
        {
          component_kind: "TAX",
          source_system: "manual",
          component_key: "wht",
          amount_usd: "200",
        },
      ],
    },
    {
      key: "account_allocated_deduction_usd",
      label: "Account-allocated deductions",
      value: "34.56",
      count: 1,
      allocations: [
        {
          adsense_account_id: "acct-1",
          component_kind: "FEE",
          source_system: "adsense",
          component_key: "platform-fee",
          basis_source_kind: "youtube_cms",
          basis_share: "0.5",
          allocated_amount_usd: "34.56",
        },
      ],
      allocation_source: "live_compute",
      committed_run: null,
    },
  ],
  warnings: [
    {
      code: "PENDING_MANUAL_OVERRIDES",
      message: "1 pending manual override(s) not included in net_revenue_usd.",
    },
  ],
  audit_events: [],
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

// Route the channel-list GET (net-revenue) vs the explain POST to responders.
const routeFetch = (opts: {
  channels: () => Response;
  explain?: () => Response;
}) => {
  return (input: unknown) => {
    const url = urlOf(input);
    if (url.includes("/explain") && opts.explain) {
      return Promise.resolve(opts.explain());
    }
    return Promise.resolve(opts.channels());
  };
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

/** Resolve a Response from outside, so an explain POST can be held in flight. */
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

const renderTraceView = (canViewFinance = true) => {
  return render(
    <TenantProvider initialSlug="ums">
      <TraceView canViewFinance={canViewFinance} role="finance" />
    </TenantProvider>,
  );
};

describe("TraceView wired to the explain endpoint", () => {
  it("renders the idle prompt and does not POST before Explain is clicked", async () => {
    fetchMock().mockImplementation(
      routeFetch({ channels: () => jsonResponse(NET_REVENUE_BODY) }),
    );
    renderTraceView();

    // The channel dropdown populates from the net-revenue summary.
    await waitFor(() =>
      expect(
        (screen.getByLabelText("Channel") as HTMLSelectElement).value,
      ).toBe("demo-channel-alpha"),
    );
    expect(
      screen.getByText(/select Explain to generate the source-linked breakdown/i),
    ).toBeInTheDocument();
    // Only the channel-list GET fired; no explain POST yet.
    expect(
      fetchMock().mock.calls.some(([input]) => urlOf(input).includes("/explain")),
    ).toBe(false);
  });

  it("shows the loading state while the explanation POST is in flight", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        channels: () => jsonResponse(NET_REVENUE_BODY),
        explain: () => {
          throw new Error("should be a pending promise, not invoked sync");
        },
      }),
    );
    // Override: channel list resolves, explain hangs.
    fetchMock().mockImplementation((input: unknown) => {
      if (urlOf(input).includes("/explain")) {
        // Never resolves: the explain POST stays in flight so the loading state holds.
        return new Promise<Response>(() => {
          /* intentionally pending */
        });
      }
      return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
    });

    renderTraceView();
    const explainButton = await screen.findByRole("button", { name: /^explain$/i });
    // The button stays disabled until the channel list resolves; wait so the click
    // does not depend on microtask ordering.
    await waitFor(() => expect(explainButton).not.toBeDisabled());
    fireEvent.click(explainButton);

    await waitFor(() =>
      expect(screen.getByText("Generating explanation…")).toBeInTheDocument(),
    );
  });

  it("renders a real-shaped NET explanation: value, formula, component split, confidence, and warnings", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        channels: () => jsonResponse(NET_REVENUE_BODY),
        explain: () => jsonResponse(NET_EXPLANATION),
      }),
    );
    renderTraceView();

    // Select the net metric, then Explain.
    const metricSelect = await screen.findByLabelText("Metric");
    fireEvent.change(metricSelect, { target: { value: "net_revenue_usd" } });
    const explainButton = screen.getByRole("button", { name: /^explain$/i });
    // Wait for the button to enable (channel list resolved) before clicking.
    await waitFor(() => expect(explainButton).not.toBeDisabled());
    fireEvent.click(explainButton);

    // The metric value renders as a formatted money string.
    await waitFor(() =>
      expect(screen.getByText("$1,000.00")).toBeInTheDocument(),
    );
    // Formula + confidence label + score.
    expect(screen.getByLabelText("Metric formula").textContent).toContain(
      "net_revenue_usd = adjusted_gross_revenue_usd",
    );
    expect(screen.getByText("0.95")).toBeInTheDocument();
    // Component breakdown rows (the net deduction split).
    expect(screen.getByText("Baseline gross revenue")).toBeInTheDocument();
    expect(screen.getByText("Channel-direct deductions")).toBeInTheDocument();
    expect(screen.getByText("Account-allocated deductions")).toBeInTheDocument();
    // A component money cell.
    expect(screen.getByText("$34.56")).toBeInTheDocument();
    // The warning surfaces.
    expect(
      screen.getByText(
        "1 pending manual override(s) not included in net_revenue_usd.",
      ),
    ).toBeInTheDocument();
  });

  it("withholds money cells from a viewer without finance permission", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        channels: () => jsonResponse(NET_REVENUE_BODY),
        explain: () => jsonResponse(NET_EXPLANATION),
      }),
    );
    render(
      <TenantProvider initialSlug="ums">
        <TraceView canViewFinance={false} role="assistant" />
      </TenantProvider>,
    );

    const explainButton = await screen.findByRole("button", {
      name: /^explain$/i,
    });
    // Wait for the button to enable (channel list resolved) before clicking.
    await waitFor(() => expect(explainButton).not.toBeDisabled());
    fireEvent.click(explainButton);

    await waitFor(() =>
      expect(screen.getByText("Baseline gross revenue")).toBeInTheDocument(),
    );
    // No money strings; the gated value shows the Restricted sentinel.
    expect(screen.getAllByText("Restricted").length).toBeGreaterThan(0);
    expect(screen.queryByText("$1,000.00")).not.toBeInTheDocument();
  });

  it("maps a 403 from the explain POST to a no-permission message", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        channels: () => jsonResponse(NET_REVENUE_BODY),
        explain: () =>
          jsonResponse(
            { detail: "Missing permission: view:finalized-payments" },
            403,
          ),
      }),
    );
    renderTraceView();

    const explainButton = await screen.findByRole("button", {
      name: /^explain$/i,
    });
    // Wait for the button to enable (channel list resolved) before clicking.
    await waitFor(() => expect(explainButton).not.toBeDisabled());
    fireEvent.click(explainButton);

    await waitFor(() =>
      expect(screen.getByText("No permission")).toBeInTheDocument(),
    );
  });

  it("discards a stale explanation when the metric changes while the explain POST is in flight", async () => {
    // The channel list resolves immediately; the explain POST is held open via a
    // deferred so we can change a filter BEFORE the old response arrives.
    const inFlight = deferred<Response>();
    fetchMock().mockImplementation((input: unknown) => {
      if (urlOf(input).includes("/explain")) return inFlight.promise;
      return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
    });
    renderTraceView();

    // Explain the default (gross) metric; the POST stays in flight.
    const explainButton = await screen.findByRole("button", { name: /^explain$/i });
    await waitFor(() => expect(explainButton).not.toBeDisabled());
    fireEvent.click(explainButton);
    await waitFor(() =>
      expect(screen.getByText("Generating explanation…")).toBeInTheDocument(),
    );

    // The operator changes the metric while the gross explain is still pending.
    // This is an explain input, so the reset effect must abandon the in-flight
    // request and clear the rendered state.
    fireEvent.change(screen.getByLabelText("Metric"), {
      target: { value: "net_revenue_usd" },
    });

    // The OLD (gross) explanation now resolves — under the new metric filter. It
    // must be discarded: its breakdown must NOT render.
    await act(async () => {
      inFlight.resolve(jsonResponse(NET_EXPLANATION));
      await Promise.resolve();
    });

    // Back to the idle prompt (state cleared), not the stale breakdown.
    await waitFor(() =>
      expect(
        screen.getByText(/select Explain to generate the source-linked breakdown/i),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Baseline gross revenue")).not.toBeInTheDocument();
    expect(screen.queryByText("Channel-direct deductions")).not.toBeInTheDocument();
  });

  it("surfaces a channel-list load error without crashing the screen", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        channels: () =>
          jsonResponse({ detail: "Missing permission: view:revenue" }, 403),
      }),
    );
    renderTraceView();

    // The error band surfaces the channel-list failure (unique message text).
    await waitFor(() =>
      expect(
        screen.getByText(/Channel list unavailable/i),
      ).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    // The Explain button is disabled when there is no channel to explain.
    expect(screen.getByRole("button", { name: /^explain$/i })).toBeDisabled();
  });
});
