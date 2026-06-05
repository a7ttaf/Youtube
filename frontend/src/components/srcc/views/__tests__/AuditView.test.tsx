import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AuditView from "@/components/srcc/views/AuditView";
import type { AuditEventListResponse } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

// Real-shaped audit-event page (AuditLogEntry.to_api() + cursor pagination).
const EVENTS: AuditEventListResponse = {
  items: [
    {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      user_id: "finance.admin@ums",
      event_type: "REVENUE_EXPORTED",
      entity_type: "export_job",
      entity_id: "exp-1",
      scope_type: "global",
      scope_id: null,
      request_id: "req-1",
      reason: "Monthly close export",
      details: { checksum: "exp_8b3c41" },
      details_redacted: false,
      sensitive: false,
      created_at: "2026-03-21T02:18:44Z",
    },
  ],
  pagination: { limit: 50, returned: 1, has_more: false, next_cursor: null },
  audit_event: {},
};

const REDACTED_EVENTS: AuditEventListResponse = {
  items: [
    {
      id: "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb",
      user_id: "finance.admin@ums",
      event_type: "PAYMENT_VIEWED",
      entity_type: "payment",
      entity_id: "pay-1",
      scope_type: "global",
      scope_id: null,
      request_id: "req-2",
      reason: null,
      // Server-driven redaction: details already {} when details_redacted true.
      details: {},
      details_redacted: true,
      sensitive: true,
      created_at: "2026-03-21T02:03:11Z",
    },
  ],
  pagination: { limit: 50, returned: 1, has_more: false, next_cursor: null },
  audit_event: {},
};

const EMPTY_EVENTS: AuditEventListResponse = {
  items: [],
  pagination: { limit: 50, returned: 0, has_more: false, next_cursor: null },
  audit_event: {},
};

function jsonResponse(body: unknown, status = 200) { // skipcq: JS-0067
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: unknown): string { // skipcq: JS-0067
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

function fetchMock() { // skipcq: JS-0067
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

// Route the auto-fetch GET /audit/events to a fixed body; let callers override.
function routeEvents( // skipcq: JS-0067
  responder: (url: string) => Response | null,
) {
  return (input: unknown) => {
    const url = urlOf(input);
    const custom = responder(url);
    if (custom) return Promise.resolve(custom);
    if (url.startsWith("/audit/events")) {
      return Promise.resolve(jsonResponse(EVENTS));
    }
    return Promise.resolve(jsonResponse({}, 200));
  };
}

function renderAuditView(canViewAudit = true, canViewFinance = true) { // skipcq: JS-0067
  return render(
    <TenantProvider initialSlug="ums">
      <AuditView canViewAudit={canViewAudit} canViewFinance={canViewFinance} />
    </TenantProvider>,
  );
}

function auditCalls() { // skipcq: JS-0067
  return fetchMock().mock.calls.filter(([input]) =>
    urlOf(input).startsWith("/audit/events"),
  );
}

describe("AuditView wired to GET /audit/events", () => {
  it("renders live audit events from the API (title + sub from real fields)", async () => {
    fetchMock().mockImplementation(routeEvents(() => null));
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    // Sub composed from the real non-null fields.
    expect(screen.getByText(/export_job/)).toBeInTheDocument();
    expect(screen.getByText(/exp-1/)).toBeInTheDocument();
    expect(screen.getByText(/finance\.admin@ums/)).toBeInTheDocument();
    expect(screen.getByText(/Monthly close export/)).toBeInTheDocument();
    // Exactly one self-auditing fetch per mount (no loop).
    expect(auditCalls()).toHaveLength(1);
  });

  it("shows a loading state then the loaded events", async () => {
    let resolveFetch: ((r: Response) => void) | undefined;
    fetchMock().mockImplementation(
      () =>
        new Promise<Response>((res) => {
          resolveFetch = res;
        }),
    );
    renderAuditView();

    // Loading row visible before the fetch resolves.
    expect(screen.getByText(/Loading audit events/i)).toBeInTheDocument();

    resolveFetch?.(jsonResponse(EVENTS));
    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
  });

  it("shows an honest empty state when there are no events", async () => {
    fetchMock().mockImplementation(
      routeEvents((url) =>
        url.startsWith("/audit/events") ? jsonResponse(EMPTY_EVENTS) : null,
      ),
    );
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText(/No audit events recorded/i)).toBeInTheDocument(),
    );
  });

  it("renders the restricted placeholder and fires NO fetch when canViewAudit is false", async () => {
    fetchMock().mockImplementation(routeEvents(() => null));
    renderAuditView(false);

    expect(screen.getByText("Audit view restricted")).toBeInTheDocument();
    // Fail-closed: the gated branch calls no hook, so no /audit/events fetch.
    expect(auditCalls()).toHaveLength(0);
  });

  it("shows the redacted/sensitive treatment for a redacted row and never exposes a reveal control or details payload", async () => {
    fetchMock().mockImplementation(
      routeEvents((url) =>
        url.startsWith("/audit/events") ? jsonResponse(REDACTED_EVENTS) : null,
      ),
    );
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("PAYMENT_VIEWED")).toBeInTheDocument(),
    );
    // Server-driven redaction is surfaced honestly.
    expect(screen.getByText(/Sensitive \(redacted\)/i)).toBeInTheDocument();
    // No reveal control exists for a redacted payload.
    expect(
      screen.queryByRole("button", { name: /reveal/i }),
    ).not.toBeInTheDocument();
  });

  it("maps a 403 on the events read to a no-permission message in an alert", async () => {
    fetchMock().mockImplementation(
      routeEvents((url) =>
        url.startsWith("/audit/events")
          ? jsonResponse({ detail: "Missing permission: audit.view_log" }, 403)
          : null,
      ),
    );
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("No permission")).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
    // The 403 detail is audit-appropriate, not the shared net-revenue copy.
    expect(screen.getByText(/cannot view the audit log/i)).toBeInTheDocument();
    expect(screen.queryByText(/net revenue/i)).not.toBeInTheDocument();
  });
});
