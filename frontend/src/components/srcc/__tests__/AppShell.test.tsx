// frontend/src/components/srcc/__tests__/AppShell.test.tsx
import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { StrictMode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import AppShell, { isImportScopeSettled } from "@/components/srcc/AppShell";
import { SessionProvider } from "@/contexts/SessionContext";
import { TenantProvider } from "@/contexts/TenantContext";
import type { SessionMe } from "@/lib/api/types";

const ORIGINAL_FETCH = globalThis.fetch;

// A real-shaped /session/me body with every capability granted so the wired
// dashboard renders and these tenant-proof tests focus on the tenant bootstrap.
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
    canViewConfidence: true,
    canViewPayments: true,
    canViewBankReconciliation: true,
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

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  // The unsettled-import flag mirrors into localStorage ON PURPOSE, so it
  // outlives a browser reload. That makes it leak between tests unless it is
  // cleared here — the leak is the feature working, not a bug to design away.
  globalThis.localStorage.clear();
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
});

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

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

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

// The two shell reads every render performs. Named once because they are used
// as BOTH route-map keys and substring probes: a literal that drifts in one
// place and not the other silently routes a test to the fallback responder.
const SESSION_ROUTE = "/session/me";
const TENANT_ROUTE = "/tenants/me";

// The one tenant every shell test resolves to; duplicated per route map before.
const SHELL_TENANT = { id: "t1", slug: "ums", display_name: "UMS" };

const isTenantCall = (input: unknown): boolean => {
  return urlOf(input).includes(TENANT_ROUTE);
};

const isSessionCall = (input: unknown): boolean => {
  return urlOf(input).includes(SESSION_ROUTE);
};

// Route fetch by URL: /session/me -> a ready full-capability session (so the
// dashboard renders), /tenants/me -> the provided tenant responder, everything
// else (the wired CommandView net-revenue call) -> a neutral net-revenue body.
const routeFetch = (tenantResponder: () => Response) => {
  return (input: unknown) => {
    if (isSessionCall(input)) return Promise.resolve(jsonResponse(FULL_SESSION));
    if (isTenantCall(input)) return Promise.resolve(tenantResponder());
    return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
  };
};

const tenantFetchCalls = () => {
  const mock = globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
  return mock.mock.calls.filter(([input]) => isTenantCall(input));
};

