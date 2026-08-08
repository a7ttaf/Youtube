import { StrictMode } from "react";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import AuditView from "@/components/srcc/views/AuditView";
import type { AuditEventListResponse, AuditSummaryResponse } from "@/lib/api/types";
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

const PAGED_EVENTS_FIRST: AuditEventListResponse = {
  ...EVENTS,
  pagination: { limit: 50, returned: 1, has_more: true, next_cursor: { created_at: "2026-03-21T02:18:44Z", id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" } },
};

const PAGED_EVENTS_SECOND: AuditEventListResponse = {
  items: [
    {
      id: "cccccccc-cccc-cccc-cccc-cccccccccccc",
      user_id: "security.admin@ums",
      event_type: "CONNECTOR_JOB_RUN",
      entity_type: "connector_job",
      entity_id: "job-1",
      scope_type: "global",
      scope_id: null,
      request_id: "req-3",
      reason: "connector reconciliation run",
      details: { status: "completed" },
      details_redacted: false,
      sensitive: false,
      created_at: "2026-03-21T02:18:00Z",
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

// Real-shaped aggregate response (AuditSummaryResponse, api/audit.py:38-44).
// Values chosen so the formatted (toLocaleString) digit groups are unambiguous.
const SUMMARY: AuditSummaryResponse = {
  total_events: 1840,
  sensitive_events: 230,
  recent_count: 184,
  window_hours: 24,
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const csvResponse = (body: string, extraHeaders: Record<string, string> = {}) => {
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/csv", ...extraHeaders },
  });
};

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

// Pick the /audit/events fixture page matching the query-string variant.
const eventsResponse = (url: string) => {
  if (url.includes("cursor_created_at=")) {
    return jsonResponse(PAGED_EVENTS_SECOND);
  }
  if (url.includes("event_type=CHANNEL_UPDATED")) {
    return jsonResponse({
      ...EVENTS,
      items: [
        {
          ...EVENTS.items[0],
          event_type: "CHANNEL_UPDATED",
          id: "dddddddd-dddd-dddd-dddd-dddddddddddd",
        },
      ],
    });
  }
  return jsonResponse(EVENTS);
};

// Route the auto-fetch GET /audit/events to a fixed body; let callers override.
const routeEvents = (
  responder: (url: string) => Response | null,
) => {
  return (input: unknown) => {
    const url = urlOf(input);
    const custom = responder(url);
    if (custom) return Promise.resolve(custom);
    if (url.startsWith("/audit/summary")) {
      return Promise.resolve(jsonResponse(SUMMARY));
    }
    if (url.startsWith("/audit/events")) {
      return Promise.resolve(eventsResponse(url));
    }
    return Promise.resolve(jsonResponse({}, 200));
  };
};

const renderAuditView = (canViewAudit = true, canViewFinance = true) => {
  return render(
    <TenantProvider initialSlug="ums">
      <AuditView canViewAudit={canViewAudit} canViewFinance={canViewFinance} />
    </TenantProvider>,
  );
};

const auditCalls = () => {
  return fetchMock().mock.calls.filter(([input]) =>
    urlOf(input).startsWith("/audit/events"),
  );
};

const summaryCalls = () => {
  return fetchMock().mock.calls.filter(([input]) =>
    urlOf(input).startsWith("/audit/summary"),
  );
};

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
    // Non-redacted details rendered as safe key=value pairs.
    expect(screen.getByText(/checksum=exp_8b3c41/)).toBeInTheDocument();
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

  it("renders the restricted placeholder and fires NO fetch when canViewAudit is false", () => {
    fetchMock().mockImplementation(routeEvents(() => null));
    renderAuditView(false);

    expect(screen.getByText("Audit view restricted")).toBeInTheDocument();
    expect(screen.getByText(/VIEW_AUDIT_LOG permission/i)).toBeInTheDocument();
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

  it("resets the live timeline when the event type filter changes", async () => {
    const user = userEvent.setup();
    fetchMock().mockImplementation(routeEvents(() => null));
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );

    await user.selectOptions(screen.getByLabelText(/audit event type/i), "CHANNEL_UPDATED");

    await waitFor(() =>
      expect(screen.getByText("CHANNEL_UPDATED")).toBeInTheDocument(),
    );
    expect(screen.queryByText("REVENUE_EXPORTED")).not.toBeInTheDocument();
    const lastAuditCall = auditCalls().at(-1);
    expect(lastAuditCall).toBeDefined();
    expect(urlOf(lastAuditCall?.[0])).toContain("event_type=CHANNEL_UPDATED");
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

  it("fires exactly ONE /audit/events fetch under StrictMode (the read self-audits, so no double-call)", async () => {
    fetchMock().mockImplementation(routeEvents(() => null));
    // Mirror the real app root (main.tsx wraps the tree in <StrictMode>), which
    // double-invokes effects in dev. Because every GET /audit/events also WRITES
    // an AUDIT_LOG_VIEWED row, a double-invoke would record a duplicate audit
    // event. The useAsync in-flight dedupe must keep it to a single request.
    render(
      <StrictMode>
        <TenantProvider initialSlug="ums">
          <AuditView canViewAudit canViewFinance />
        </TenantProvider>
      </StrictMode>,
    );

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    expect(auditCalls()).toHaveLength(1);
  });

  it("downloads the export as a blob (no truncation header => no notice) and never sends cursor params", async () => {
    const createObjectURL = vi.fn(() => "blob:audit-1");
    const revokeObjectURL = vi.fn();
    vi.stubGlobal("URL", { ...URL, createObjectURL, revokeObjectURL });

    fetchMock().mockImplementation((input: unknown) => {
      const url = urlOf(input);
      if (url.startsWith("/audit/events/export")) {
        return Promise.resolve(csvResponse("created_at,event_type\n2026,LOGIN\n"));
      }
      return routeEvents(() => null)(input);
    });
    renderAuditView(true);

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /download audit view/i }));

    await waitFor(() => expect(createObjectURL).toHaveBeenCalledTimes(1));
    expect(revokeObjectURL).toHaveBeenCalledWith("blob:audit-1");
    // No X-Truncated header => no truncation notice.
    expect(screen.queryByText(/truncated at 10,000/i)).not.toBeInTheDocument();
    // The export request carried no cursor params.
    const exportCall = fetchMock().mock.calls.find(([i]) =>
      urlOf(i).startsWith("/audit/events/export"),
    );
    expect(exportCall).toBeDefined();
    expect(urlOf(exportCall?.[0])).not.toContain("cursor_created_at");
    expect(urlOf(exportCall?.[0])).not.toContain("cursor_id");
  });

  it("surfaces a truncation notice only when the export sets X-Truncated: true", async () => {
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:audit-2"),
      revokeObjectURL: vi.fn(),
    });

    fetchMock().mockImplementation((input: unknown) => {
      const url = urlOf(input);
      if (url.startsWith("/audit/events/export")) {
        return Promise.resolve(
          csvResponse("created_at,event_type\n2026,LOGIN\n", { "X-Truncated": "true" }),
        );
      }
      return routeEvents(() => null)(input);
    });
    renderAuditView(true);

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /download audit view/i }));

    await waitFor(() =>
      expect(screen.getByText(/truncated at 10,000 rows/i)).toBeInTheDocument(),
    );
  });

  it("disables the download button when the viewer cannot view the audit log", () => {
    fetchMock().mockImplementation(routeEvents(() => null));
    renderAuditView(false);
    expect(screen.getByRole("button", { name: /download audit view/i })).toBeDisabled();
  });

  it("wires the event-type selector to `event_type` and updates the list query", async () => {
    fetchMock().mockImplementation(
      routeEvents((url) => {
        if (url.startsWith("/audit/events?event_type=CHANNEL_UPDATED")) {
          return jsonResponse({
            ...EVENTS,
            items: [
              {
                ...EVENTS.items[0],
                event_type: "CHANNEL_UPDATED",
                id: "dddddddd-dddd-dddd-dddd-dddddddddddd",
              },
            ],
            pagination: { ...EVENTS.pagination, has_more: false, next_cursor: null },
          });
        }
        return null;
      }),
    );
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    const filter = screen.getByRole("combobox", { name: /audit event type/i });
    await userEvent.selectOptions(filter, "CHANNEL_UPDATED");

    await waitFor(() =>
      expect(screen.getByText("CHANNEL_UPDATED")).toBeInTheDocument(),
    );
    const lastRequest = fetchMock().mock.calls.at(-1);
    if (!lastRequest) {
      throw new Error("expected fetch to have been called");
    }
    expect(urlOf(lastRequest[0])).toContain("event_type=CHANNEL_UPDATED");
  });

  it("loads another page via Load More and appends rows using next_cursor", async () => {
    fetchMock().mockImplementation(
      (input: unknown) => {
        const url = urlOf(input);
        if (url.startsWith("/audit/events")) {
          if (url.includes("cursor_created_at=")) {
            return Promise.resolve(jsonResponse(PAGED_EVENTS_SECOND));
          }
          return Promise.resolve(
            jsonResponse({
              ...PAGED_EVENTS_FIRST,
              items: [
                {
                  ...EVENTS.items[0],
                  id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
                  event_type: "REVENUE_EXPORTED",
                },
              ],
            }),
          );
        }
        return Promise.resolve(jsonResponse({}, 200));
      },
    );
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /load more/i })).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    await waitFor(() =>
      expect(screen.getByText("CONNECTOR_JOB_RUN")).toBeInTheDocument(),
    );
    expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument();
  });

  it("dedupes appended rows by event id when a page repeats an id", async () => {
    fetchMock().mockImplementation((input: unknown) => {
      const url = urlOf(input);
      if (url.startsWith("/audit/events")) {
        if (url.includes("cursor_created_at=")) {
          // Second page repeats the first page's id plus a new row.
          return Promise.resolve(
            jsonResponse({
              ...PAGED_EVENTS_SECOND,
              items: [
                { ...EVENTS.items[0], id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" },
                ...PAGED_EVENTS_SECOND.items,
              ],
            }),
          );
        }
        return Promise.resolve(PAGED_EVENTS_FIRST).then((b) => jsonResponse(b));
      }
      return Promise.resolve(jsonResponse({}, 200));
    });
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /load more/i }));

    await waitFor(() =>
      expect(screen.getByText("CONNECTOR_JOB_RUN")).toBeInTheDocument(),
    );
    // The duplicated first-page id renders exactly once after the append.
    expect(screen.getAllByText("REVENUE_EXPORTED")).toHaveLength(1);
  });

  it("includes event_type in the export request but never cursor params", async () => {
    vi.stubGlobal("URL", {
      ...URL,
      createObjectURL: vi.fn(() => "blob:audit-3"),
      revokeObjectURL: vi.fn(),
    });
    fetchMock().mockImplementation((input: unknown) => {
      const url = urlOf(input);
      if (url.startsWith("/audit/events/export")) {
        return Promise.resolve(csvResponse("created_at\n2026\n"));
      }
      if (url.startsWith("/audit/events?event_type=CHANNEL_UPDATED")) {
        return Promise.resolve(
          jsonResponse({
            ...EVENTS,
            items: [
              { ...EVENTS.items[0], event_type: "CHANNEL_UPDATED", id: "dddddddd-dddd-dddd-dddd-dddddddddddd" },
            ],
          }),
        );
      }
      return routeEvents(() => null)(input);
    });
    renderAuditView();

    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    await userEvent.selectOptions(
      screen.getByRole("combobox", { name: /audit event type/i }),
      "CHANNEL_UPDATED",
    );
    await waitFor(() =>
      expect(screen.getByText("CHANNEL_UPDATED")).toBeInTheDocument(),
    );
    await userEvent.click(screen.getByRole("button", { name: /download audit view/i }));

    const exportCall = fetchMock().mock.calls.find(([i]) =>
      urlOf(i).startsWith("/audit/events/export"),
    );
    expect(urlOf(exportCall?.[0])).toContain("event_type=CHANNEL_UPDATED");
    expect(urlOf(exportCall?.[0])).not.toContain("cursor");
  });
});

