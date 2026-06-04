import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ExportListResponse } from "@/lib/api/types";
import { useExports } from "@/lib/api/useExports";
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
});

// Real-shaped list response (ExportJobEntry.to_api() items + pagination).
const LIST_BODY: ExportListResponse = {
  items: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      export_type: "FINANCE_EXCEL",
      scope_type: "company",
      scope_id: "company-a",
      scope_channel_ids: ["channel-a"],
      month: "2026-03",
      currency: "USD",
      requested_by: "user-1",
      status: "COMPLETED",
      file_url: "file-store://export/11111111/ums-finance.xlsx",
      artifact_filename: "ums-finance.xlsx",
      artifact_content_type:
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
      artifact_byte_size: 4096,
      artifact_checksum_sha256: "a".repeat(64),
      failure_reason: null,
      month_lock_status: "LOCKED",
      include_confidence_notes: true,
      include_manual_override_notes: true,
      created_at: "2026-03-31T01:42:00+00:00",
      completed_at: "2026-03-31T01:43:00+00:00",
    },
  ],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
};

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchMock() {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

function lastFetchArgs() {
  return fetchMock().mock.calls.at(-1);
}

/** Narrow the last fetch args away from `undefined`, failing the test if none. */
function requireFetchArgs() {
  const args = lastFetchArgs();
  if (!args) throw new Error("expected fetch to have been called");
  return args;
}

describe("useExports", () => {
  it("auto-fetches GET /exports on mount and returns the items", async () => {
    fetchMock().mockResolvedValue(jsonResponse(LIST_BODY));
    const { result } = renderHook(() => useExports(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/exports");
    expect(result.current.data?.items).toHaveLength(1);
    expect(result.current.data?.items[0]?.status).toBe("COMPLETED");
    expect(result.current.error).toBeNull();
  });

  it("encodes limit/offset query params when provided", async () => {
    fetchMock().mockResolvedValue(jsonResponse(LIST_BODY));
    const { result } = renderHook(() => useExports({ limit: 10, offset: 20 }), {
      wrapper,
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/exports?limit=10&offset=20");
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: exports.analytics" }, 403),
    );
    const { result } = renderHook(() => useExports(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("reload() refetches the list", async () => {
    fetchMock().mockResolvedValue(jsonResponse(LIST_BODY));
    const { result } = renderHook(() => useExports(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    const callsBefore = fetchMock().mock.calls.length;
    act(() => {
      result.current.reload();
    });
    await waitFor(() =>
      expect(fetchMock().mock.calls.length).toBeGreaterThan(callsBefore),
    );
  });
});
