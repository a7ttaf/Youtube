import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_MONTH, MONTH_OPTIONS, WRITE_DEFAULT_MONTH } from "@/components/srcc/shared";
import { ConnectorsView } from "@/components/srcc/views/ConnectorsView";
import type {
  AdsensePaymentListResponse,
  ConnectorCredentialHealthResponse,
  ConnectorCredentialListResponse,
  ConnectorRunListResponse,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

// Real-shaped credential list (ConnectorCredentialEntry.to_api() + pagination).
const CREDENTIALS: ConnectorCredentialListResponse = {
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

const EMPTY_CREDENTIALS: ConnectorCredentialListResponse = {
  items: [],
  pagination: { limit: 50, offset: 0, returned: 0, has_more: false },
};

// Default credential-health page served by routeBoth to the unrelated tests.
// Uses a DISTINCT credential identity (analytics · pub-health) and a benign
// health_state so it never collides with the run-history rows
// (youtube_reporting · acct-1 / adsense · pub-9) or the test-connection result
// badges (ok / auth_failed / not_found) those tests assert on. The telemetry-
// breakdown test below overrides the health route with its own richer fixture.
const HEALTH: ConnectorCredentialHealthResponse = {
  credentials: [
    {
      id: "55555555-5555-5555-5555-555555555555",
      connector_key: "analytics",
      account_id: "pub-health",
      status: "ACTIVE",
      has_secret_ref: true,
      last_refresh_attempt_at: "2026-03-01T12:00:00Z",
      token_expiry_at: "2030-01-01T00:00:00Z",
      last_refresh_status: "succeeded",
      last_refresh_error_class: null,
      health_state: "healthy",
    },
  ],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
};

// Rich credential-health page exercising the auth_failed state + every telemetry
// field (expiry, last attempt, failed status, error class) for the breakdown test.
const HEALTH_AUTH_FAILED: ConnectorCredentialHealthResponse = {
  credentials: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      connector_key: "youtube_reporting",
      account_id: "acct-1",
      status: "ACTIVE",
      has_secret_ref: true,
      last_refresh_attempt_at: "2026-03-01T12:00:00Z",
      token_expiry_at: "2030-01-01T00:00:00Z",
      last_refresh_status: "failed",
      last_refresh_error_class: "RefreshError",
      health_state: "auth_failed",
    },
  ],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
};

// Real-shaped AdSense payment list (AdSensePaymentEntry.to_api() + pagination).
const PAYMENTS: AdsensePaymentListResponse = {
  items: [
    {
      id: "22222222-2222-2222-2222-222222222222",
      source_account_id: "pub-1",
      month: "2026-03",
      payment_name: "AdSense payment March 2026",
      payment_date: "2026-03-21",
      payment_amount: "930",
      payment_currency: "USD",
      payment_status: "PAID",
      raw_payload: {},
      source_report_id: null,
      imported_by: null,
    },
  ],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
  audit_event: {},
};

const EMPTY_PAYMENTS: AdsensePaymentListResponse = {
  items: [],
  pagination: { limit: 50, offset: 0, returned: 0, has_more: false },
  audit_event: {},
};

// Real-shaped connector-run page (ConnectorRunEntry.to_api() + cursor pagination).
const RUNS: ConnectorRunListResponse = {
  items: [
    {
      id: "33333333-3333-3333-3333-333333333333",
      connector_key: "youtube_reporting",
      account_id: "acct-1",
      report_month: "2026-03",
      triggered_by_user_id: "ops@ums",
      started_at: "2026-03-21T02:00:00Z",
      finished_at: "2026-03-21T02:05:00Z",
      status: "SUCCEEDED",
      counts: {
        reports_attempted: 3,
        reports_succeeded: 3,
        reports_failed: 0,
        rows_upserted_total: 120,
        rows_upserted_created: 100,
        rows_upserted_updated: 20,
        rows_upserted_unchanged: 0,
      },
      error_summary: null,
    },
  ],
  pagination: { limit: 50, returned: 1, has_more: false, next_cursor: null },
};

const EMPTY_RUNS: ConnectorRunListResponse = {
  items: [],
  pagination: { limit: 50, returned: 0, has_more: false, next_cursor: null },
};

const PAGED_RUNS_FIRST: ConnectorRunListResponse = {
  ...RUNS,
  pagination: {
    limit: 50,
    returned: 1,
    has_more: true,
    next_cursor: { started_at: "2026-03-21T02:00:00Z", id: "33333333-3333-3333-3333-333333333333" },
  },
};

