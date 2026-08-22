import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import CommandView from "@/components/srcc/views/CommandView";
import type { MonthGapExplanation, NetRevenueResponse } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

// ============================================================================
// Purpose: Pins for the Gap narrative panel inside CommandView — the two leg
//   rows (operands → gap), the evidence components with confidence badges
//   (including the first UI render of fx_difference_usd), residual rows, the
//   month narrative + close badge, warnings, the three-way permission gate
//   (restricted band + NO fetch), and error containment.
// Standards: The gap fixture is a byte-faithful copy of the backend builder's
//   output for the headline scenario (930 facts / 900 paid + 30 pending /
//   880 received + 12 fee + 5 FX) — not an invented shape. Fetches are routed
//   by URL substring so the panel is driven independently of net revenue.
// Connections:
//   - File: frontend/src/components/srcc/views/CommandView.tsx -> GapNarrativePanel.
//   - File: backend/ums_smart_revenue/finance/gap_explanation.py -> fixture source.
// ============================================================================

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

// Byte-faithful backend output for the headline scenario (provenance trimmed
// to a representative subset — the panel does not render provenance).
const GAP_EXPLANATION_BODY: MonthGapExplanation = {
  month: "2026-03",
  currency: "USD",
  close_status: "OPEN",
  status: "PARTIALLY_EXPLAINED",
  tolerance_usd: "0.01",
  payment_leg: {
    status: "FULLY_EXPLAINED",
    youtube_revenue_total_usd: "930",
    adsense_paid_amount_usd: "900",
    payment_gap_usd: "30",
    payment_match_status: "PAYMENT_VARIANCE",
    components: [
      {
        key: "non_paid_adsense_payments",
        label: "AdSense payments not yet PAID",
        amount_usd: "30",
        evidence_count: 1,
        confidence: { label: "HIGH", score: "0.95" },
      },
    ],
    unexplained_residual_usd: "0",
    narrative:
      "YouTube revenue of $930.00 exceeds paid AdSense payments of $900.00 " +
      "by $30.00. $30.00 is not yet PAID (1 payment); $0.00 remains unexplained.",
  },
  bank_leg: {
    status: "PARTIALLY_EXPLAINED",
    adsense_paid_amount_usd: "900",
    bank_received_amount_usd: "880",
    bank_gap_usd: "20",
    bank_reconciliation_status: "BANK_VARIANCE",
    components: [
      {
        key: "transfer_fee",
        label: "Bank transfer fees",
        amount_usd: "12",
        evidence_count: 1,
        confidence: { label: "MEDIUM", score: "0.80" },
      },
      {
        key: "fx_difference",
        label: "Bank-side FX difference",
        amount_usd: "5",
        evidence_count: 1,
        confidence: { label: "MEDIUM", score: "0.80" },
      },
    ],
    unexplained_residual_usd: "3",
    narrative:
      "Paid AdSense payments of $900.00 exceed normalized bank receipts of " +
      "$880.00 by $20.00. Evidence: $12.00 in bank transfer fees (1 bank " +
      "entry); $5.00 in bank-side fx difference (1 bank entry). $3.00 " +
      "remains unexplained.",
  },
  warnings: [],
  money_provenance: {
    "bank_leg.components.fx_difference.amount_usd": {
      source: "bank_reconciliation_entries",
      formula: "sum(fx_difference_usd) for bank entries in the month",
      confidence: "SOURCE_BACKED",
      export_value: "5",
    },
    tolerance_usd: {
      source: "gap_explanation_policy",
      formula: "configured month gap-explanation tolerance",
      confidence: "POLICY",
      export_value: "0.01",
    },
  },
  narrative: "2026-03: payment leg FULLY_EXPLAINED, bank leg PARTIALLY_EXPLAINED.",
  audit_events: [],
};

const GAP_EXPLANATION_WITH_WARNINGS: MonthGapExplanation = {
  ...GAP_EXPLANATION_BODY,
  warnings: [
    {
      code: "CANCELLED_ADSENSE_PAYMENTS",
      message: "1 CANCELLED AdSense payment row is excluded from the components.",
    },
  ],
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

// Route each fetch by URL so the gap panel and the net-revenue content can be
// driven with different responses in the same render.
const routeFetch = (opts: { gapExplanation?: () => Response }) => {
  (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
    (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes("/gap-explanation")) {
        return Promise.resolve(
          (opts.gapExplanation ?? (() => jsonResponse(GAP_EXPLANATION_BODY)))(),
        );
      }
      if (url.includes("/net-revenue")) {
        return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
      }
      return Promise.resolve(jsonResponse({}, 404));
    },
  );
};

const renderCommandView = (overrides?: {
  canViewFinance?: boolean;
  canViewPayments?: boolean;
  canViewBankReconciliation?: boolean;
}) => {
  return render(
    <TenantProvider initialSlug="ums">
      <CommandView
        canViewFinance={overrides?.canViewFinance ?? true}
        canViewPayments={overrides?.canViewPayments ?? true}
        canViewBankReconciliation={overrides?.canViewBankReconciliation ?? true}
      />
    </TenantProvider>,
  );
};

