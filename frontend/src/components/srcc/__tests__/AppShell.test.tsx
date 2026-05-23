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

describe("AppShell tenant proof tag", () => {
  it("hydrates the tenant and shows UMS (ums) on the dev-only tag", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({
        id: "00000000-0000-0000-0000-000000000001",
        slug: "ums",
        display_name: "UMS",
      }),
    );
    render(
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    expect(tag.textContent).toContain("UMS (ums)");
    expect(tag.textContent).toContain("00000000-0000-0000-0000-000000000001");
  });

  it("shows the typed ApiError message on 503", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "Tenant registry unavailable" }, 503),
    );
    render(
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    expect(tag.textContent).toMatch(/503/);
  });

  it("appends the JSON body.detail string to the proof tag when present", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "Tenant registry unavailable" }, 503),
    );
    render(
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    expect(tag.textContent).toContain("Tenant registry unavailable");
  });

  it("fires fetch exactly once under <StrictMode> (re-entry guard)", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: "00000000-0000-0000-0000-000000000001",
        slug: "ums",
        display_name: "UMS",
      }),
    );
    render(
      <StrictMode>
        <TenantProvider>
          <AppShell />
        </TenantProvider>
      </StrictMode>,
    );
    await screen.findByTestId("tenant-proof");
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("clears stale tenantError on successful retry after an earlier failure (outside-diff CodeRabbit regression)", async () => {
    const fetchMock = globalThis.fetch as ReturnType<typeof vi.fn>;
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "transient 503" }, 503))
      .mockResolvedValueOnce(
        jsonResponse({
          id: "00000000-0000-0000-0000-000000000001",
          slug: "ums",
          display_name: "UMS",
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
    fetchMock.mockResolvedValue(
      jsonResponse({
        id: "00000000-0000-0000-0000-0000000000ac",
        slug: "acme",
        display_name: "Acme Holdings",
      }),
    );
    render(
      <TenantProvider>
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    expect(tag.textContent).toContain("Acme Holdings (acme)");
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [, init] = fetchMock.mock.calls.at(-1)!;
    const sentHeaders = new Headers((init as RequestInit | undefined)?.headers);
    expect(sentHeaders.has("X-UMS-Tenant")).toBe(false);
  });
});