describe("import scope settling", () => {
  // The rule that decides whether an import may be ADMITTED at all. Its effect
  // on the button is covered in RegistryImportFlow.test.tsx.
  //
  // NOT covered here, stated so the gap is not mistaken for coverage: that a
  // FAILED /tenants/me reaches this predicate as `tenantSettled: false`. That
  // is one line in useTenantBootstrap (`tenant.id !== null`, with the former
  // `|| tenantError !== null` removed), and this predicate cannot see the
  // difference — a failure and an in-flight request both arrive as false. An
  // end-to-end assertion needs the shell driven through registry -> upload ->
  // preview, which this file has no fetch routing for.
  const sessionWith = (tenant: { id: string } | null) =>
    ({ ...FULL_SESSION, tenant }) as unknown as Parameters<typeof isImportScopeSettled>[0];

  it("is settled when the SESSION carries the tenant, bootstrap or not", () => {
    // Nothing has to resolve: the session's own tenant is authoritative.
    expect(isImportScopeSettled(sessionWith({ id: "t1" }), false)).toBe(true);
  });

  it("is NOT settled while a tenantless session is still resolving", () => {
    expect(isImportScopeSettled(sessionWith(null), false)).toBe(false);
  });

  it("is settled once a tenantless session resolves its tenant", () => {
    expect(isImportScopeSettled(sessionWith(null), true)).toBe(true);
  });
});

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
      <SessionProvider>
        <TenantProvider initialSlug="ums">
          <AppShell />
        </TenantProvider>
      </SessionProvider>,
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
      <SessionProvider>
        <TenantProvider initialSlug="ums">
          <AppShell />
        </TenantProvider>
      </SessionProvider>,
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
      <SessionProvider>
        <TenantProvider initialSlug="ums">
          <AppShell />
        </TenantProvider>
      </SessionProvider>,
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
        <SessionProvider>
          <TenantProvider>
            <AppShell />
          </TenantProvider>
        </SessionProvider>
      </StrictMode>,
    );
    await screen.findByTestId("tenant-proof");
    // The wired CommandView fires its own net-revenue call; the re-entry guard
    // is specifically about the single /tenants/me bootstrap call.
    await waitFor(() => expect(tenantFetchCalls()).toHaveLength(1));
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
      <SessionProvider>
        <TenantProvider initialSlug="ums">
          <AppShell />
        </TenantProvider>
      </SessionProvider>,
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

  it("renders the presentation-only hint beside the role preview switcher", async () => {
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
      <SessionProvider>
        <TenantProvider initialSlug="ums">
          <AppShell />
        </TenantProvider>
      </SessionProvider>,
    );
    // The preview switcher is shown (vitest runs with import.meta.env.DEV), so
    // the disclaimer that the role preview does not change backend authorization
    // must render alongside it.
    const switcher = await screen.findByLabelText(/current role/i);
    expect(switcher.tagName).toBe("SELECT");
    const hint = screen.getByTestId("role-preview-hint");
    expect(hint).toBeInTheDocument();
    expect(hint.textContent).toMatch(/presentation preview only/i);
    expect(hint.textContent).toMatch(/api permissions come from the dev gateway role/i);
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
      <SessionProvider>
        <TenantProvider>
          <AppShell />
        </TenantProvider>
      </SessionProvider>,
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

// ---------------------------------------------------------------- session hydration

// Build a /session/me body from a capability override + flags. Defaults to all
// capabilities false so each test opts INTO exactly the capabilities it asserts.
const sessionBody = (
  capabilities: Partial<SessionMe["capabilities"]> = {},
  overrides: Partial<SessionMe> = {},
): SessionMe => {
  return {
    ...FULL_SESSION,
    capabilities: {
      canViewRevenue: false,
      canViewConfidence: false,
      canViewPayments: false,
      canViewBankReconciliation: false,
      canCloseMonth: false,
      canUnlockMonth: false,
      canChangeAllocation: false,
      canExportRevenue: false,
      canExportAnalyticsReports: false,
      canManageRegistry: false,
      canManageGroups: false,
      canImportChannels: false,
      canManageConnectors: false,
      canViewConnectorHealth: false,
      canRunConnectorJobs: false,
      canViewAudit: false,
      canViewAnalytics: false,
      ...capabilities,
    },
    ...overrides,
  };
};

// Empty-but-real connector + AdSense list shapes so the Connectors view renders
// its always-visible controls without a malformed body.
const EMPTY_CONNECTOR_CREDENTIALS = {
  items: [],
  pagination: { limit: 50, offset: 0, returned: 0, has_more: false },
};
const EMPTY_ADSENSE_PAYMENTS = {
  items: [],
  pagination: { limit: 50, offset: 0, returned: 0, has_more: false },
  audit_event: {},
};
const EMPTY_EXPORTS = {
  items: [],
  pagination: { limit: 50, offset: 0, returned: 0, has_more: false },
};
// GET /groups returns a bare array (see useGroups.ts) — unlike the paginated
// wrapper shapes above. The Groups view fetches it unconditionally on mount.
const EMPTY_GROUPS: unknown[] = [];

// GET /connectors/content-owners returns the least-privilege picker payload
// ({items: [{account_id}]} — no pagination wrapper, see useContentOwners.ts);
// the manage-groups sync header fetches it on mount.
const EMPTY_CONTENT_OWNERS = { items: [] };

type FetchRouteMap = ReadonlyMap<string, () => Response>;

const defaultSessionRouteResponse = () => jsonResponse(NET_REVENUE_BODY);

const routeFetchWithSessionRoutes = (sessionResponder: () => Response): FetchRouteMap => new Map([
  [SESSION_ROUTE, sessionResponder],
  [TENANT_ROUTE, () => jsonResponse(SHELL_TENANT)],
  ["/connectors/credentials", () => jsonResponse(EMPTY_CONNECTOR_CREDENTIALS)],
  ["/connectors/content-owners", () => jsonResponse(EMPTY_CONTENT_OWNERS)],
  ["/adsense/payments", () => jsonResponse(EMPTY_ADSENSE_PAYMENTS)],
  ["/exports", () => jsonResponse(EMPTY_EXPORTS)],
  ["/groups", () => jsonResponse(EMPTY_GROUPS)],
]);

const requestPathOf = (input: unknown) =>
  new URL(urlOf(input), "http://ums.local").pathname;

const responseForRoutedSessionRequest = (routes: FetchRouteMap, input: unknown) =>
  (routes.get(requestPathOf(input)) ?? defaultSessionRouteResponse)();

// Route fetch with a caller-supplied /session/me responder; connector, AdSense,
// and export list calls get empty real-shaped bodies, tenant gets a fixed UMS
// body, and the net-revenue call gets the neutral body.
const routeFetchWithSession = (sessionResponder: () => Response) => {
  const routes = routeFetchWithSessionRoutes(sessionResponder);
  return (input: unknown) => {
    return Promise.resolve(responseForRoutedSessionRequest(routes, input));
  };
};

const renderShell = () => {
  return render(
    <SessionProvider>
      <TenantProvider initialSlug="ums">
        <AppShell />
      </TenantProvider>
    </SessionProvider>,
  );
};

describe("AppShell production session hydration", () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it("PRODUCTION: a successful /session/me hydration renders the live dashboard gated by capabilities", async () => {
    // Production: no dev role preview. Capabilities are authoritative.
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_ROLE_PREVIEW", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() =>
        jsonResponse(sessionBody({ canViewRevenue: true })),
      ),
    );
    renderShell();

    // No permanent access-denied screen; the live command dashboard renders.
    // canViewRevenue=true -> the role-state shows money visible (not withheld).
    expect(await screen.findByText(/money visible/i)).toBeInTheDocument();
    expect(screen.queryByText(/access denied/i)).not.toBeInTheDocument();
    // In production the role control is a read-only <output>, NOT a <select>
    // dropdown — the dev preview selector must not render.
    const roleControl = screen.getByLabelText(/current role/i);
    expect(roleControl.tagName).toBe("OUTPUT");
    expect(screen.queryByTestId("role-preview-hint")).not.toBeInTheDocument();
  });

  it("PRODUCTION: withheld canViewRevenue gates finance cells to the Restricted sentinel", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_ROLE_PREVIEW", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse(sessionBody({ canViewRevenue: false }))),
    );
    renderShell();

    // The dashboard renders (no access denied), but money is withheld.
    expect(await screen.findByText(/money withheld/i)).toBeInTheDocument();
    expect(screen.queryByText(/access denied/i)).not.toBeInTheDocument();
  });

  it("EXPORTS: analytics export plus revenue visibility enables analytics CSV creation", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_ROLE_PREVIEW", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() =>
        jsonResponse(
          sessionBody({
            canExportAnalyticsReports: true,
            canViewAnalytics: true,
            canViewRevenue: true,
          }),
        ),
      ),
    );
    renderShell();

    fireEvent.click(await screen.findByText("Exports"));
    expect(
      await screen.findByRole("heading", { name: "Export Center", level: 1 }),
    ).toBeInTheDocument();
    const reportType = screen.getByLabelText("Report type") as HTMLSelectElement;
    expect(reportType).not.toBeDisabled();
    expect(Array.from(reportType.options).map((option) => option.textContent)).toEqual([
      "Analytics summary (CSV)",
    ]);
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "Need it" },
    });
    expect(screen.getByRole("button", { name: /^generate$/i })).not.toBeDisabled();
  });

  it("FAIL CLOSED: a 401 /session/me hydration renders AccessDeniedState", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_ROLE_PREVIEW", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse({ detail: "Missing token" }, 401)),
    );
    renderShell();

    expect(await screen.findByText(/access denied/i)).toBeInTheDocument();
    expect(screen.getByText(/an authenticated session is required/i)).toBeInTheDocument();
  });

  it("FAIL CLOSED: a 403 /session/me hydration renders AccessDeniedState", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_ROLE_PREVIEW", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse({ detail: "Disabled or unknown" }, 403)),
    );
    renderShell();

    expect(await screen.findByText(/access denied/i)).toBeInTheDocument();
  });

  it("FAIL CLOSED: a network error on /session/me renders AccessDeniedState", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_ROLE_PREVIEW", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation((input: unknown) => {
      if (isSessionCall(input)) return Promise.reject(new Error("network down"));
      if (isTenantCall(input)) {
        return Promise.resolve(
          jsonResponse({ id: "t1", slug: "ums", display_name: "UMS" }),
        );
      }
      return Promise.resolve(jsonResponse(NET_REVENUE_BODY));
    });
    renderShell();

    expect(await screen.findByText(/access denied/i)).toBeInTheDocument();
  });

  it("FAIL CLOSED: a hydrated session with disabled=true renders AccessDeniedState", async () => {
    vi.stubEnv("DEV", false);
    vi.stubEnv("VITE_ENABLE_ROLE_PREVIEW", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() =>
        jsonResponse(sessionBody({ canViewRevenue: true }, { disabled: true })),
      ),
    );
    renderShell();

    expect(await screen.findByText(/access denied/i)).toBeInTheDocument();
    expect(screen.getByText(/this account is disabled/i)).toBeInTheDocument();
  });

  it("DEV PREVIEW: the role selector still renders and the shell hydrates from the session (preview not broken)", async () => {
    // vitest runs with import.meta.env.DEV truthy by default -> dev preview on.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse(sessionBody({ canViewRevenue: true }))),
    );
    renderShell();

    // The dashboard renders (hydrated) AND the dev role selector is present.
    expect(await screen.findByText(/money visible/i)).toBeInTheDocument();
    const switcher = await screen.findByLabelText(/current role/i);
    expect(switcher.tagName).toBe("SELECT");
    expect(screen.queryByText(/access denied/i)).not.toBeInTheDocument();
  });

  it("DEV PREVIEW: switching the dev role does NOT fabricate a capability the session lacks", async () => {
    // Session grants NO revenue. The dev role selector changing to "finance"
    // (which historically implied money-visible) must NOT make money visible.
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse(sessionBody({ canViewRevenue: false }))),
    );
    renderShell();

    const roleSelect = (await screen.findByLabelText(/current role/i)) as HTMLSelectElement;
    fireEvent.change(roleSelect, { target: { value: "finance" } });

    // Capabilities are authoritative: money stays withheld despite the finance label.
    await waitFor(() => expect(screen.getByText(/money withheld/i)).toBeInTheDocument());
  });

  it("CONNECTOR CONTROLS: disabled when canRunConnectorJobs=false (honest gating)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() =>
        jsonResponse(sessionBody({ canViewRevenue: true, canRunConnectorJobs: false })),
      ),
    );
    renderShell();

    // Navigate to the Connectors view.
    fireEvent.click(await screen.findByText("Connectors"));
    const reasonField = (await screen.findByLabelText(/sync reason/i)) as HTMLInputElement;
    expect(reasonField).toBeDisabled();
    const adsenseAccount = screen.getByLabelText(/account id/i) as HTMLInputElement;
    expect(adsenseAccount).toBeDisabled();
  });

  it("CONNECTOR CONTROLS: enabled when canRunConnectorJobs=true (even with finance off)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() =>
        jsonResponse(sessionBody({ canViewRevenue: false, canRunConnectorJobs: true })),
      ),
    );
    renderShell();

    fireEvent.click(await screen.findByText("Connectors"));
    const reasonField = (await screen.findByLabelText(/sync reason/i)) as HTMLInputElement;
    expect(reasonField).not.toBeDisabled();
    const adsenseAccount = screen.getByLabelText(/account id/i) as HTMLInputElement;
    expect(adsenseAccount).not.toBeDisabled();
  });

  it("CONNECTOR CONTROLS: finance admin (canViewRevenue, no canRunConnectorJobs) cannot run connector jobs", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() =>
        jsonResponse(
          sessionBody({
            canViewRevenue: true,
            canCloseMonth: true,
            canExportRevenue: true,
            canRunConnectorJobs: false,
          }),
        ),
      ),
    );
    renderShell();

    fireEvent.click(await screen.findByText("Connectors"));
    const reasonField = (await screen.findByLabelText(/sync reason/i)) as HTMLInputElement;
    // Finance visibility is on, but connector job controls stay disabled.
    expect(reasonField).toBeDisabled();
  });

  it("CONNECTOR CONTROLS: Run pull disabled when canRunConnectorJobs=false", async () => {
    // The per-row Run pull button only renders when at least one credential row
    // exists. routeFetchWithSession returns an EMPTY credentials list, so route
    // /connectors/credentials to a populated body here and delegate the rest to
    // the shared session router (without modifying the harness helper).
    const credentialsBody = {
      items: [
        {
          id: "11111111-1111-1111-1111-111111111111",
          connector_key: "youtube_reporting",
          account_id: "acct-1",
          status: "ACTIVE",
          has_secret_ref: true,
        },
      ],
      pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
    };
    const routed = routeFetchWithSession(() =>
      jsonResponse(
        sessionBody({ canViewRevenue: true, canRunConnectorJobs: false }),
      ),
    );
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      (input: unknown) => {
        if (urlOf(input).includes("/connectors/credentials")) {
          return Promise.resolve(jsonResponse(credentialsBody));
        }
        return routed(input);
      },
    );
    renderShell();
    fireEvent.click(await screen.findByText("Connectors"));
    const runPull = (await screen.findByRole("button", {
      name: /run pull/i,
    })) as HTMLButtonElement;
    expect(runPull).toBeDisabled();
  });
});

