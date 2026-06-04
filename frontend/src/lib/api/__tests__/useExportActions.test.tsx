import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ExportJobCreated, ExportRequestBody } from "@/lib/api/types";
import { useExportActions } from "@/lib/api/useExportActions";
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

// Real-shaped POST response: the created QUEUED job + audit_event (202).
const CREATED: ExportJobCreated = {
  id: "22222222-2222-2222-2222-222222222222",
  export_type: "FINANCE_EXCEL",
  scope_type: "company",
  scope_id: "company-a",
  scope_channel_ids: ["channel-a"],
  month: "2026-03",
  currency: "USD",
  requested_by: "user-1",
  status: "QUEUED",
  file_url: null,
  artifact_filename: null,
  artifact_content_type: null,
  artifact_byte_size: null,
  artifact_checksum_sha256: null,
  failure_reason: null,
  month_lock_status: "LOCKED",
  include_confidence_notes: true,
  include_manual_override_notes: true,
  created_at: "2026-03-31T01:42:00+00:00",
  completed_at: null,
  audit_event: { event_type: "EXPORT_CREATED" },
};

const REQUEST_BODY: ExportRequestBody = {
  export_type: "FINANCE_EXCEL",
  scope_type: "company",
  scope_id: "company-a",
  month: "2026-03",
  currency: "USD",
  reason: "Monthly finance close workbook",
  include_confidence_notes: true,
  include_manual_override_notes: true,
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

describe("useExportActions", () => {
  it("starts idle (no data, no loading, no error) before requestExport()", () => {
    fetchMock().mockResolvedValue(jsonResponse(CREATED, 202));
    const { result } = renderHook(() => useExportActions(), { wrapper });
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("POSTs the request body to /exports and returns the created job", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CREATED, 202));
    const { result } = renderHook(() => useExportActions(), { wrapper });

    await act(async () => {
      await result.current.requestExport(REQUEST_BODY);
    });

    const [url, init] = lastFetchArgs()!;
    expect(url).toBe("/exports");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toMatchObject({
      export_type: "FINANCE_EXCEL",
      scope_type: "company",
      scope_id: "company-a",
      reason: "Monthly finance close workbook",
    });
    expect(result.current.data?.status).toBe("QUEUED");
    expect(result.current.data?.id).toBe(CREATED.id);
    expect(result.current.error).toBeNull();
  });

  it("captures a typed ApiError (422 unknown export_type) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Unknown export_type: WAT" }, 422),
    );
    const { result } = renderHook(() => useExportActions(), { wrapper });

    await act(async () => {
      await expect(
        result.current.requestExport(REQUEST_BODY),
      ).rejects.toMatchObject({ name: "ApiError", status: 422 });
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 422 });
  });
});