describe("AuditView summary tiles wired to GET /audit/summary", () => {
  it("fetches GET /audit/summary and renders the three live counts (no 'live aggregate endpoint coming')", async () => {
    fetchMock().mockImplementation(routeEvents(() => null));
    renderAuditView();

    // Live formatted counts from the stubbed aggregate (toLocaleString grouping).
    // The audit tile uses the runtime locale's number grouping, so derive the
    // expected display string the same way to keep this stable under any
    // locale default (en-US -> "1,840", de-DE -> "1.840", etc.).
    const recentFormatted = SUMMARY.recent_count.toLocaleString();
    const sensitiveFormatted = SUMMARY.sensitive_events.toLocaleString();
    const totalFormatted = SUMMARY.total_events.toLocaleString();
    await waitFor(() =>
      expect(screen.getByText(recentFormatted)).toBeInTheDocument(),
    );
    expect(screen.getByText(sensitiveFormatted)).toBeInTheDocument();
    expect(screen.getByText(totalFormatted)).toBeInTheDocument();
    // Tile labels are the live-wired ones, not the old mock labels.
    expect(screen.getByText("Events (24h)")).toBeInTheDocument();
    expect(screen.getByText("Total events")).toBeInTheDocument();
    // The retention tile stays a static frontend constant (not from the endpoint).
    expect(screen.getByText("7 years")).toBeInTheDocument();
    // Exactly one summary fetch fired.
    expect(summaryCalls()).toHaveLength(1);
    // The now-false disclaimer is gone.
    expect(screen.queryByText(/live aggregate endpoint coming/i)).not.toBeInTheDocument();
    expect(screen.queryByText(/reference figures/i)).not.toBeInTheDocument();
  });

  it("fires NO summary fetch and fails the tiles closed when canViewAudit is false", () => {
    fetchMock().mockImplementation(routeEvents(() => null));
    renderAuditView(false);

    // Fail-closed: the restricted branch mounts no hook, so no /audit/summary fetch.
    expect(summaryCalls()).toHaveLength(0);
    // Numeric tiles show the honest placeholder, never an invented or stale count.
    // Use the same runtime-locale formatting the tiles use so this stays
    // stable under any default Intl locale (matches the positive assertions).
    expect(
      screen.queryByText(SUMMARY.recent_count.toLocaleString()),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByText(SUMMARY.total_events.toLocaleString()),
    ).not.toBeInTheDocument();
    // The static retention tile still renders; the tiles label is present.
    expect(screen.getByText("7 years")).toBeInTheDocument();
    expect(screen.getByText("Total events")).toBeInTheDocument();
  });

  it("keeps the numeric tiles honest (placeholder) when the summary read 403s", async () => {
    fetchMock().mockImplementation(
      routeEvents((url) =>
        url.startsWith("/audit/summary")
          ? jsonResponse({ detail: "Missing permission: audit.view" }, 403)
          : null,
      ),
    );
    renderAuditView();

    // The events feed still loads (its own fetch is fine); the summary 403 must
    // not surface an invented count.
    await waitFor(() =>
      expect(screen.getByText("REVENUE_EXPORTED")).toBeInTheDocument(),
    );
    expect(summaryCalls()).toHaveLength(1);
    expect(screen.queryByText("184")).not.toBeInTheDocument();
    expect(screen.queryByText("1,840")).not.toBeInTheDocument();
    // Retention tile is unaffected by the aggregate failure.
    expect(screen.getByText("7 years")).toBeInTheDocument();
  });
});