// ---------------------------------------------------------------- groups nav

describe("AppShell groups navigation", () => {
  it("GROUPS NAV: CMS Groups sits after Channel Registry in the Workspace group and renders the Groups view", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse(sessionBody({ canViewRevenue: true }))),
    );
    renderShell();

    // Nav order: the Workspace group lists CMS Groups after Channel Registry.
    const workspaceNav = await screen.findByRole("navigation", { name: "Workspace" });
    const itemLabels = within(workspaceNav)
      .getAllByRole("button")
      .map((button) => button.textContent ?? "");
    const registryIndex = itemLabels.findIndex((text) => text.includes("Channel Registry"));
    const groupsIndex = itemLabels.findIndex((text) => text.includes("CMS Groups"));
    expect(registryIndex).toBeGreaterThanOrEqual(0);
    expect(groupsIndex).toBeGreaterThan(registryIndex);

    // Clicking it renders the Groups view via its VIEW_COPY title as the page heading.
    fireEvent.click(await screen.findByText("CMS Groups"));
    expect(
      await screen.findByRole("heading", { name: "CMS Groups", level: 1 }),
    ).toBeInTheDocument();
  });

  it("GROUPS NAV: shows the manage-groups sync surface when canManageGroups is granted", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse(sessionBody({ canManageGroups: true }))),
    );
    renderShell();

    fireEvent.click(await screen.findByText("CMS Groups"));
    expect(await screen.findByRole("button", { name: /sync/i })).toBeInTheDocument();
  });

  it("GROUPS NAV: hides the manage-groups sync surface with the default all-false session", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeFetchWithSession(() => jsonResponse(sessionBody())),
    );
    renderShell();

    fireEvent.click(await screen.findByText("CMS Groups"));
    // Settle on the Groups view's loaded (empty) state before asserting absence.
    await screen.findByRole("heading", { name: "CMS Groups", level: 1 });
    expect(screen.queryByRole("button", { name: /sync/i })).not.toBeInTheDocument();
  });
});

