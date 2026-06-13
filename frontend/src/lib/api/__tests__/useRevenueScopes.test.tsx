import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { RevenueScopeOption } from "@/lib/api/types";
import { useRevenueScopes } from "@/lib/api/useRevenueScopes";
import { TenantProvider } from "@/contexts/TenantContext";

function wrapper({ children }: { children: React.ReactNode }) {
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
}

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const SCOPES: RevenueScopeOption[] = [
  { scope_type: "global", scope_id: null, label: "Global" },
  { scope_type: "sector", scope_id: "sector-tv", label: "TV Sector" },
  { scope_type: "company", scope_id: "company-a", label: "Company Alpha" },
];

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchMock() {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

function urlOf(input: unknown): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

function requireFetchArgs() {
  const args = fetchMock().mock.calls.at(-1);
  if (!args) throw new Error("expected fetch to have been called");
  return args;
}

describe("useRevenueScopes", () => {
  it("auto-fetches GET /revenue/scopes on mount and returns the .scopes array", async () => {
    fetchMock().mockResolvedValue(jsonResponse({ scopes: SCOPES }));
    const { result } = renderHook(() => useRevenueScopes(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(urlOf(requireFetchArgs()[0])).toBe("/revenue/scopes");
    expect(result.current.data).toHaveLength(3);
    expect(result.current.data?.[0]?.scope_type).toBe("global");
    expect(result.current.data?.[0]?.scope_id).toBeNull();
    expect(result.current.data?.[2]?.scope_id).toBe("company-a");
    expect(result.current.data?.[2]?.label).toBe("Company Alpha");
    expect(result.current.error).toBeNull();
    expect(typeof result.current.reload).toBe("function");
  });

  it("returns loading: true before the response arrives", () => {
    fetchMock().mockImplementation(
      () => new Promise<Response>(() => { /* never resolves */ }),
    );
    const { result } = renderHook(() => useRevenueScopes(), { wrapper });
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: finance.view_revenue" }, 403),
    );
    const { result } = renderHook(() => useRevenueScopes(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse({ scopes: SCOPES }));
    const { result } = renderHook(() => useRevenueScopes(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });
});
