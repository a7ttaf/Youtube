import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { useState, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AppShell from "@/components/srcc/AppShell";
import { SessionProvider } from "@/contexts/SessionContext";
import { TenantProvider } from "@/contexts/TenantContext";
import { useWriteInFlightControl } from "@/contexts/WriteInFlightContext";
import type { SessionMe } from "@/lib/api/types";

const { SENSITIVE_ERROR, WRITE_CRASH, WRITE_GATE } = vi.hoisted(() => {
  const error = new Error("groups-message-secret");
  error.name = "GroupsTenantSecretError";
  error.stack = "groups-stack-secret at /private/groups.tsx:44";
  const writeCrash = new Error("apply-result-secret");
  writeCrash.name = "ApplyRenderSecretError";
  return {
    SENSITIVE_ERROR: error,
    WRITE_CRASH: writeCrash,
    WRITE_GATE: {
      pending: Promise.resolve(),
      release: (() => undefined) as (value?: void) => void,
    },
  };
});

vi.mock("@/components/srcc/views/GroupsView", () => ({
  GroupsView: (): ReactNode => {
    throw SENSITIVE_ERROR;
  },
}));

vi.mock("@/components/srcc/views/RegistryView", () => ({
  default: (): ReactNode => {
    const write = useWriteInFlightControl();
    const [crashed, setCrashed] = useState(false);
    if (crashed) throw WRITE_CRASH;
    const startWrite = () => {
      write.arm("An import apply is running and cannot be aborted.");
      WRITE_GATE.pending.then(write.release, write.release);
      setCrashed(true);
    };
    return <button onClick={startWrite}>Start pending apply and crash</button>;
  },
}));

const ORIGINAL_FETCH = globalThis.fetch;
const SAFE_DIAGNOSTIC = "[ErrorBoundary] view_render_failed";
let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

const FULL_SESSION: SessionMe = {
  user_id: "00000000-0000-0000-0000-0000000000aa",
  email: "dev@ums.local",
  tenant: { id: "t1", slug: "ums", display_name: "UMS" },
  roles: [],
  permissions: [],
  is_service_account: false,
  disabled: false,
  capabilities: {
    canViewRevenue: true,
    canViewRevenueGlobal: true,
    canViewConfidence: true,
    canViewPayments: true,
    canViewBankReconciliation: true,
    paymentsViewScopes: { globalScope: true, financeMonths: [] },
    bankReconciliationViewScopes: { globalScope: true, financeMonths: [] },
    canCloseMonth: true,
    canUnlockMonth: true,
    canChangeAllocation: true,
    canExportRevenue: true,
    canExportAnalyticsReports: true,
    canManageRegistry: true,
    canManageGroups: true,
    canImportChannels: true,
    canManageConnectors: true,
    canViewConnectorHealth: true,
    canRunConnectorJobs: true,
    canViewAudit: true,
    canViewAnalytics: true,
  },
};

const NET_REVENUE_BODY = {
  month: "2026-08",
  status: "CALCULATED",
  channel_count: 0,
  calculated_channel_count: 0,
  missing_net_source_count: 0,
  pending_manual_override_count: 0,
  total_adjusted_gross_revenue_usd: "0",
  total_net_revenue_usd: "0",
  total_deduction_amount_usd: "0",
  total_channel_direct_deduction_amount_usd: "0",
  total_account_allocated_deduction_amount_usd: "0",
  unallocated_account_deduction_total_usd: null,
  unallocated_account_issues: null,
  channels: [],
  currency: "USD",
  allocation_source: "live_compute",
  committed_run: null,
  audit_events: [],
};

const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const routeShellFetch = (input: RequestInfo | URL): Promise<Response> => {
  const url = String(input);
  if (url.includes("/session/me")) return Promise.resolve(jsonResponse(FULL_SESSION));
  if (url.includes("/tenants/me")) {
    return Promise.resolve(
      jsonResponse({ id: "t1", slug: "ums", display_name: "UMS" }),
    );
  }
  if (url.includes("/revenue/scopes")) {
    return Promise.resolve(
      jsonResponse({
        scopes: [{ scope_type: "global", scope_id: null, label: "Global" }],
      }),
    );
  }
  if (url.includes("/rankings")) {
    return Promise.resolve(
      jsonResponse({
        month: "2026-08",
        metric: "gross",
        channels: [],
        companies: [],
        sectors: [],
        committed_run: null,
      }),
    );
  }
  if (url.includes("/smart-alerts")) {
    return Promise.resolve(
      jsonResponse({
        month: "2026-08",
        status: "CLEAR",
        highest_severity: null,
        alert_count: 0,
        alerts: [],
        audit_events: [],
      }),
    );
  }
  if (url.includes("/net-revenue")) return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
  return Promise.resolve(jsonResponse({ detail: "not under test" }, 404));
};

beforeEach(() => {
  WRITE_GATE.pending = new Promise<void>((resolve) => {
    WRITE_GATE.release = resolve;
  });
  vi.stubGlobal("fetch", vi.fn(routeShellFetch));
  globalThis.localStorage.clear();
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const renderShell = () =>
  render(
    <SessionProvider>
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>
    </SessionProvider>,
    { onCaughtError: () => undefined },
  );

const navButton = (label: string): HTMLElement => {
  const sidebar = screen.getByRole("complementary", { name: "Primary navigation" });
  const button = within(sidebar).getByText(label).closest("button");
  if (!button) throw new Error(`no nav button for ${label}`);
  return button;
};

const comboboxOptionLabels = (scope: HTMLElement): string[] =>
  within(scope)
    .queryAllByRole("combobox")
    .flatMap((box) => Array.from(box.querySelectorAll("option")))
    .map((option) => option.textContent?.trim() ?? "");

describe("AppShell factual chrome", () => {
  it("lists exact view labels without fabricated count badges", async () => {
    renderShell();
    const sidebar = await screen.findByRole("complementary", {
      name: "Primary navigation",
    });

    expect(
      within(sidebar)
        .getAllByRole("button")
        .map((button) => button.textContent?.trim()),
    ).toEqual([
      "Command Center",
      "Channel Registry",
      "CMS Groups",
      "Month Close",
      "Trace Explorer",
      "Exports",
      "Connectors",
      "Audit Log",
    ]);
  });

  it("keeps rolling Month but removes inert scope, currency, refresh, and export controls", async () => {
    renderShell();
    const filters = await screen.findByRole("group", { name: "Report filters" });

    expect(within(filters).getByLabelText("Month")).toBeInTheDocument();
    expect(within(filters).queryByLabelText("Scope")).not.toBeInTheDocument();
    expect(within(filters).queryByLabelText(/currency/iu)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Refresh reports" }))
      .not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /create export/iu }))
      .not.toBeInTheDocument();
    expect(comboboxOptionLabels(document.body)).not.toEqual(
      expect.arrayContaining(["EGP", "AED"]),
    );
  });

  it("removes fabricated operational cues, workflow rail, and raw-file status", async () => {
    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });

    expect(screen.queryByLabelText("Operational status")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Month close workflow")).not.toBeInTheDocument();
    expect(screen.queryByText(/2 blockers before export/iu)).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /open close/iu }))
      .not.toBeInTheDocument();
    expect(within(screen.getByLabelText("Role permission state"))
      .queryByText(/raw files gated/iu)).not.toBeInTheDocument();
  });
});