// ============================================================================
// Shell navigation is the exit RegistryImportFlow cannot guard itself: the
// sidebar lives outside that component's tree, so switching views unmounts the
// flow no matter what its own Cancel and Back do. With an un-abortable apply
// POST pending, that means the write still commits while its completion
// handler is gone — no reload, no confirmation, and the operator left looking
// at a stale registry (review #184, codex P1). AppShell latches nav off
// WriteInFlightContext for exactly the duration of that write.
// ============================================================================

const IMPORT_CHANNELS = [
  {
    youtube_channel_id: "UC-DRAMA-01",
    channel_name: "UMS Drama",
    primary_company_id: "united-studios",
    cms_status: "INSIDE_CMS",
    content_owner_id: "OWNERaaa",
    revenue_required: true,
    revenue_source_status: "OFFICIAL_CMS_REVENUE",
    active: true,
  },
];

// A COMPLETE plan payload: useChannelImport structurally validates every 2xx
// (a body missing plan_fingerprint would reach the next Apply as `undefined`
// and silently unbind the write), so a shorthand fixture would be rejected
// before these nav-latch tests ever reached Preview.
const IMPORT_PLAN = {
  dry_run: true,
  content_owner_id: "OWNERaaa",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
  plan_fingerprint: "plan-appshell-v1",
  rows: [
    {
      row_number: 1,
      youtube_channel_id: "UCa",
      outcome: "CREATE",
      channel_name: "Alpha Channel",
      group_id: null,
      group_action: null,
      revenue_required: true,
      revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
      changes: {},
      reason: null,
    },
  ],
};