const PAGED_RUNS_SECOND: ConnectorRunListResponse = {
  items: [
    {
      id: "44444444-4444-4444-4444-444444444444",
      connector_key: "adsense",
      account_id: "pub-9",
      report_month: "2026-02",
      triggered_by_user_id: null,
      started_at: "2026-03-21T01:00:00Z",
      finished_at: null,
      status: "FAILED",
      counts: {
        reports_attempted: 2,
        reports_succeeded: 0,
        reports_failed: 2,
        rows_upserted_total: 0,
        rows_upserted_created: 0,
        rows_upserted_updated: 0,
        rows_upserted_unchanged: 0,
      },
      error_summary: "auth expired",
    },
  ],
  pagination: { limit: 50, returned: 1, has_more: false, next_cursor: null },
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

const methodOf = (init: unknown): string => {
  return ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase();
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

const DEFAULT_ROUTE_RESPONSES = [
  { prefix: "/connectors/runs", body: RUNS },
  { prefix: "/connectors/credentials/health", body: HEALTH },
  { prefix: "/connectors/credentials", body: CREDENTIALS },
  { prefix: "/adsense/payments", body: PAYMENTS },
] as const;

// Route the two auto-fetch GETs (credentials + payments) to fixed bodies and let
// callers override individual routes via the supplied responder.
const routeBoth = (
  responder: (url: string, init: unknown) => Response | null,
) => {
  return (input: unknown, init?: unknown) => {
    const url = urlOf(input);
    const custom = responder(url, init);
    if (custom) return Promise.resolve(custom);
    const route = DEFAULT_ROUTE_RESPONSES.find(({ prefix }) => url.startsWith(prefix));
    if (route) return Promise.resolve(jsonResponse(route.body));
    return Promise.resolve(jsonResponse({}, 200));
  };
};

const runCalls = () => {
  return fetchMock().mock.calls.filter(([input]) =>
    urlOf(input).startsWith("/connectors/runs"),
  );
};

const healthCalls = () => {
  return fetchMock().mock.calls.filter(([input]) =>
    urlOf(input).startsWith("/connectors/credentials/health"),
  );
};

const renderConnectorsView = (
  canRunConnectors = true,
  canViewFinance = true,
  canViewConnectorHealth = true,
  canManageConnectors = true,
) => {
  return render(
    <TenantProvider initialSlug="ums">
      <ConnectorsView
        canRunConnectors={canRunConnectors}
        canManageConnectors={canManageConnectors}
        canViewFinance={canViewFinance}
        canViewConnectorHealth={canViewConnectorHealth}
      />
    </TenantProvider>,
  );
};

describe("ConnectorsView wired to the connector + AdSense endpoints", () => {
  it("opens on the last COMPLETE month, with the current month still offered", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView();

    const monthSelect =
      await screen.findByLabelText<HTMLSelectElement>("AdSense month");
    // The screen's month state is a WRITE default (connector report_month +
    // AdSense payment month) and the Google clients pull a WHOLE calendar
    // month, so it must not open on the in-progress month.
    expect(monthSelect.value).toBe(WRITE_DEFAULT_MONTH);
    expect(monthSelect.value).not.toBe(DEFAULT_MONTH);
    // The default changed, not the choice: every rolling option is still there,
    // current month included, and the seeded value is one of them.
    expect(Array.from(monthSelect.options).map((option) => option.value)).toEqual([
      ...MONTH_OPTIONS,
    ]);
  });

  // Regression (PR #211 review, Devin + Qodo): the write-month default must
  // come from the SAME module-load snapshot as MONTH_OPTIONS, never from a
  // second wall-clock read when the view mounts. Load the shared month module
  // before a month boundary, advance the fake clock across TWO month
  // boundaries, then mount ConnectorsView from that already-loaded graph: the
  // selector must still be nonblank and the submitted report_month must equal
  // the visible selection.
  it("keeps the write-month default selectable when the tab outlives the month window", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      // Module-load snapshot: mid-March 2026, comfortably before the April
      // boundary, so MONTH_OPTIONS freezes to the March window.
      vi.setSystemTime(new Date(2026, 2, 15, 12, 0, 0));
      vi.resetModules();
      const { ConnectorsView: StaleTabConnectorsView } = await import(
        "@/components/srcc/views/ConnectorsView"
      );
      const { TenantProvider: StaleTabTenantProvider } = await import(
        "@/contexts/TenantContext"
      );

      // The tab stays open across two month boundaries: March -> April -> May.
      // A live lastCompleteMonthKey() read at mount now yields "2026-04",
      // which the frozen March selector cannot display — the previous code
      // rendered a blank select whose state still submitted that month.
      vi.setSystemTime(new Date(2026, 4, 15, 12, 0, 0));

      fetchMock().mockImplementation(
        routeBoth((url, init) => {
          if (url === "/connectors/jobs" && methodOf(init) === "POST") {
            return jsonResponse(
              {
                connector_key: "youtube_reporting",
                account_id: "acct-1",
                report_month: "2026-02",
                dry_run: false,
                execution_status: "submitted",
                audit_event: {},
              },
              202,
            );
          }
          return null;
        }),
      );
      render(
        <StaleTabTenantProvider initialSlug="ums">
          <StaleTabConnectorsView
            canRunConnectors
            canManageConnectors
            canViewFinance
            canViewConnectorHealth
          />
        </StaleTabTenantProvider>,
      );

      const monthSelect =
        await screen.findByLabelText<HTMLSelectElement>("AdSense month");
      const optionValues = Array.from(monthSelect.options).map(
        (option) => option.value,
      );
      // Options are the frozen March window — the module-load snapshot, not a
      // May read.
      expect(optionValues).toEqual(["2026-03", "2026-02", "2026-01", "2025-12"]);
      // The default is nonblank AND one of the rendered options.
      expect(monthSelect.value).not.toBe("");
      expect(optionValues).toContain(monthSelect.value);
      expect(monthSelect.value).toBe("2026-02");

      // The submitted month is exactly the visible selection.
      await waitFor(() =>
        expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
      );
      fireEvent.change(
        screen.getByLabelText("Sync reason (required, audited)"),
        { target: { value: "Stale-tab regression pull" } },
      );
      fireEvent.click(screen.getByRole("button", { name: /run pull/i }));
      await waitFor(() =>
        expect(screen.getByText(/Submitted to executor/i)).toBeInTheDocument(),
      );
      const jobCall = fetchMock().mock.calls.find(
        ([input, init]) =>
          urlOf(input) === "/connectors/jobs" && methodOf(init) === "POST",
      );
      expect(jobCall).toBeDefined();
      const body = JSON.parse(
        String((jobCall?.[1] as RequestInit | undefined)?.body ?? "{}"),
      );
      expect(body.report_month).toBe(monthSelect.value);
      expect(body.report_month).toBe("2026-02");
    } finally {
      vi.useRealTimers();
      vi.resetModules();
    }
  });

  it("renders the configured data sources (credentials) list", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    expect(screen.getByText("acct-1")).toBeInTheDocument();
    expect(screen.getByText("ACTIVE")).toBeInTheDocument();
    // has_secret_ref true -> "Configured" badge.
    expect(screen.getByText("Configured")).toBeInTheDocument();
  });

  it("renders the synced AdSense payments list with the string amount formatted", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    // canViewFinance true -> the source-of-truth payment amount is visible.
    renderConnectorsView(true, true);

    await waitFor(() =>
      expect(screen.getByText("AdSense payment March 2026")).toBeInTheDocument(),
    );
    expect(screen.getByText("pub-1")).toBeInTheDocument();
    // payment_amount "930" formatted as USD currency.
    expect(screen.getByText("$930.00")).toBeInTheDocument();
    expect(screen.getByText("PAID")).toBeInTheDocument();
  });

  it("withholds the AdSense payment amount behind the Restricted sentinel when the viewer cannot view finance", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    // canViewFinance false -> the amount cell shows the Restricted sentinel, not
    // the source-of-truth payment value, so a non-finance preview role cannot
    // read the money through the connectors screen.
    renderConnectorsView(true, false);

    await waitFor(() =>
      expect(screen.getByText("AdSense payment March 2026")).toBeInTheDocument(),
    );
    expect(screen.getByText("pub-1")).toBeInTheDocument();
    // The real amount is gone; the shared finance gate renders "Restricted".
    expect(screen.queryByText("$930.00")).not.toBeInTheDocument();
    expect(screen.getByText("Restricted")).toBeInTheDocument();
    expect(screen.getByText("PAID")).toBeInTheDocument();
  });

  it("shows empty states when no data sources or payments exist", async () => {
    fetchMock().mockImplementation(
      routeBoth((url) => {
        if (url.startsWith("/connectors/credentials")) {
          return jsonResponse(EMPTY_CREDENTIALS);
        }
        if (url.startsWith("/adsense/payments")) {
          return jsonResponse(EMPTY_PAYMENTS);
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(
        screen.getByText(/No connector data sources configured/i),
      ).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/No AdSense payments synced for this month/i),
    ).toBeInTheDocument();
  });

  it("renders the live run-history feed from GET /connectors/runs", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView();

    await waitFor(() =>
      expect(
        screen.getByText("youtube_reporting · acct-1"),
      ).toBeInTheDocument(),
    );
    // Status badge + month + counts breakdown all render from real fields.
    expect(screen.getByText("SUCCEEDED")).toBeInTheDocument();
    expect(screen.getByText(/month=2026-03/)).toBeInTheDocument();
    expect(screen.getByText(/reports 3\/3 ok/)).toBeInTheDocument();
  });

  it("shows an honest empty state when there are no connector runs", async () => {
    fetchMock().mockImplementation(
      routeBoth((url) =>
        url.startsWith("/connectors/runs") ? jsonResponse(EMPTY_RUNS) : null,
      ),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(
        screen.getByText(/No connector runs recorded/i),
      ).toBeInTheDocument(),
    );
  });

  it("maps a 403 on the runs read to a no-permission message in an alert", async () => {
    fetchMock().mockImplementation(
      routeBoth((url) =>
        url.startsWith("/connectors/runs")
          ? jsonResponse({ detail: "Missing permission: connectors.view_health" }, 403)
          : null,
      ),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("No permission")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/cannot view connector run history/i),
    ).toBeInTheDocument();
  });

  it("fail-closed: renders the restricted placeholder and fires NO /connectors/runs fetch when lacking the connector-health capability", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView(true, true, false);

    await waitFor(() =>
      expect(screen.getByText("Run history restricted")).toBeInTheDocument(),
    );
    expect(screen.getByText(/VIEW_CONNECTOR_HEALTH permission/i)).toBeInTheDocument();
    // The gated branch mounts no hook, so no /connectors/runs request is issued.
    expect(runCalls()).toHaveLength(0);
  });

  it("loads another run page via Load More, appends, and dedupes a repeated id", async () => {
    fetchMock().mockImplementation((input: unknown, init?: unknown) => {
      const url = urlOf(input);
      if (url.startsWith("/connectors/runs")) {
        if (url.includes("cursor_started_at=")) {
          // Second page repeats the first page's id plus a new row.
          return Promise.resolve(
            jsonResponse({
              ...PAGED_RUNS_SECOND,
              items: [RUNS.items[0], ...PAGED_RUNS_SECOND.items],
            }),
          );
        }
        return Promise.resolve(jsonResponse(PAGED_RUNS_FIRST));
      }
      return routeBoth(() => null)(input, init);
    });
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting · acct-1")).toBeInTheDocument(),
    );
    // First page only carries the limit (no cursor); cursor appears on page 2.
    const firstRunCall = runCalls()[0];
    expect(urlOf(firstRunCall?.[0])).not.toContain("cursor_started_at");

    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    await waitFor(() =>
      expect(screen.getByText("adsense · pub-9")).toBeInTheDocument(),
    );
    // The repeated first-page row renders exactly once after the append.
    expect(screen.getAllByText("youtube_reporting · acct-1")).toHaveLength(1);
    // The second request carried BOTH cursor halves (both-or-neither).
    const pagedCall = runCalls().find(([i]) =>
      urlOf(i).includes("cursor_started_at="),
    );
    expect(urlOf(pagedCall?.[0])).toContain("cursor_id=");
  });

  it("tests a credential connection (ok) and shows a green status badge + detail", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (
          url === "/connectors/credentials/youtube_reporting/acct-1/test" &&
          methodOf(init) === "POST"
        ) {
          return jsonResponse({
            connector_key: "youtube_reporting",
            account_id: "acct-1",
            status: "ok",
            detail: "Credential is active and refreshed.",
            audit_event: {},
          });
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /^test$/i }));

    await waitFor(() =>
      expect(screen.getByText("ok")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Credential is active and refreshed."),
    ).toBeInTheDocument();
    // The POST carried the FIXED audited reason.
    const testCall = fetchMock().mock.calls.find(
      ([i, init]) =>
        urlOf(i) === "/connectors/credentials/youtube_reporting/acct-1/test" &&
        methodOf(init) === "POST",
    );
    const body = JSON.parse(
      String((testCall?.[1] as RequestInit | undefined)?.body ?? "{}"),
    );
    expect(body.reason).toBe("operator connection health check");
  });

  it("disables the Test button when the viewer cannot manage connectors", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView(true, true, true, false);

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /^test$/i })).toBeDisabled();
  });

  it("shows a red badge when the test reports auth_failed", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url.endsWith("/test") && methodOf(init) === "POST") {
          return jsonResponse({
            connector_key: "youtube_reporting",
            account_id: "acct-1",
            status: "auth_failed",
            detail: "OAuth token rejected.",
            audit_event: {},
          });
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /^test$/i }));

    await waitFor(() =>
      expect(screen.getByText("auth_failed")).toBeInTheDocument(),
    );
    expect(screen.getByText("OAuth token rejected.")).toBeInTheDocument();
  });

  it("maps a 404 test response to a not_found result instead of an unhandled error", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url.endsWith("/test") && methodOf(init) === "POST") {
          return jsonResponse({ detail: "Credential not found." }, 404);
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /^test$/i }));

    await waitFor(() =>
      expect(screen.getByText("not_found")).toBeInTheDocument(),
    );
    expect(screen.getByText("Credential not found.")).toBeInTheDocument();
  });

  it("latches the Test button disabled while a probe is in flight", async () => {
    let resolveTest: ((r: Response) => void) | undefined;
    fetchMock().mockImplementation((input: unknown, init?: unknown) => {
      const url = urlOf(input);
      if (url.endsWith("/test") && methodOf(init) === "POST") {
        return new Promise<Response>((res) => {
          resolveTest = res;
        });
      }
      return routeBoth(() => null)(input, init);
    });
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    const testButton = screen.getByRole("button", { name: /^test$/i });
    await userEvent.click(testButton);

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /testing…/iu })).toBeDisabled(),
    );

    resolveTest?.(
      jsonResponse({
        connector_key: "youtube_reporting",
        account_id: "acct-1",
        status: "ok",
        detail: "Active.",
        audit_event: {},
      }),
    );
    await waitFor(() => expect(screen.getByText("ok")).toBeInTheDocument());
  });

  // CONTRACT FLIP (Task 9): the per-row job control is now "Run pull" on the
  // executing path; a 202 with execution_status "submitted" surfaces the
  // Submitted banner. The POST carries the trimmed reason typed inline.
  it("requests a connector job (POST /connectors/jobs) with the inline reason and shows the submitted result", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            {
              connector_key: "youtube_reporting",
              account_id: "acct-1",
              report_month: "2026-03",
              dry_run: false,
              execution_status: "submitted",
              audit_event: {},
            },
            202,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );

    const requestButton = screen.getByRole("button", { name: /run pull/i });
    // The button is disabled until a sync reason is typed (no window.prompt).
    expect(requestButton).toBeDisabled();

    fireEvent.change(
      screen.getByLabelText("Sync reason (required, audited)"),
      { target: { value: "Manual March resync" } },
    );
    expect(requestButton).toBeEnabled();
    fireEvent.click(requestButton);

    await waitFor(() =>
      expect(screen.getByText("Sync requested")).toBeInTheDocument(),
    );
    // Honest copy: the job was submitted to the executor. (Match the banner
    // message specifically; the status badge also renders "submitted", so a
    // bare /Submitted/i would match two elements.)
    expect(screen.getByText(/Submitted to executor/i)).toBeInTheDocument();
    const jobCall = fetchMock().mock.calls.find(
      ([input, init]) =>
        urlOf(input) === "/connectors/jobs" && methodOf(init) === "POST",
    );
    expect(jobCall).toBeDefined();
    // The POST carried the trimmed reason typed into the inline field.
    const jobBody = JSON.parse(
      String((jobCall?.[1] as RequestInit | undefined)?.body ?? "{}"),
    );
    expect(jobBody.reason).toBe("Manual March resync");
  });

  it("Run pull POSTs report_month + dry_run and shows the submitted banner", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            {
              connector_key: "youtube_reporting",
              account_id: "acct-1",
              report_month: "2026-03",
              dry_run: false,
              execution_status: "submitted",
              audit_event: {},
            },
            202,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    fireEvent.change(
      screen.getByLabelText("Sync reason (required, audited)"),
      { target: { value: "Manual March pull" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /run pull/i }));

    // Match the banner message specifically; the status badge also renders
    // "submitted", so a bare /Submitted/i would match two elements.
    await waitFor(() =>
      expect(screen.getByText(/Submitted to executor/i)).toBeInTheDocument(),
    );
    const jobCall = fetchMock().mock.calls.find(
      ([input, init]) =>
        urlOf(input) === "/connectors/jobs" && methodOf(init) === "POST",
    );
    const body = JSON.parse(
      String((jobCall?.[1] as RequestInit | undefined)?.body ?? "{}"),
    );
    // This month is a WRITE default: it becomes the run's report_month and the
    // Google clients pull the whole calendar month, so the seeded value must be
    // the last COMPLETE month — never the in-progress DEFAULT_MONTH the read
    // views open on, and never a frozen literal that ages out.
    expect(body.report_month).toBe(WRITE_DEFAULT_MONTH);
    expect(body.report_month).not.toBe(DEFAULT_MONTH);
    expect(body.dry_run).toBe(false);
    expect(body.reason).toBe("Manual March pull");
  });

  it("refetches the runs list after a 202 submitted", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            {
              connector_key: "youtube_reporting",
              account_id: "acct-1",
              report_month: "2026-03",
              dry_run: false,
              execution_status: "submitted",
              audit_event: {},
            },
            202,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    const before = runCalls().length;
    fireEvent.change(
      screen.getByLabelText("Sync reason (required, audited)"),
      { target: { value: "Pull then refresh" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /run pull/i }));
    await waitFor(() => expect(runCalls().length).toBeGreaterThan(before));
  });

  it("does NOT refetch the run-history feed for a dry-run (no row is ever created)", async () => {
    // FIX: a dry-run returns 202 'submitted' but the backend intentionally
    // creates no connector_runs row, so polling the run-history feed would
    // wait forever for a row that will never appear. The view gates the
    // refetch on !dryRun and skips the poll entirely for dry-runs.
    const setTimeoutSpy = vi.spyOn(window, "setTimeout");
    try {
      fetchMock().mockImplementation(
        routeBoth((url, init) => {
          if (url === "/connectors/jobs" && methodOf(init) === "POST") {
            return jsonResponse(
              {
                connector_key: "youtube_reporting",
                account_id: "acct-1",
                report_month: "2026-03",
                dry_run: true,
                execution_status: "submitted",
                audit_event: {},
              },
              202,
            );
          }
          return null;
        }),
      );
      renderConnectorsView();
      // Toggle the dry-run switch in the UI before submitting.
      await waitFor(() =>
        expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
      );
      const beforeCalls = setTimeoutSpy.mock.calls.length;
      fireEvent.change(
        screen.getByLabelText("Sync reason (required, audited)"),
        { target: { value: "Dry-run only" } },
      );
      // Click the dry-run checkbox.
      const dryRunCheckbox = screen.getByLabelText(/dry run/i) as HTMLInputElement;
      fireEvent.click(dryRunCheckbox);
      fireEvent.click(screen.getByRole("button", { name: /run pull/i }));
      // Give the request a moment to resolve.
      await new Promise((resolve) => setTimeout(resolve, 0));
      // No new delayed refetch timers were scheduled.
      const newCalls = setTimeoutSpy.mock.calls
        .slice(beforeCalls)
        .map(([, delay]) => delay)
        .filter(
          (d): d is number => typeof d === "number" && d >= 1000,
        );
      expect(newCalls).toEqual([]);
    } finally {
      setTimeoutSpy.mockRestore();
    }
  });

  it("schedules delayed refetch timers after a 202 submitted so the worker row catches up", async () => {
    // FIX: the worker does not create the connector_runs row until after
    // credential resolution + OAuth refresh inside _run_live, so a single
    // immediate refetch can miss the RUNNING row. The view schedules three
    // delayed setReloadToken calls (1s/3s/5s) so the run-history feed
    // re-queries once the worker has committed start_run. We spy on
    // window.setTimeout here (the polling uses setTimeout) and assert the
    // expected 1s/3s/5s delays are scheduled.
    const setTimeoutSpy = vi.spyOn(window, "setTimeout");
    try {
      fetchMock().mockImplementation(
        routeBoth((url, init) => {
          if (url === "/connectors/jobs" && methodOf(init) === "POST") {
            return jsonResponse(
              {
                connector_key: "youtube_reporting",
                account_id: "acct-1",
                report_month: "2026-03",
                dry_run: false,
                execution_status: "submitted",
                audit_event: {},
              },
              202,
            );
          }
          return null;
        }),
      );
      renderConnectorsView();

      await waitFor(() =>
        expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
      );
      const beforeCalls = setTimeoutSpy.mock.calls.length;
      fireEvent.change(
        screen.getByLabelText("Sync reason (required, audited)"),
        { target: { value: "Poll for new row" } },
      );
      fireEvent.click(screen.getByRole("button", { name: /run pull/i }));
      await waitFor(() =>
        expect(setTimeoutSpy.mock.calls.length).toBeGreaterThan(beforeCalls),
      );
      const pollDelays = setTimeoutSpy.mock.calls
        .slice(beforeCalls)
        .map(([, delay]) => delay)
        .filter(
          (d): d is number => typeof d === "number",
        );
      // Exactly the three delayed refetch ticks; the 0s tick is the
      // immediate setReloadToken inside runsReloadPoll itself, not a
      // setTimeout.
      expect(pollDelays).toEqual(expect.arrayContaining([1000, 3000, 5000]));
    } finally {
      setTimeoutSpy.mockRestore();
    }
  });

  it("surfaces a 409 detail verbatim on Run pull", async () => {
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            { detail: "A connector job for this scope is already in flight" },
            409,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    fireEvent.change(
      screen.getByLabelText("Sync reason (required, audited)"),
      { target: { value: "Duplicate" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /run pull/i }));
    await waitFor(() =>
      expect(
        screen.getByText(/already in flight/i),
      ).toBeInTheDocument(),
    );
  });

  it("disables Run pull + shows the role hint when the viewer cannot run connectors", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView(false);

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /run pull/i }),
    ).toBeDisabled();
    expect(
      screen.getAllByText("Requires a connector-operations role.").length,
    ).toBeGreaterThan(0);
  });

  it("syncs AdSense payments (POST /adsense/sync-payments) and refetches the list", async () => {
    let paymentListCalls = 0;
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/adsense/sync-payments" && methodOf(init) === "POST") {
          return jsonResponse({
            synced_count: 1,
            items: PAYMENTS.items,
            audit_event: {},
          });
        }
        if (url.startsWith("/adsense/payments")) {
          paymentListCalls += 1;
          return jsonResponse(paymentListCalls <= 1 ? EMPTY_PAYMENTS : PAYMENTS);
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(
        screen.getByText(/No AdSense payments synced for this month/i),
      ).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("Account id"), {
      target: { value: "pub-1" },
    });
    fireEvent.change(screen.getByLabelText("Payment name"), {
      target: { value: "AdSense payment March 2026" },
    });
    fireEvent.change(screen.getByLabelText("Payment date"), {
      target: { value: "2026-03-21" },
    });
    // The form states which month the row will file under, derived from the
    // entered payment date — the operator sees the same month the POST carries.
    expect(screen.getByText(/files under mar 2026/i)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Amount"), {
      target: { value: "930" },
    });
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "Manual March payment" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^sync payments$/i }));

    await waitFor(() =>
      expect(screen.getByText("Payments synced")).toBeInTheDocument(),
    );
    // The filed month (2026-03) is OLDER than the rolling window the selector
    // offers, so the filter stays put — switching would render a value with no
    // matching option — and the success banner names where the rows landed.
    const monthSelectAfter =
      screen.getByLabelText<HTMLSelectElement>("AdSense month");
    expect(monthSelectAfter.value).toBe(WRITE_DEFAULT_MONTH);
    expect(
      screen.getByText(/upserted into the finance source under mar 2026/i),
    ).toBeInTheDocument();
    // The list refetched and now shows the synced row.
    await waitFor(() =>
      expect(screen.getByText("AdSense payment March 2026")).toBeInTheDocument(),
    );
    const syncCall = fetchMock().mock.calls.find(
      ([input, init]) =>
        urlOf(input) === "/adsense/sync-payments" && methodOf(init) === "POST",
    );
    expect(syncCall).toBeDefined();
    // The row files under the month of its payment date (2026-03-21 -> 2026-03),
    // matching the automated AdSense mapping — not the screen's write default.
    const syncBody = JSON.parse(
      String((syncCall?.[1] as RequestInit | undefined)?.body ?? "{}"),
    );
    expect(syncBody.payments[0].month).toBe("2026-03");
  });

  // Regression (PR #211 review, codex P1): a manual payment row must file
  // under the month of its PAYMENT DATE, never under the screen's
  // last-complete-month write default — on Aug 21 the old code filed the
  // payment under July because the form inherited the connector-report default.
  it("files a manual payment under its payment date's month, not the write default", async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    try {
      // Module load in mid-August 2026: the write default is July 2026.
      vi.setSystemTime(new Date(2026, 7, 15, 12, 0, 0));
      vi.resetModules();
      const { ConnectorsView: DatedConnectorsView } = await import(
        "@/components/srcc/views/ConnectorsView"
      );
      const { TenantProvider: DatedTenantProvider } = await import(
        "@/contexts/TenantContext"
      );

      fetchMock().mockImplementation(
        routeBoth((url, init) => {
          if (url === "/adsense/sync-payments" && methodOf(init) === "POST") {
            return jsonResponse({
              synced_count: 1,
              items: [],
              audit_event: {},
            });
          }
          return null;
        }),
      );
      render(
        <DatedTenantProvider initialSlug="ums">
          <DatedConnectorsView
            canRunConnectors
            canManageConnectors
            canViewFinance
            canViewConnectorHealth
          />
        </DatedTenantProvider>,
      );

      // The screen selector still opens on the last COMPLETE month (July) —
      // that default is right for whole-month connector pulls.
      const monthSelect =
        await screen.findByLabelText<HTMLSelectElement>("AdSense month");
      expect(monthSelect.value).toBe("2026-07");

      fireEvent.change(screen.getByLabelText("Account id"), {
        target: { value: "pub-1" },
      });
      fireEvent.change(screen.getByLabelText("Payment name"), {
        target: { value: "AdSense payment August 2026" },
      });
      // ... but the payment is DATED inside the CURRENT month (August 21).
      fireEvent.change(screen.getByLabelText("Payment date"), {
        target: { value: "2026-08-21" },
      });
      expect(screen.getByText(/files under aug 2026/i)).toBeInTheDocument();
      fireEvent.change(screen.getByLabelText("Amount"), {
        target: { value: "930" },
      });
      fireEvent.change(screen.getByLabelText("Reason"), {
        target: { value: "Manual August payment" },
      });
      fireEvent.click(screen.getByRole("button", { name: /^sync payments$/i }));

      await waitFor(() =>
        expect(screen.getByText("Payments synced")).toBeInTheDocument(),
      );
      // Filed under the payment date's month (August), not the July default,
      // and the selector FOLLOWS the filed month so the synced row is visible.
      const syncCall = fetchMock().mock.calls.find(
        ([input, init]) =>
          urlOf(input) === "/adsense/sync-payments" && methodOf(init) === "POST",
      );
      expect(syncCall).toBeDefined();
      const body = JSON.parse(
        String((syncCall?.[1] as RequestInit | undefined)?.body ?? "{}"),
      );
      expect(body.payments[0].month).toBe("2026-08");
      // The filed month IS one of the offered options here, so the selector
      // switches to it and the refreshed list shows the new row.
      const monthSelectAfter =
        screen.getByLabelText<HTMLSelectElement>("AdSense month");
      await waitFor(() => expect(monthSelectAfter.value).toBe("2026-08"));
      expect(
        screen.getByText(/upserted into the finance source under aug 2026/i),
      ).toBeInTheDocument();
    } finally {
      vi.useRealTimers();
      vi.resetModules();
    }
  });

  it("maps a 403 on the AdSense payments read to a no-permission message", async () => {
    fetchMock().mockImplementation(
      routeBoth((url) => {
        if (url.startsWith("/adsense/payments")) {
          return jsonResponse(
            { detail: "Missing permission: revenue.view_finalized_payments" },
            403,
          );
        }
        return null;
      }),
    );
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("No permission")).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("disables the Run pull + Sync payments actions and shows the role hint when the viewer cannot run connectors", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView(false);

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /run pull/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /^sync payments$/i }),
    ).toBeDisabled();
    // The inline sync-reason field is disabled too.
    expect(
      screen.getByLabelText("Sync reason (required, audited)"),
    ).toBeDisabled();
    // The honest no-permission hint is visible (mirrors the credentials UX).
    expect(
      screen.getAllByText("Requires a connector-operations role.").length,
    ).toBeGreaterThan(0);
  });
});

