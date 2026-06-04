// frontend/src/components/srcc/__tests__/AppShell.test.tsx
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AppShell from "@/components/srcc/AppShell";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

// Minimal real-shaped net-revenue body so the wired CommandView can render
// without errors while these tests focus on the tenant bootstrap behavior.
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

function urlOf(input: unknown): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

function isTenantCall(input: unknown): boolean {
  return urlOf(input).includes("/tenants/me");
}

// Route fetch by URL: /tenants/me -> the provided tenant responder, everything
// else (the wired CommandView net-revenue call) -> a neutral net-revenue body.
function routeFetch(tenantResponder: () => Response) {
  return (input: unknown) =>
    Promise.resolve(
      isTenantCall(input) ? tenantResponder() : jsonResponse(NET_REVENUE_BODY),
    );
}

function tenantFetchCalls() {
  const mock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  return mock.mock.calls.filter(([input]) => isTenantCall(input));
}

describe("AppShell tenant proof tag", () => {
  it("hydrates the tenant and shows UMS (ums) on the dev-only tag", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetch(() =>
        jsonResponse({
          id: "00000000-0000-0000-0000-000000000001",
          slug: "ums",
          display_name: "UMS",
        }),
      ),
    );
    render(
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    // findByTestId only waits for the element; the tag first renders the
    // "(resolving…) (loading…)" placeholder, so wait for the resolved text before
    // asserting (mirrors the other async tenant assertions in this file).
    await waitFor(() => expect(tag.textContent).toContain("UMS (ums)"));
    expect(tag.textContent).toContain("00000000-0000-0000-0000-000000000001");
  });

  it("shows the typed ApiError message on 503", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetch(() => jsonResponse({ detail: "Tenant registry unavailable" }, 503)),
    );
    render(
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    // The tag first renders the "(loading…)" placeholder; wait for the rejected
    // /tenants/me promise to settle and surface the 503 before asserting.
    await waitFor(() => expect(tag.textContent).toMatch(/503/));
  });

  it("appends the JSON body.detail string to the proof tag when present", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetch(() => jsonResponse({ detail: "Tenant registry unavailable" }, 503)),
    );
    render(
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    // The tag first renders "Tenant: ums (loading…)"; wait for the rejected
    // /tenants/me promise to settle and surface the failure detail (mirrors the
    // adjacent successful-retry test that also waits on the post-settle text).
    await waitFor(() =>
      expect(tag.textContent).toContain("Tenant registry unavailable"),
    );
  });

  it("fires the bootstrap /tenants/me fetch exactly once under <StrictMode> (re-entry guard)", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockImplementation(
      routeFetch(() =>
        jsonResponse({
          id: "00000000-0000-0000-0000-000000000001",
          slug: "ums",
          display_name: "UMS",
        }),
      ),
    );
    render(
      <StrictMode>
        <TenantProvider>
          <AppShell />
        </TenantProvider>
      </StrictMode>,
    );
    await screen.findByTestId("tenant-proof");
    // The wired CommandView fires its own net-revenue call; the re-entry guard
    // is specifically about the single /tenants/me bootstrap call.
    expect(tenantFetchCalls()).toHaveLength(1);
  });

  it("clears stale tenantError on successful retry after an earlier failure (outside-diff CodeRabbit regression)", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    // First /tenants/me call fails (503); the next succeeds. Net-revenue calls
    // are routed separately so they do not consume the tenant responders.
    let tenantCallCount = 0;
    fetchMock.mockImplementation(
      routeFetch(() => {
        tenantCallCount += 1;
        return tenantCallCount === 1
          ? jsonResponse({ detail: "transient 503" }, 503)
          : jsonResponse({
              id: "00000000-0000-0000-0000-000000000001",
              slug: "ums",
              display_name: "UMS",
            });
      }),
    );
    render(
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    await waitFor(() => expect(tag.textContent).toMatch(/503/));

    // Switch role → displayedRole changes → effect re-fires.
    // After the prior 503, hasRequestedTenantRef was reset to false and
    // tenant.id is still null, so the guard allows the retry which consumes
    // the success mock above.
    const roleSelect = screen.getByLabelText(/current role/i) as HTMLSelectElement;
    fireEvent.change(roleSelect, { target: { value: "finance" } });

    await waitFor(() => expect(tag.textContent).toContain("UMS (ums)"));
    expect(tag.textContent).not.toMatch(/503/);
    expect(tag.textContent).not.toContain("transient 503");
  });

  it("fires the bootstrap /tenants/me call without X-UMS-Tenant so the gateway is the tenant authority", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockImplementation(
      routeFetch(() =>
        jsonResponse({
          id: "00000000-0000-0000-0000-0000000000ac",
          slug: "acme",
          display_name: "Acme Holdings",
        }),
      ),
    );
    render(
      <TenantProvider>
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    // findByTestId only waits for the element; wait for the resolved tenant text
    // so the assertion never reads the initial "(resolving…) (loading…)" placeholder.
    await waitFor(() =>
      expect(tag.textContent).toContain("Acme Holdings (acme)"),
    );
    const tenantCalls = tenantFetchCalls();
    expect(tenantCalls).toHaveLength(1);
    const lastCall = tenantCalls.at(-1);
    if (!lastCall) {
      throw new Error("expected a recorded /tenants/me fetch call");
    }
    const [, init] = lastCall;
    const sentHeaders = new Headers((init as RequestInit | undefined)?.headers);
    expect(sentHeaders.has("X-UMS-Tenant")).toBe(false);
  });
});
