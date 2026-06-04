import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ConnectorsView from "@/components/srcc/views/ConnectorsView";
import type {
  AdsensePaymentListResponse,
  ConnectorCredentialListResponse,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;
const ORIGINAL_PROMPT = globalThis.prompt;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  globalThis.prompt = ORIGINAL_PROMPT;
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

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: unknown): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

function methodOf(init: unknown): string {
  return ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase();
}

function fetchMock() {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

// Route the two auto-fetch GETs (credentials + payments) to fixed bodies and let
// callers override individual routes via the supplied responder.
function routeBoth(
  responder: (url: string, init: unknown) => Response | null,
) {
  return (input: unknown, init?: unknown) => {
    const url = urlOf(input);
    const custom = responder(url, init);
    if (custom) return Promise.resolve(custom);
    if (url.startsWith("/connectors/credentials")) {
      return Promise.resolve(jsonResponse(CREDENTIALS));
    }
    if (url.startsWith("/adsense/payments")) {
      return Promise.resolve(jsonResponse(PAYMENTS));
    }
    return Promise.resolve(jsonResponse({}, 200));
  };
}

function renderConnectorsView(canRunConnectors = true) {
  return render(
    <TenantProvider initialSlug="ums">
      <ConnectorsView canRunConnectors={canRunConnectors} />
    </TenantProvider>,
  );
}

describe("ConnectorsView wired to the connector + AdSense endpoints", () => {
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
    renderConnectorsView();

    await waitFor(() =>
      expect(screen.getByText("AdSense payment March 2026")).toBeInTheDocument(),
    );
    expect(screen.getByText("pub-1")).toBeInTheDocument();
    // payment_amount "930" formatted as USD currency.
    expect(screen.getByText("$930.00")).toBeInTheDocument();
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

  it("always surfaces the run-history honesty note", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView();

    await waitFor(() =>
      expect(
        screen.getByText(/Run history not yet available/i),
      ).toBeInTheDocument(),
    );
  });

  it("requests a connector job (POST /connectors/jobs) and shows the recorded-not-executed result", async () => {
    globalThis.prompt = vi.fn(() => "Manual March resync");
    fetchMock().mockImplementation(
      routeBoth((url, init) => {
        if (url === "/connectors/jobs" && methodOf(init) === "POST") {
          return jsonResponse(
            {
              connector_key: "youtube_reporting",
              account_id: "acct-1",
              execution_status: "recorded_not_executed",
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
    fireEvent.click(screen.getByRole("button", { name: /request sync/i }));

    await waitFor(() =>
      expect(screen.getByText("Sync requested")).toBeInTheDocument(),
    );
    // Honest copy: the job is recorded, not executed.
    expect(
      screen.getByText(/Queued \(recorded, not yet executed\)/i),
    ).toBeInTheDocument();
    expect(
      fetchMock().mock.calls.some(
        ([input, init]) =>
          urlOf(input) === "/connectors/jobs" && methodOf(init) === "POST",
      ),
    ).toBe(true);
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
    // The list refetched and now shows the synced row.
    await waitFor(() =>
      expect(screen.getByText("AdSense payment March 2026")).toBeInTheDocument(),
    );
    expect(
      fetchMock().mock.calls.some(
        ([input, init]) =>
          urlOf(input) === "/adsense/sync-payments" &&
          methodOf(init) === "POST",
      ),
    ).toBe(true);
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

  it("disables the Request sync + Sync payments actions when the viewer cannot run connectors", async () => {
    fetchMock().mockImplementation(routeBoth(() => null));
    renderConnectorsView(false);

    await waitFor(() =>
      expect(screen.getByText("youtube_reporting")).toBeInTheDocument(),
    );
    expect(
      screen.getByRole("button", { name: /request sync/i }),
    ).toBeDisabled();
    expect(
      screen.getByRole("button", { name: /^sync payments$/i }),
    ).toBeDisabled();
  });
});
