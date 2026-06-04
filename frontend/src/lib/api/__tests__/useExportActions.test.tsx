import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ExportJobCreated, ExportRequestBody } from "@/lib/api/types";
import { useExportActions } from "@/lib/api/useExportActions";
import { TenantProvider } from "@/contexts/TenantContext";

const wrapper = ({ children }: { children: React.ReactNode }) => {
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
};

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

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

const lastFetchArgs = () => {
  return fetchMock().mock.calls.at(-1);
};

/** Narrow the last fetch args away from `undefined`, failing the test if none. */
const requireFetchArgs = () => {
  const args = lastFetchArgs();
  if (!args) throw new Error("expected fetch to have been called");
  return args;
};

/** Resolve a promise from outside via a deferred, for ordering concurrent calls. */
const deferred = function <T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

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

    const [url, init] = requireFetchArgs();
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

  it("drops a same-tick duplicate submit: exactly one POST, one created job", async () => {
    const first = deferred<Response>();
    // Only ONE response is queued; a second POST would have no mock and surface
    // the dedupe regression as an undefined-response failure.
    fetchMock().mockReturnValueOnce(first.promise);
    const { result } = renderHook(() => useExportActions(), { wrapper });

    const ONLY = { ...CREATED, id: "only" };
    let firstResolved: ExportJobCreated | null | undefined;
    let secondResolved: ExportJobCreated | null | undefined;

    await act(async () => {
      // Double-click before re-render: both calls run off the same render
      // closure (the state `loading` guard cannot catch the second).
      const p1 = result.current
        .requestExport(REQUEST_BODY)
        .catch(() => undefined);
      const p2 = result.current
        .requestExport(REQUEST_BODY)
        .catch(() => undefined);
      first.resolve(jsonResponse(ONLY, 202));
      [firstResolved, secondResolved] = await Promise.all([p1, p2]);
    });

    // Exactly one POST was dispatched; the duplicate was dropped, not queued.
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    // The surviving call resolves with the created job; the dropped one with null.
    expect(firstResolved).toMatchObject({ id: "only" });
    expect(secondResolved).toBeNull();
    expect(result.current.data?.id).toBe("only");
    expect(result.current.loading).toBe(false);
  });

  it("allows a fresh submit after the in-flight request settles", async () => {
    // A fresh Response per call: a Response body can only be read once.
    fetchMock().mockImplementation(() => jsonResponse(CREATED, 202));
    const { result } = renderHook(() => useExportActions(), { wrapper });

    // First submit completes and clears the in-flight latch.
    await act(async () => {
      await result.current.requestExport(REQUEST_BODY);
    });
    // A later, non-overlapping submit must NOT be dropped.
    await act(async () => {
      await result.current.requestExport(REQUEST_BODY);
    });

    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(result.current.data?.id).toBe(CREATED.id);
  });
});
