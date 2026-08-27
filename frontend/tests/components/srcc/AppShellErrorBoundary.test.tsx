import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AppShell from "@/components/srcc/AppShell";
import { SessionProvider } from "@/contexts/SessionContext";
import { TenantProvider } from "@/contexts/TenantContext";
import type { SessionMe } from "@/lib/api/types";

// ============================================================================
// Purpose: Prove the boundary is actually WIRED into the shell, which the
//   component's own unit tests cannot show. Before this, a view that threw
//   during render tore down the entire React root — React 19 unmounts the tree
//   — leaving a blank page with no sidebar and no route back. These assert the
//   degraded shape instead: the crashed view becomes one card, the shell chrome
//   around it stays mounted, and navigating away clears the caught error.
// Standards: The crash is injected by MOCKING one view to throw rather than by
//   feeding a real view malformed data — the point under test is the shell's
//   containment, and a data-shaped crash would silently stop reproducing the
//   moment that view grew a guard of its own.
// Blast Radius: Test-only. Lives in its own file because the module mock is
//   hoisted per file and would break every other AppShell case.
// ============================================================================

// The crashing view. GroupsView is chosen because it is a plain named export
// with a single boolean prop, so the stub needs no fixture of its own.
vi.mock("@/components/srcc/views/GroupsView", () => ({
  GroupsView: (): ReactNode => {
    throw new TypeError("groups view exploded during render");
  },
}));

const ORIGINAL_FETCH = globalThis.fetch;

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

// A full-capability session so every nav item renders and these tests are about
// containment rather than permission gating.
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

// Minimal real-shaped net-revenue body so the wired CommandView renders without
// an error state of its own confusing the "healthy view" assertions.
const NET_REVENUE_BODY = {
  month: "2026-03",
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

/** Wrap a body in a JSON Response, mirroring the other shell test harnesses. */
const jsonResponse = (body: unknown, status = 200): Response =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

/** Normalize a fetch input (string | URL | Request) to its URL string. */
const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

/** Route the shell's two bootstrap reads; everything else gets net-revenue. */
const routeShellFetch = (input: unknown): Promise<Response> => {
  const url = urlOf(input);
  if (url.includes("/session/me")) return Promise.resolve(jsonResponse(FULL_SESSION));
  if (url.includes("/tenants/me")) {
    return Promise.resolve(
      jsonResponse({ id: "t1", slug: "ums", display_name: "UMS" }),
    );
  }
  return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn(routeShellFetch));
  globalThis.localStorage.clear();
  // React logs every caught error plus its component stack; the crash injected
  // here would otherwise bury the real assertion output.
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

/** True when the boundary's own componentDidCatch line reached the console. */
const boundaryLogged = (): boolean =>
  consoleErrorSpy.mock.calls.some((call) =>
    String(call[0]).includes("[ErrorBoundary]"),
  );

/** Render the shell inside the providers it needs, as the other suites do. */
const renderShell = () =>
  render(
    <SessionProvider>
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>
    </SessionProvider>,
  );

/**
 * The SIDEBAR button carrying this label. Scoped to the landmark because view
 * titles repeat the nav labels in the topbar heading.
 */
const navButton = (label: string): HTMLElement => {
  const sidebar = screen.getByRole("complementary", { name: "Primary navigation" });
  const button = within(sidebar).getByText(label).closest("button");
  if (button === null) throw new Error(`no nav button for ${label}`);
  return button;
};

describe("AppShell view error boundary", () => {
  it("renders a healthy view normally, with no fallback card", async () => {
    renderShell();

    expect(
      await screen.findByRole("heading", { name: "Revenue Command Center", level: 1 }),
    ).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
    expect(boundaryLogged()).toBe(false);
  });

  it("degrades a crashing view to a card and leaves the shell mounted", async () => {
    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });

    fireEvent.click(navButton("CMS Groups"));

    // The crashed view is one card...
    const fallback = await screen.findByTestId("view-error-fallback");
    expect(within(fallback).getByText("TypeError")).toBeInTheDocument();
    expect(
      within(fallback).getByRole("button", { name: "Try again" }),
    ).toBeInTheDocument();

    // ...and everything around it survived: the sidebar is still there, its
    // nav is still usable, and the topbar still names where the operator is.
    // Without the boundary this is a blank page — React unmounts the root.
    expect(
      screen.getByRole("complementary", { name: "Primary navigation" }),
    ).toBeInTheDocument();
    expect(navButton("Command Center")).toBeEnabled();
    expect(
      screen.getByRole("heading", { name: "CMS Groups", level: 1 }),
    ).toBeInTheDocument();
    expect(boundaryLogged()).toBe(true);
  });

  it("clears the caught error when the operator navigates to another view", async () => {
    // The boundary is keyed by the active view, so a switch REMOUNTS it. Held
    // without the key, a single crash would pin the fallback over every view
    // the operator visited afterwards.
    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });

    fireEvent.click(navButton("CMS Groups"));
    await screen.findByTestId("view-error-fallback");

    fireEvent.click(navButton("Command Center"));

    await waitFor(() =>
      expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument(),
    );
    expect(
      await screen.findByRole("heading", { name: "Revenue Command Center", level: 1 }),
    ).toBeInTheDocument();
  });
});