describe("GapNarrativePanel in CommandView", () => {
  it("renders both legs, evidence components, residuals, and the month narrative", async () => {
    routeFetch({});
    renderCommandView();

    expect(
      await screen.findByText("Payment leg · YouTube revenue → AdSense paid"),
    ).toBeInTheDocument();
    expect(
      screen.getByText("Bank leg · AdSense paid → bank received"),
    ).toBeInTheDocument();
    // Operand chains render formatted backend strings, never derived numbers.
    expect(screen.getByText("$930.00 → $900.00 · gap $30.00")).toBeInTheDocument();
    expect(screen.getByText("$900.00 → $880.00 · gap $20.00")).toBeInTheDocument();
    // Evidence components — fx_difference_usd finally reaches the UI here.
    expect(screen.getByText("AdSense payments not yet PAID")).toBeInTheDocument();
    expect(screen.getByText("Bank transfer fees")).toBeInTheDocument();
    expect(screen.getByText("Bank-side FX difference")).toBeInTheDocument();
    expect(screen.getByText("$12.00")).toBeInTheDocument();
    expect(screen.getByText("$5.00")).toBeInTheDocument();
    // Confidence badges use the API labels.
    expect(screen.getAllByText("HIGH").length).toBeGreaterThan(0);
    expect(screen.getAllByText("MEDIUM").length).toBeGreaterThan(0);
    // One residual row per leg.
    expect(screen.getAllByText("Unexplained residual")).toHaveLength(2);
    // Month narrative line plus the read-only close badge.
    expect(
      screen.getByText("2026-03: payment leg FULLY_EXPLAINED, bank leg PARTIALLY_EXPLAINED."),
    ).toBeInTheDocument();
    expect(screen.getByText("OPEN")).toBeInTheDocument();
    // Leg + header badges carry the status codes.
    expect(screen.getAllByText("PARTIALLY_EXPLAINED").length).toBeGreaterThan(0);
    expect(screen.getAllByText("FULLY_EXPLAINED").length).toBeGreaterThan(0);
  });

  it("renders warning rows when the backend attaches data caveats", async () => {
    routeFetch({
      gapExplanation: () => jsonResponse(GAP_EXPLANATION_WITH_WARNINGS),
    });
    renderCommandView();

    expect(
      await screen.findByText(
        "1 CANCELLED AdSense payment row is excluded from the components.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByText("CANCELLED_ADSENSE_PAYMENTS")).toBeInTheDocument();
    expect(screen.getByText("Warning")).toBeInTheDocument();
  });

  it("shows a loading state before the gap-explanation response resolves", async () => {
    let resolveGap: ((value: Response) => void) | undefined;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (input: RequestInfo | URL) => {
        const url = String(input);
        if (url.includes("/gap-explanation")) {
          return new Promise<Response>((resolve) => {
            resolveGap = resolve;
          });
        }
        if (url.includes("/net-revenue")) {
          return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
        }
        return Promise.resolve(jsonResponse({}, 404));
      },
    );
    renderCommandView();

    expect(await screen.findByText("Loading gap narrative…")).toBeInTheDocument();

    resolveGap?.(jsonResponse(GAP_EXPLANATION_BODY));
    await waitFor(() =>
      expect(
        screen.getByText("Payment leg · YouTube revenue → AdSense paid"),
      ).toBeInTheDocument(),
    );
  });

  it("renders the restricted band and fires NO fetch without the three-way grant", async () => {
    routeFetch({});
    renderCommandView({ canViewBankReconciliation: false });

    expect(await screen.findByText("Gap narrative restricted")).toBeInTheDocument();
    // Net revenue still renders for the finance-granted viewer.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // The composed endpoint was never called — no-fetch-when-restricted.
    const calls = (globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls;
    expect(
      calls.some((call) => String(call[0]).includes("/gap-explanation")),
    ).toBe(false);
  });

  it("degrades a malformed 200 body to the empty state without crashing the view", async () => {
    // Contract-drift guard: a truthy body without the leg objects must render
    // the panel's empty state — never throw inside CommandView (the regression
    // the existing CommandView tests' 200-{} fetch fallback exposed).
    routeFetch({ gapExplanation: () => jsonResponse({}) });
    renderCommandView();

    expect(await screen.findByText("No gap explanation returned")).toBeInTheDocument();
    // The surrounding view keeps rendering.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
  });

  it("fails independently: a gap-explanation 403 is contained to the panel", async () => {
    routeFetch({
      gapExplanation: () =>
        jsonResponse({ detail: "Missing permission: finance.view_revenue" }, 403),
    });
    renderCommandView();

    // Net-revenue content renders despite the panel 403.
    await waitFor(() =>
      expect(screen.getAllByText("UC-DRAMA-01").length).toBeGreaterThan(0),
    );
    // The panel surfaces the no-permission copy without crashing the view.
    expect(screen.getAllByText("No permission").length).toBeGreaterThan(0);
    expect(screen.getByText("Gap narrative")).toBeInTheDocument();
  });
});
