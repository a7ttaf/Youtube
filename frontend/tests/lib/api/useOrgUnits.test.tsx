import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { OrgUnit } from "@/lib/api/types";
import { useOrgUnits } from "@/lib/api/useOrgUnits";
import { TenantProvider } from "@/contexts/TenantContext";

const wrapper = ({ children }: { children: React.ReactNode }) => {
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
};

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const ORG_UNITS: OrgUnit[] = [
  {
    id: "11111111-1111-1111-1111-111111111111",
    parent_id: null,
    type: "SECTOR",
    name: "TV",
    active: true,
  },
  {
    id: "22222222-2222-2222-2222-222222222222",
    parent_id: "11111111-1111-1111-1111-111111111111",
    type: "COMPANY",
    name: "United Studios",
    active: true,
  },
];

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

const requireFetchArgs = () => {
  const args = fetchMock().mock.calls.at(-1);
  if (!args) throw new Error("expected fetch to have been called");
  return args;
};

describe("useOrgUnits", () => {
  it("auto-fetches GET /org-units once on mount and returns {data, loading, error, reload}", async () => {
    fetchMock().mockResolvedValue(jsonResponse(ORG_UNITS));
    const { result } = renderHook(() => useOrgUnits(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(urlOf(requireFetchArgs()[0])).toBe("/org-units");
    expect(result.current.data).toHaveLength(2);
    expect(result.current.data?.[0]?.id).toBe(
      "11111111-1111-1111-1111-111111111111",
    );
    expect(result.current.data?.[1]?.name).toBe("United Studios");
    expect(result.current.error).toBeNull();
    expect(typeof result.current.reload).toBe("function");
  });

  it("returns loading: true before the response arrives", () => {
    fetchMock().mockImplementation(
      () => new Promise<Response>(() => { /* never resolves */ }),
    );
    const { result } = renderHook(() => useOrgUnits(), { wrapper });
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();
  });

  it("returns an empty array when the API returns an empty list", async () => {
    fetchMock().mockResolvedValue(jsonResponse([]));
    const { result } = renderHook(() => useOrgUnits(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toEqual([]);
    expect(result.current.error).toBeNull();
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: analytics.view" }, 403),
    );
    const { result } = renderHook(() => useOrgUnits(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse(ORG_UNITS));
    const { result } = renderHook(() => useOrgUnits(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });
});