describe("AppShell view error boundary", () => {
  it("renders a healthy view without a fallback", async () => {
    renderShell();

    expect(
      await screen.findByRole("heading", { name: "Revenue Command Center", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it("contains a crashing view without exposing its payload or unmounting chrome", async () => {
    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });

    fireEvent.click(navButton("CMS Groups"));

    const fallback = await screen.findByTestId("view-error-fallback");
    expect(fallback.textContent).not.toMatch(
      /GroupsTenantSecretError|groups-message-secret|groups-stack-secret/u,
    );
    expect(screen.getByRole("complementary", { name: "Primary navigation" }))
      .toBeInTheDocument();
    expect(navButton("Command Center")).toBeEnabled();
    expect(consoleErrorSpy.mock.calls).toEqual([[SAFE_DIAGNOSTIC]]);
  });

  it("clears the caught state when navigation remounts the keyed boundary", async () => {
    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });
    fireEvent.click(navButton("CMS Groups"));
    await screen.findByTestId("view-error-fallback");

    fireEvent.click(navButton("Command Center"));

    await waitFor(() =>
      expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument(),
    );
  });

  it("keeps recovery and navigation latched when a view crashes during an unabortable apply", async () => {
    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });
    fireEvent.click(navButton("Channel Registry"));
    fireEvent.click(
      await screen.findByRole("button", { name: "Start pending apply and crash" }),
    );

    const fallback = await screen.findByTestId("view-error-fallback");
    expect(fallback.textContent).not.toMatch(/apply-result-secret/iu);
    expect(navButton("Command Center")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Try again" })).toBeDisabled();
    expect(fallback).toHaveTextContent(/wait for the active write to finish/iu);

    await act(async () => {
      WRITE_GATE.release();
      await WRITE_GATE.pending;
    });
    await waitFor(() => expect(navButton("Command Center")).toBeEnabled());
    expect(screen.getByRole("button", { name: "Try again" })).toBeEnabled();
  });
});