/** A pending Response plus its resolver, for holding the apply POST open. */
const deferredImportResponse = () => {
  let release!: (response: Response) => void;
  // `reject` models the LOST response — a transport failure, where the POST was
  // dispatched and never answered. That is a different outcome from any status
  // code, so the helper must be able to produce it.
  let fail!: (reason: unknown) => void;
  const pending = new Promise<Response>((resolve, reject) => {
    release = resolve;
    fail = reject;
  });
  // An unobserved rejection would fail the run before the flow catches it; the
  // flow's own catch is the observer, so keep the promise quiet until then.
  pending.catch(() => undefined);
  return { pending, release, reject: fail };
};

/** The SIDEBAR button carrying this label (the button also holds an icon +
 * count, and labels like "Channel Registry" also appear in the view itself,
 * so the lookup is scoped to the sidebar landmark). */
const navButton = (label: string): HTMLElement => {
  const sidebar = screen.getByRole("complementary", { name: "Primary navigation" });
  const button = within(sidebar).getByText(label).closest("button");
  if (button === null) throw new Error(`no nav button for ${label}`);
  return button;
};

// The reads this flow needs, keyed by pathname like routeFetchWithSessionRoutes.
// Only /channels/import is answered per test, so it is the sole branch in the
// router below rather than another entry here.
const IMPORT_SHELL_ROUTES: FetchRouteMap = new Map([
  [SESSION_ROUTE, () => jsonResponse(FULL_SESSION)],
  [TENANT_ROUTE, () => jsonResponse(SHELL_TENANT)],
  ["/channels", () => jsonResponse(IMPORT_CHANNELS)],
  ["/org-units", () => jsonResponse([])],
  ["/connectors/content-owners", () => jsonResponse({ items: [{ account_id: "OWNERaaa" }] })],
]);

