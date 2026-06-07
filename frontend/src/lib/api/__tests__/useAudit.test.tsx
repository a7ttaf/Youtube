import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuditEventListResponse } from "@/lib/api/types";
import {
  buildAuditEventsExportUrl,
  useAuditEvents,
} from "@/lib/api/useAudit";
import { TenantProvider } from "@/contexts/TenantContext";

function wrapper({ children }: { children: React.ReactNode }) { // skipcq: JS-0067
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

// Real-shaped audit-event page (AuditLogEntry.to_api() + cursor pagination).
const EVENTS: AuditEventListResponse = {
  items: [
    {
      id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
      user_id: "user-1",
      event_type: "REVENUE_VIEWED",
      entity_type: "channel",
      entity_id: "chan-1",
      scope_type: "global",
      scope_id: null,
      request_id: "req-1",
      reason: null,
      details: { metric: "net_revenue_usd" },
      details_redacted: false,
      sensitive: false,
      created_at: "2026-03-21T02:18:44Z",
    },
  ],
  pagination: {
    limit: 50,
    returned: 1,
    has_more: false,
    next_cursor: null,
  },
  audit_event: {},
};

function jsonResponse(body: unknown, status = 200) { // skipcq: JS-0067
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchMock() { // skipcq: JS-0067
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

function lastFetchArgs() { // skipcq: JS-0067
  return fetchMock().mock.calls.at(-1);
}

/** Narrow the last fetch args away from `undefined`, failing the test if none. */
function requireFetchArgs() { // skipcq: JS-0067
  const args = lastFetchArgs();
  if (!args) throw new Error("expected fetch to have been called");
  return args;
}

function urlOf(input: unknown): string { // skipcq: JS-0067
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

describe("useAuditEvents", () => {
  it("auto-fetches GET /audit/events on mount and returns {data, loading, error, reload}", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EVENTS));
    const { result } = renderHook(() => useAuditEvents(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(urlOf(requireFetchArgs()[0])).toBe("/audit/events");
    expect(result.current.data?.items).toHaveLength(1);
    expect(result.current.data?.items[0]?.event_type).toBe("REVENUE_VIEWED");
    expect(result.current.error).toBeNull();
    expect(typeof result.current.reload).toBe("function");
  });

  it("encodes event_type/entity_type/entity_id/limit query params when provided", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EVENTS));
    const { result } = renderHook(
      () =>
        useAuditEvents({
          event_type: "REVENUE_VIEWED",
          entity_type: "channel",
          entity_id: "chan-1",
          limit: 10,
        }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    const url = urlOf(requireFetchArgs()[0]);
    expect(url).toContain("/audit/events?");
    expect(url).toContain("event_type=REVENUE_VIEWED");
    expect(url).toContain("entity_type=channel");
    expect(url).toContain("entity_id=chan-1");
    expect(url).toContain("limit=10");
  });

  it("sends BOTH cursor params when both are given (cursor pagination)", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EVENTS));
    const { result } = renderHook(
      () =>
        useAuditEvents({
          cursor_created_at: "2026-03-21T02:18:44Z",
          cursor_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa",
        }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    const url = urlOf(requireFetchArgs()[0]);
    expect(url).toContain("cursor_created_at=");
    expect(url).toContain("cursor_id=aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa");
  });

  it("sends NEITHER cursor param when only one is given (both-or-neither)", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EVENTS));
    const { result } = renderHook(
      () => useAuditEvents({ cursor_created_at: "2026-03-21T02:18:44Z" }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    const url = urlOf(requireFetchArgs()[0]);
    expect(url).not.toContain("cursor_created_at");
    expect(url).not.toContain("cursor_id");
  });

  it("sends NEITHER cursor param when only cursor_id is given (both-or-neither, symmetric)", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EVENTS));
    const { result } = renderHook(
      () => useAuditEvents({ cursor_id: "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa" }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    const url = urlOf(requireFetchArgs()[0]);
    expect(url).not.toContain("cursor_created_at");
    expect(url).not.toContain("cursor_id");
  });

  it("builds /audit/events/export URL with event_type/entity_type/entity_id filters", () => {
    expect(
      buildAuditEventsExportUrl("EXPORT_DOWNLOADED", "export", "exp-1"),
    ).toBe("/audit/events/export?event_type=EXPORT_DOWNLOADED&entity_type=export&entity_id=exp-1");
  });

  it("builds /audit/events/export URL without query params when no filters are provided", () => {
    expect(buildAuditEventsExportUrl(undefined, undefined, undefined)).toBe(
      "/audit/events/export",
    );
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EVENTS));
    const { result } = renderHook(() => useAuditEvents(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: audit.view_log" }, 403),
    );
    const { result } = renderHook(() => useAuditEvents(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});
