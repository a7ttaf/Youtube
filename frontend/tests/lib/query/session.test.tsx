import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import type { ReactNode } from "react";
import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { TenantProvider } from "@/contexts/TenantContext";
import type { SessionMe } from "@/lib/api/types";
import { useSessionMeQuery } from "@/lib/query/session";

const ORIGINAL_FETCH = globalThis.fetch;

const SESSION_BODY: SessionMe = {
  user_id: "00000000-0000-0000-0000-0000000000aa",
  email: "session-query@ums.local",
  tenant: { id: "tenant-1", slug: "ums", display_name: "UMS" },
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

const jsonResponse = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });

const createWrapper = () => {
  const queryClient = new QueryClient({
    defaultOptions: { queries: { retry: false } },
  });
  return ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>
      <TenantProvider initialSlug="ums">{children}</TenantProvider>
    </QueryClientProvider>
  );
};

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

describe("useSessionMeQuery", () => {
  it("reads the authoritative session endpoint through the shared query boundary", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(SESSION_BODY),
    );

    const { result } = renderHook(() => useSessionMeQuery(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toEqual(SESSION_BODY);
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    expect((globalThis.fetch as ReturnType<typeof vi.fn>).mock.calls[0]?.[0]).toBe(
      "/session/me",
    );
  });

  it("fails closed without retrying a rejected session request", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockRejectedValue(
      new Error("gateway unavailable"),
    );

    const { result } = renderHook(() => useSessionMeQuery(), {
      wrapper: createWrapper(),
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.data).toBeUndefined();
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
  });

  it("does not issue a request when a seeded session is already authoritative", () => {
    const { result } = renderHook(() => useSessionMeQuery(false), {
      wrapper: createWrapper(),
    });

    expect(result.current.fetchStatus).toBe("idle");
    expect(globalThis.fetch).not.toHaveBeenCalled();
  });
});