describe("ConnectorsView token-health panel", () => {
  it("renders credential telemetry (state + expiry + last attempt/status + error class) when the viewer can view connector health", async () => {
    fetchMock().mockImplementation(
      routeBoth((url) =>
        url.startsWith("/connectors/credentials/health")
          ? jsonResponse(HEALTH_AUTH_FAILED)
          : null,
      ),
    );
    renderConnectorsView();

    const panel = await screen.findByRole("region", { name: /token health/i });
    const utils = within(panel);
    // The health row identifies the credential and its server-derived state.
    expect(
      await utils.findByText("youtube_reporting · acct-1"),
    ).toBeInTheDocument();
    expect(utils.getByText("auth_failed")).toBeInTheDocument();
    // The four telemetry fields surface: expiry, last attempt, status, error class.
    expect(utils.getByText(/expires/i)).toBeInTheDocument();
    expect(utils.getByText(/last attempt/i)).toBeInTheDocument();
    expect(utils.getByText(/refresh failed/i)).toBeInTheDocument();
    expect(utils.getByText(/RefreshError/)).toBeInTheDocument();
    // The auto-fetch hit the credential-health endpoint exactly once.
    expect(healthCalls()).toHaveLength(1);
  });

  it("fail-closed: renders NOTHING and fires NO /connectors/credentials/health fetch when the viewer lacks the connector-health capability", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView(true, true, false);

    // The rest of the screen still mounts (credentials list is independent).
    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    // No token-health panel is rendered at all (fail-closed: render nothing).
    expect(
      screen.queryByRole("region", { name: /token health/i }),
    ).not.toBeInTheDocument();
    // The gated branch mounts no hook, so no credential-health request is issued.
    expect(healthCalls()).toHaveLength(0);
  });
});
