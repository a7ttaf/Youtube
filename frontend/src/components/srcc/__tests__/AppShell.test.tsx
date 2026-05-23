// frontend/src/components/srcc/__tests__/AppShell.test.tsx
import { render, screen } from "@testing-library/react";
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
      <TenantProvider>
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
      <TenantProvider>
        <AppShell />
      </TenantProvider>,
    );
    const tag = await screen.findByTestId("tenant-proof");
    expect(tag.textContent).toMatch(/503/);
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
});
