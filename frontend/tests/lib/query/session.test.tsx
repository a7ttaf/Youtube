import {
  focusManager,
  onlineManager,
  QueryClient,
  QueryClientProvider,
} from "@tanstack/react-query";
import { useLayoutEffect, type ReactNode } from "react";
import { act, fireEvent, render, renderHook, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { SessionProvider, useSession, useSessionBootstrap } from "@/contexts/SessionContext";
import { TenantProvider, useTenant } from "@/contexts/TenantContext";
import type { SessionMe } from "@/lib/api/types";
import {
  SESSION_ME_QUERY_KEY,
  sessionMeQueryKey,
  useSessionMeQuery,
} from "@/lib/query/session";

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

const sessionFor = (
  userId: string,
  overrides: Partial<SessionMe> = {},
): SessionMe => ({
  ...SESSION_BODY,
  user_id: userId,
  email: `${userId}@ums.local`,
  ...overrides,
});

const TENANT_A = { id: "tenant-1", slug: "tenant-a", display_name: "Tenant A" };
const TENANT_B = { id: "tenant-2", slug: "tenant-b", display_name: "Tenant B" };

const SessionProbe = ({ frames }: { frames?: string[] }) => {
  const bootstrap = useSessionBootstrap();
  const { clearSession } = useSession();
  const tenant = useTenant();
  useLayoutEffect(() => {
    frames?.push(
      [
        bootstrap.status,
        bootstrap.session?.user_id ?? "none",
        String(bootstrap.session?.disabled ?? false),
        tenant.tenantSlug,
      ].join("|"),
    );
  });
  return (
    <div
      data-testid="session-probe"
      data-status={bootstrap.status}
      data-user-id={bootstrap.session?.user_id ?? ""}
      data-disabled={String(bootstrap.session?.disabled ?? false)}
      data-tenant-slug={tenant.tenantSlug}
    >
      {bootstrap.session?.email ?? bootstrap.error?.message ?? bootstrap.status}
      <button type="button" onClick={() => tenant.hydrate(TENANT_B)}>
        Switch tenant
      </button>
      <button type="button" onClick={() => tenant.hydrate(TENANT_A)}>
        Adopt tenant
      </button>
      <button type="button" onClick={clearSession}>
        Clear session
      </button>
    </div>
  );
};

const renderSession = (
  queryClient: QueryClient,
  tenantSlug = "ums",
  frames?: string[],
) =>
  render(
    <QueryClientProvider client={queryClient}>
      <SessionProvider>
        <TenantProvider initialSlug={tenantSlug}>
          <SessionProbe frames={frames} />
        </TenantProvider>
      </SessionProvider>
    </QueryClientProvider>,
  );

const sessionQueryClient = () =>
  new QueryClient({
    defaultOptions: {
      queries: {
        retry: false,
        staleTime: 30_000,
      },
    },
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
  focusManager.setFocused(undefined);
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

  it("keeps an explicit provider seed authoritative without a network request", () => {
    const seededPrincipal = sessionFor("seeded-principal", { tenant: TENANT_A });
    const queryClient = sessionQueryClient();

    const rendered = render(
      <QueryClientProvider client={queryClient}>
        <SessionProvider initialSession={seededPrincipal}>
          <TenantProvider>
            <SessionProbe />
          </TenantProvider>
        </SessionProvider>
      </QueryClientProvider>,
    );

    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "ready");
    expect(screen.getByTestId("session-probe")).toHaveAttribute(
      "data-user-id",
      "seeded-principal",
    );
    expect(globalThis.fetch).not.toHaveBeenCalled();
    rendered.unmount();
  });

  it("namespaces the exact session cache key by both provider lifetime and tenant", () => {
    expect(sessionMeQueryKey(41, "tenant-a")).toEqual([
      ...SESSION_ME_QUERY_KEY,
      41,
      "tenant-a",
    ]);
    expect(sessionMeQueryKey(41, "tenant-a")).not.toEqual(
      sessionMeQueryKey(41, "tenant-b"),
    );
    expect(sessionMeQueryKey(41, "tenant-a")).not.toEqual(
      sessionMeQueryKey(42, "tenant-a"),
    );
  });

  it("ignores a fresh principal cached under the legacy constant key", async () => {
    const queryClient = sessionQueryClient();
    queryClient.setQueryData(SESSION_ME_QUERY_KEY, sessionFor("principal-a"));
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(sessionFor("principal-b")),
    );

    const rendered = renderSession(queryClient, "tenant-b");
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "loading");
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    expect(screen.queryByText("principal-a@ums.local")).not.toBeInTheDocument();
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );
    expect(globalThis.fetch).toHaveBeenCalledTimes(1);
    rendered.unmount();
  });

  it("does not hydrate principal A or tenant A when a provider remounts for principal B", async () => {
    const principalA = sessionFor("principal-a");
    const principalB = sessionFor("principal-b", {
      tenant: { id: "tenant-2", slug: "tenant-b", display_name: "Tenant B" },
    });
    const responses = [jsonResponse(principalA), jsonResponse(principalB)];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      Promise.resolve(responses.shift() ?? jsonResponse({ detail: "missing response" }, 500)),
    );
    const queryClient = sessionQueryClient();

    const first = renderSession(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );
    first.unmount();

    const second = renderSession(queryClient, "tenant-b");
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "loading");
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    const secondRequestHeaders = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[1]?.[1]?.headers as Headers;
    expect(secondRequestHeaders.get("X-UMS-Tenant")).toBe("tenant-b");
    expect(queryClient.getQueriesData({ queryKey: SESSION_ME_QUERY_KEY })).toHaveLength(1);
    second.unmount();
  });

  it("fails closed and rehydrates when the tenant changes inside the same provider", async () => {
    const principalA = sessionFor("principal-a", { tenant: TENANT_A });
    const principalB = sessionFor("principal-b", { tenant: TENANT_B });
    let resolveReplacement: ((response: Response) => void) | undefined;
    const replacementPending = new Promise<Response>((resolve) => {
      resolveReplacement = resolve;
    });
    const responses: Array<Response | Promise<Response>> = [
      jsonResponse(principalA),
      replacementPending,
    ];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      responses.shift() ?? Promise.reject(new Error("missing response")),
    );
    const queryClient = sessionQueryClient();

    const rendered = renderSession(queryClient, TENANT_A.slug);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Switch tenant" }));
    expect(screen.getByTestId("session-probe")).toHaveAttribute(
      "data-tenant-slug",
      TENANT_B.slug,
    );
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "loading");
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    expect(screen.queryByText("principal-a@ums.local")).not.toBeInTheDocument();

    resolveReplacement?.(jsonResponse(principalB));
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    const replacementHeaders = (globalThis.fetch as ReturnType<typeof vi.fn>).mock
      .calls[1]?.[1]?.headers as Headers;
    expect(replacementHeaders.get("X-UMS-Tenant")).toBe(TENANT_B.slug);
    rendered.unmount();
  });

  it("revalidates a same-tenant principal after the mounted SPA regains focus", async () => {
    const principalA = sessionFor("principal-a", { tenant: TENANT_A });
    const principalB = sessionFor("principal-b", { tenant: TENANT_A });
    let resolveReplacement: ((response: Response) => void) | undefined;
    const replacementPending = new Promise<Response>((resolve) => {
      resolveReplacement = resolve;
    });
    const responses: Array<Response | Promise<Response>> = [
      jsonResponse(principalA),
      replacementPending,
    ];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      responses.shift() ?? Promise.reject(new Error("missing response")),
    );
    const queryClient = sessionQueryClient();

    const rendered = renderSession(queryClient, TENANT_A.slug);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "loading");
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");

    resolveReplacement?.(jsonResponse(principalB));
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );
    expect(screen.getByTestId("session-probe")).toHaveAttribute(
      "data-tenant-slug",
      TENANT_A.slug,
    );
    rendered.unmount();
  });

  it.each([
    ["disabled", true],
    ["enabled", false],
  ])(
    "never exposes the empty-slug principal while tenant adoption resolves to %s principal B",
    async (_replacementState, disabled) => {
      const principalA = sessionFor("principal-a", { tenant: TENANT_A });
      const principalB = sessionFor("principal-b", {
        tenant: TENANT_A,
        disabled,
      });
      let resolveReplacement: ((response: Response) => void) | undefined;
      const replacementPending = new Promise<Response>((resolve) => {
        resolveReplacement = resolve;
      });
      const responses: Array<Response | Promise<Response>> = [
        jsonResponse(principalA),
        replacementPending,
      ];
      (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
        responses.shift() ?? Promise.reject(new Error("missing response")),
      );
      const queryClient = sessionQueryClient();
      const frames: string[] = [];

      const rendered = renderSession(queryClient, "", frames);
      await waitFor(() =>
        expect(screen.getByTestId("session-probe")).toHaveAttribute(
          "data-user-id",
          "principal-a",
        ),
      );
      frames.length = 0;

      fireEvent.click(screen.getByRole("button", { name: "Adopt tenant" }));
      await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-status",
        "loading",
      );
      expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");

      resolveReplacement?.(jsonResponse(principalB));
      await waitFor(() =>
        expect(screen.getByTestId("session-probe")).toHaveAttribute(
          "data-user-id",
          "principal-b",
        ),
      );

      const loadingFrame = `loading|none|false|${TENANT_A.slug}`;
      const replacementFrame =
        `ready|principal-b|${String(disabled)}|${TENANT_A.slug}`;
      expect(frames[0]).toBe(loadingFrame);
      expect(
        frames.every(
          (frame) => frame === loadingFrame || frame === replacementFrame,
        ),
      ).toBe(true);
      expect(frames.at(-1)).toBe(replacementFrame);
      rendered.unmount();
    },
  );

  it("never re-exposes principal A when a same-tenant focus refetch fails", async () => {
    const principalA = sessionFor("principal-a", { tenant: TENANT_A });
    let rejectReplacement: ((error: Error) => void) | undefined;
    const replacementPending = new Promise<Response>((_resolve, reject) => {
      rejectReplacement = reject;
    });
    const responses: Array<Response | Promise<Response>> = [
      jsonResponse(principalA),
      replacementPending,
    ];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      responses.shift() ?? Promise.reject(new Error("missing response")),
    );
    const queryClient = sessionQueryClient();
    const frames: string[] = [];

    const rendered = renderSession(queryClient, TENANT_A.slug, frames);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );
    frames.length = 0;

    act(() => {
      focusManager.setFocused(false);
      focusManager.setFocused(true);
    });
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "loading");

    act(() => rejectReplacement?.(new Error("replacement gateway unavailable")));
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "error"),
    );

    const loadingFrame = `loading|none|false|${TENANT_A.slug}`;
    const errorFrame = `error|none|false|${TENANT_A.slug}`;
    expect(frames[0]).toBe(loadingFrame);
    expect(
      frames.every((frame) => frame === loadingFrame || frame === errorFrame),
    ).toBe(true);
    expect(frames.at(-1)).toBe(errorFrame);
    rendered.unmount();
  });

  it("never re-exposes principal A before reconnect resolves to disabled principal B", async () => {
    const principalA = sessionFor("principal-a", { tenant: TENANT_A });
    const principalB = sessionFor("principal-b", {
      tenant: TENANT_A,
      disabled: true,
    });
    let resolveReplacement: ((response: Response) => void) | undefined;
    const replacementPending = new Promise<Response>((resolve) => {
      resolveReplacement = resolve;
    });
    const responses: Array<Response | Promise<Response>> = [
      jsonResponse(principalA),
      replacementPending,
    ];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      responses.shift() ?? Promise.reject(new Error("missing response")),
    );
    const queryClient = sessionQueryClient();
    const frames: string[] = [];

    const rendered = renderSession(queryClient, TENANT_A.slug, frames);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );
    frames.length = 0;

    act(() => {
      onlineManager.setOnline(false);
      onlineManager.setOnline(true);
    });
    await waitFor(() => expect(globalThis.fetch).toHaveBeenCalledTimes(2));
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "loading");

    resolveReplacement?.(jsonResponse(principalB));
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );

    const loadingFrame = `loading|none|false|${TENANT_A.slug}`;
    const disabledReplacementFrame =
      `ready|principal-b|true|${TENANT_A.slug}`;
    expect(frames[0]).toBe(loadingFrame);
    expect(
      frames.every(
        (frame) => frame === loadingFrame || frame === disabledReplacementFrame,
      ),
    ).toBe(true);
    expect(frames.at(-1)).toBe(disabledReplacementFrame);
    rendered.unmount();
  });

  it("fails closed when same-provider tenant replacement hydration fails", async () => {
    let requestCount = 0;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() => {
      requestCount += 1;
      if (requestCount === 1) {
        return Promise.resolve(
          jsonResponse(sessionFor("principal-a", { tenant: TENANT_A })),
        );
      }
      return Promise.reject(new Error("replacement gateway unavailable"));
    });
    const queryClient = sessionQueryClient();

    const rendered = renderSession(queryClient, TENANT_A.slug);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Switch tenant" }));
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "error"),
    );
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    expect(screen.queryByText("principal-a@ums.local")).not.toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    rendered.unmount();
  });

  it("renders the disabled replacement after a same-provider tenant change", async () => {
    const responses = [
      jsonResponse(sessionFor("principal-a", { tenant: TENANT_A })),
      jsonResponse(sessionFor("principal-b", { tenant: TENANT_B, disabled: true })),
    ];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      Promise.resolve(responses.shift() ?? jsonResponse({ detail: "missing response" }, 500)),
    );
    const queryClient = sessionQueryClient();

    const rendered = renderSession(queryClient, TENANT_A.slug);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );

    fireEvent.click(screen.getByRole("button", { name: "Switch tenant" }));
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-disabled", "true");
    expect(screen.queryByText("principal-a@ums.local")).not.toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    rendered.unmount();
  });

  it("fails closed after principal A when the replacement session request fails", async () => {
    let requestCount = 0;
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() => {
      requestCount += 1;
      if (requestCount === 1) return Promise.resolve(jsonResponse(sessionFor("principal-a")));
      return Promise.reject(new Error("replacement gateway unavailable"));
    });
    const queryClient = sessionQueryClient();

    const first = renderSession(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );
    first.unmount();

    const second = renderSession(queryClient);
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "error"),
    );
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    expect(screen.queryByText("principal-a@ums.local")).not.toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    const cachedUserIds = queryClient
      .getQueriesData<SessionMe>({ queryKey: SESSION_ME_QUERY_KEY })
      .map(([, data]) => data?.user_id);
    expect(cachedUserIds).not.toContain("principal-a");
    second.unmount();
  });

  it("renders a replacement disabled principal, never the prior enabled principal", async () => {
    const principalB = sessionFor("principal-b", { disabled: true });
    const responses = [jsonResponse(sessionFor("principal-a")), jsonResponse(principalB)];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      Promise.resolve(responses.shift() ?? jsonResponse({ detail: "missing response" }, 500)),
    );
    const queryClient = sessionQueryClient();

    const first = renderSession(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );
    first.unmount();

    const second = renderSession(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-disabled", "true");
    expect(screen.queryByText("principal-a@ums.local")).not.toBeInTheDocument();
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    const cachedUserIds = queryClient
      .getQueriesData<SessionMe>({ queryKey: SESSION_ME_QUERY_KEY })
      .map(([, data]) => data?.user_id);
    expect(cachedUserIds).toEqual(["principal-b"]);
    second.unmount();
  });

  it("clears the current session query and context before a logout-boundary refetch", async () => {
    const replacement = sessionFor("principal-b");
    let resolveReplacement: ((response: Response) => void) | undefined;
    const replacementPending = new Promise<Response>((resolve) => {
      resolveReplacement = resolve;
    });
    const responses: Array<Response | Promise<Response>> = [
      jsonResponse(sessionFor("principal-a")),
      replacementPending,
    ];
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(() =>
      responses.shift() ?? Promise.reject(new Error("missing response")),
    );
    const queryClient = sessionQueryClient();

    const rendered = renderSession(queryClient);
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-a",
      ),
    );
    fireEvent.click(screen.getByRole("button", { name: "Clear session" }));
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-status", "loading");
    expect(screen.getByTestId("session-probe")).toHaveAttribute("data-user-id", "");
    const cachedUserIds = queryClient
      .getQueriesData<SessionMe>({ queryKey: SESSION_ME_QUERY_KEY })
      .map(([, data]) => data?.user_id);
    expect(cachedUserIds).not.toContain("principal-a");

    resolveReplacement?.(jsonResponse(replacement));
    await waitFor(() =>
      expect(screen.getByTestId("session-probe")).toHaveAttribute(
        "data-user-id",
        "principal-b",
      ),
    );
    expect(globalThis.fetch).toHaveBeenCalledTimes(2);
    rendered.unmount();
  });
});