describe("AppShell navigation latch during an un-abortable write", () => {
  const routeImportShell = (applyResponder: () => Promise<Response> | Response) => {
    return (input: unknown) => {
      const path = requestPathOf(input);
      if (path === "/channels/import") {
        return Promise.resolve(applyResponder());
      }
      return Promise.resolve((IMPORT_SHELL_ROUTES.get(path) ?? defaultSessionRouteResponse)());
    };
  };

  /** Drive the shell to Preview with the apply POST answered by `applyResponder`. */
  const openImportPreview = async (
    applyResponder: () => Promise<Response> | Response,
  ) => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockImplementation(
      routeImportShell(applyResponder),
    );
    // Returned so a test can tear the DOCUMENT down (the reload case), not
    // merely navigate within it.
    const rendered = renderShell();

    await screen.findByRole("complementary", { name: "Primary navigation" });
    fireEvent.click(navButton("Channel Registry"));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: /import csv/iu }));

    const panel = screen.getByRole("group", { name: "Import upload" });
    const picker = within(panel).getByLabelText("Content owner");
    await waitFor(() =>
      expect(within(picker).getByRole("option", { name: "OWNERaaa" })).toBeInTheDocument(),
    );
    fireEvent.change(picker, { target: { value: "OWNERaaa" } });
    fireEvent.change(within(panel).getByLabelText("Roster CSV"), {
      target: {
        files: [new File(["youtube_channel_id,channel_name\nUCa,Alpha\n"], "r.csv")],
      },
    });
    fireEvent.change(within(panel).getByLabelText("Reason (required, audited)"), {
      target: { value: "monthly roster load" },
    });
    fireEvent.click(within(panel).getByRole("button", { name: /^preview$/iu }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
    );
    return rendered;
  };

  it("NAV LATCH: blocks sidebar navigation while an import apply is in flight", async () => {
    const applyGate = deferredImportResponse();
    let firstCall = true;
    await openImportPreview(() => {
      if (firstCall) {
        firstCall = false;
        return jsonResponse(IMPORT_PLAN);
      }
      return applyGate.pending;
    });

    // Baseline: nav is live while only a preview has run.
    expect(navButton("CMS Groups")).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));
    // This asserts the WIRING — the latch reaches the sidebar — not the
    // timing. fireEvent wraps the click in act(), which flushes effects, so
    // an effect-armed latch would look armed here too. The timing property
    // (armed BEFORE the request is dispatched, so no window exists in which
    // the write is running and nav is live) is proven in
    // RegistryImportFlow.test.tsx, which observes the latch at dispatch.
    expect(navButton("CMS Groups")).toBeDisabled();

    // Every nav item is latched, and each says why.
    expect(navButton("Command Center")).toBeDisabled();
    expect(navButton("Channel Registry")).toBeDisabled();
    expect(navButton("CMS Groups").getAttribute("title")).toMatch(/cannot be aborted/iu);

    // Clicking anyway does nothing: the flow is still mounted on Preview, so
    // the pending write's completion handler is still there to run.
    fireEvent.click(navButton("CMS Groups"));
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();

    // The latch releases with the request, so nav cannot stay stuck — and it
    // releases in the same `finally` batch that advances the step, so the
    // Applied panel and the freed nav land together.
    applyGate.release(jsonResponse({ ...IMPORT_PLAN, dry_run: false }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );
    expect(navButton("CMS Groups")).toBeEnabled();
  });

  it("NAV LATCH: leaves navigation free during the read-only dry run", async () => {
    // Only the apply commits. A preview writes nothing, so latching the shell
    // for it would strand the operator on a slow read for no safety gain.
    await openImportPreview(() => jsonResponse(IMPORT_PLAN));

    expect(navButton("CMS Groups")).toBeEnabled();
    expect(navButton("CMS Groups").getAttribute("title")).toBeNull();
  });

  it("NAV LATCH: releases when a failed apply settles", async () => {
    const applyGate = deferredImportResponse();
    let firstCall = true;
    await openImportPreview(() => {
      if (firstCall) {
        firstCall = false;
        return jsonResponse(IMPORT_PLAN);
      }
      return applyGate.pending;
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));
    await waitFor(() => expect(navButton("CMS Groups")).toBeDisabled());

    // A 5xx does not establish that the write was rejected, so the flow
    // reports the outcome as unknown — but the latch still releases, because
    // it is tied to the request settling, not to the request succeeding.
    applyGate.release(jsonResponse({ detail: "boom" }, 500));
    await waitFor(() =>
      expect(screen.getByText("Apply outcome unknown")).toBeInTheDocument(),
    );
    expect(navButton("CMS Groups")).toBeEnabled();
  });

  it("UNSETTLED IMPORT: the warning survives navigating away and back", async () => {
    // The nav latch releases when the request settles, which is correct — the
    // warning's own advice is to open the Audit trail, so the shell must let
    // the operator go. But that trip UNMOUNTS RegistryView. Holding the
    // unsettled flag there meant returning to Registry produced a fresh view
    // with Import CSV live again while the original request might still be
    // committing — the duplicate CHANNEL_IMPORTED the flag exists to prevent
    // (review #184, codex P1). The flag is owned by the shell for this reason.
    const applyGate = deferredImportResponse();
    let firstCall = true;
    await openImportPreview(() => {
      if (firstCall) {
        firstCall = false;
        return jsonResponse(IMPORT_PLAN);
      }
      return applyGate.pending;
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));
    await waitFor(() => expect(navButton("CMS Groups")).toBeDisabled());
    applyGate.reject(new TypeError("Failed to fetch"));
    await waitFor(() =>
      expect(screen.getByText("Apply outcome unknown")).toBeInTheDocument(),
    );

    // Leaving the flow raises the shell-level warning.
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/iu }));
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent(/may still be committing/iu),
    );
    // Openable on purpose: re-previewing the roster is the reconciliation
    // surface. The duplicate is refused at Apply, not at the opener.
    expect(screen.getByRole("button", { name: /import csv/iu })).toBeEnabled();

    // Follow the notice's advice: nav is free, so the trip is possible at all.
    expect(navButton("Audit Log")).toBeEnabled();
    fireEvent.click(navButton("Audit Log"));
    await waitFor(() =>
      expect(screen.queryByRole("button", { name: /import csv/iu })).not.toBeInTheDocument(),
    );

    // Coming back, the warning is still there and the duplicate is still shut.
    fireEvent.click(navButton("Channel Registry"));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    expect(screen.getByRole("status")).toHaveTextContent(/may still be committing/iu);
    // Openable on purpose: re-previewing the roster is the reconciliation
    // surface. The duplicate is refused at Apply, not at the opener.
    expect(screen.getByRole("button", { name: /import csv/iu })).toBeEnabled();

    // Only the explicit acknowledgement retires it.
    fireEvent.click(
      within(screen.getByRole("status")).getByRole("button", {
        name: /checked the audit trail/iu,
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("status")).not.toBeInTheDocument(),
    );
  });

  it("UNSETTLED IMPORT: the warning survives a browser reload and a second tab", async () => {
    // Holding it in React state alone meant F5 — or opening the app in another
    // tab — initialised the flag back to false and re-enabled Import CSV while
    // the original POST might still be committing (review #184, codex P1). The
    // flag mirrors into localStorage, so a fresh DOCUMENT still sees it.
    const applyGate = deferredImportResponse();
    let firstCall = true;
    const { unmount } = await openImportPreview(() => {
      if (firstCall) {
        firstCall = false;
        return jsonResponse(IMPORT_PLAN);
      }
      return applyGate.pending;
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));
    await waitFor(() => expect(navButton("CMS Groups")).toBeDisabled());
    applyGate.reject(new TypeError("Failed to fetch"));
    await waitFor(() =>
      expect(screen.getByText("Apply outcome unknown")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/iu }));
    await screen.findByRole("status");

    // Tear the whole document down — the reload / second-tab case. Nothing of
    // the previous React tree survives this; only the mirror does.
    unmount();

    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });
    fireEvent.click(navButton("Channel Registry"));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());

    expect(screen.getByRole("status")).toHaveTextContent(/may still be committing/iu);
    // Openable on purpose: re-previewing the roster is the reconciliation
    // surface. The duplicate is refused at Apply, not at the opener.
    expect(screen.getByRole("button", { name: /import csv/iu })).toBeEnabled();
  });

  it("UNSETTLED IMPORT: leaving by the SIDEBAR still raises the warning", async () => {
    // The flow's exit handler is not the only way out. Once the latch releases
    // on an indeterminate apply, the sidebar is live and unmounts the flow
    // without ever calling onDone — so raising the flag on that callback left
    // the operator returning to a clean-looking Registry with Import CSV live
    // (review #184, codex P1). The flag is raised at DISPATCH instead, so the
    // route out no longer matters.
    const applyGate = deferredImportResponse();
    let firstCall = true;
    await openImportPreview(() => {
      if (firstCall) {
        firstCall = false;
        return jsonResponse(IMPORT_PLAN);
      }
      return applyGate.pending;
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));
    await waitFor(() => expect(navButton("CMS Groups")).toBeDisabled());
    applyGate.reject(new TypeError("Failed to fetch"));
    await waitFor(() =>
      expect(screen.getByText("Apply outcome unknown")).toBeInTheDocument(),
    );

    // Out through the sidebar — never touching Cancel.
    await waitFor(() => expect(navButton("CMS Groups")).toBeEnabled());
    fireEvent.click(navButton("CMS Groups"));
    fireEvent.click(navButton("Channel Registry"));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());

    expect(screen.getByRole("status")).toHaveTextContent(/may still be committing/iu);
    // Openable on purpose: re-previewing the roster is the reconciliation
    // surface. The duplicate is refused at Apply, not at the opener.
    expect(screen.getByRole("button", { name: /import csv/iu })).toBeEnabled();
  });

  it("UNSETTLED IMPORT: a tab closed mid-apply still warns the next document", async () => {
    // Nothing has failed yet here — the POST is simply still in flight. If the
    // operator closes the tab now, this document's fetch handler dies while the
    // backend goes on committing, so the uncertainty has to already be durable
    // BEFORE the request is dispatched, not recorded when it fails.
    const applyGate = deferredImportResponse();
    let firstCall = true;
    const { unmount } = await openImportPreview(() => {
      if (firstCall) {
        firstCall = false;
        return jsonResponse(IMPORT_PLAN);
      }
      return applyGate.pending;
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));
    await waitFor(() => expect(navButton("CMS Groups")).toBeDisabled());
    unmount();

    renderShell();
    await screen.findByRole("complementary", { name: "Primary navigation" });
    fireEvent.click(navButton("Channel Registry"));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());

    expect(screen.getByRole("status")).toHaveTextContent(/may still be committing/iu);
    // Openable on purpose: re-previewing the roster is the reconciliation
    // surface. The duplicate is refused at Apply, not at the opener.
    expect(screen.getByRole("button", { name: /import csv/iu })).toBeEnabled();
  });

  it("UNSETTLED IMPORT: an established outcome clears the flag", async () => {
    // The complement, and the reason raising at dispatch is safe: a 2xx and a
    // definite rejection both SETTLE the question, so neither may leave the
    // importer locked. A 422 is an established refusal — nothing committed.
    const applyGate = deferredImportResponse();
    let firstCall = true;
    await openImportPreview(() => {
      if (firstCall) {
        firstCall = false;
        return jsonResponse(IMPORT_PLAN);
      }
      return applyGate.pending;
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));
    applyGate.release(jsonResponse({ detail: "roster rejected" }, 422));
    await waitFor(() => expect(navButton("CMS Groups")).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/iu }));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /import csv/iu })).toBeEnabled();
  });

  it("UNSETTLED IMPORT: a browser that refuses storage REFUSES the apply", async () => {
    // Supersedes an earlier fail-OPEN reading of this case, and codex was right
    // to push back on it. A record that lives only in memory is not a claim:
    // the Web Lock is released the moment admission returns, so no other tab
    // can see it, and a reload erases it while the backend request may still
    // be committing. Admitting on one hands out a claim nobody else can
    // honour — for an audited write. So admission FAILS CLOSED.
    //
    // The cost is real and deliberate: the operator cannot import until
    // storage works. That is why the refusal names the cause and the remedy
    // rather than reading as a generic failure.
    const denied = () => {
      throw new DOMException("denied", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(denied);

    let applyCalls = 0;
    await openImportPreview(() => {
      applyCalls += 1;
      return jsonResponse(IMPORT_PLAN);
    });
    const beforeApply = applyCalls;

    fireEvent.click(screen.getByRole("button", { name: /^apply$/iu }));

    await waitFor(() =>
      expect(screen.getByText(/not storing site data/iu)).toBeInTheDocument(),
    );
    expect(screen.getByText(/allow site data/iu)).toBeInTheDocument();
    // Nothing was dispatched: the write never left, so there is no outcome to
    // be unknown about and the nav latch is free again.
    expect(applyCalls).toBe(beforeApply);
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    await waitFor(() => expect(navButton("CMS Groups")).toBeEnabled());

    vi.restoreAllMocks();
  });
});
