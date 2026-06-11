import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { AuditSummaryResponse } from "@/lib/api/types";
import { useAuditSummary } from "@/lib/api/useAuditSummary";
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

// Real-shaped aggregate response (AuditSummaryResponse, api/audit.py:38-44).
const SUMMARY: AuditSummaryResponse = {
  total_events: 1840,
  sensitive_events: 230,
  recent_count: 184,
  window_hours: 24,
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

describe("useAuditSummary", () => {
  it("auto-fetches GET /audit/summary on mount and returns {data, loading, error, reload}", async () => {
    fetchMock().mockResolvedValue(jsonResponse(SUMMARY));
    const { result } = renderHook(() => useAuditSummary(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    // No query params — window_hours defaults to 24 on the backend.
    expect(urlOf(requireFetchArgs()[0])).toBe("/audit/summary");
    expect(result.current.data).toEqual(SUMMARY);
    expect(result.current.error).toBeNull();
    expect(typeof result.current.reload).toBe("function");
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse(SUMMARY));
    const { result } = renderHook(() => useAuditSummary(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: audit.view" }, 403),
    );
    const { result } = renderHook(() => useAuditSummary(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});

describe("useAuditSummary bounded request", () => {
  it("forwards an AbortSignal to fetch so a stalled request can be cancelled", async () => {
    fetchMock().mockResolvedValue(jsonResponse(SUMMARY));
    renderHook(() => useAuditSummary(), { wrapper });

    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(1));
    const init = requireFetchArgs()[1] as RequestInit | undefined;
    expect(init).toBeDefined();
    expect(init?.signal).toBeInstanceOf(AbortSignal);
  });

  it("aborts the in-flight request when the hook unmounts", async () => {
    let capturedSignal: AbortSignal | null = null;
    fetchMock().mockImplementation((_input, init) => {
      capturedSignal = (init as RequestInit | undefined)?.signal ?? null;
      return new Promise<Response>((_resolve, reject) => {
        // Never resolve; the test verifies the unmount aborts, not a server reply.
        capturedSignal?.addEventListener("abort", () => {
          const err = new Error("aborted");
          err.name = "AbortError";
          reject(err);
        });
      });
    });
    const { unmount } = renderHook(() => useAuditSummary(), { wrapper });

    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(1));
    expect(capturedSignal).not.toBeNull();
    expect((capturedSignal as AbortSignal | null)?.aborted).toBe(false);

    unmount();
    expect((capturedSignal as AbortSignal | null)?.aborted).toBe(true);
  });

  it("aborts a hanging request after the timeout so the Audit screen cannot stay loading forever", async () => {
    vi.useFakeTimers();
    try {
      let capturedSignal: AbortSignal | null = null;
      fetchMock().mockImplementation((_input, init) => {
        capturedSignal = (init as RequestInit | undefined)?.signal ?? null;
        return new Promise<Response>((_resolve, reject) => {
          capturedSignal?.addEventListener("abort", () => {
            const err = new Error("aborted");
            err.name = "AbortError";
            reject(err);
          });
        });
      });

      const { result } = renderHook(() => useAuditSummary(), { wrapper });
      // Let the effect's first fetch start.
      await act(async () => {
        await Promise.resolve();
      });
      expect(fetchMock()).toHaveBeenCalledTimes(1);
      expect(capturedSignal).not.toBeNull();
      expect((capturedSignal as AbortSignal | null)?.aborted).toBe(false);

      // Advance fake timers past REQUEST_TIMEOUT_MS (30s) and let the abort
      // rejection propagate through the microtask queue.
      await act(async () => {
        vi.advanceTimersByTime(31_000);
        await Promise.resolve();
      });

      expect((capturedSignal as AbortSignal | null)?.aborted).toBe(true);
      // The aborted request surfaces as an error (loading false), not a
      // permanent loading spinner pinned at true.
      expect(result.current.loading).toBe(false);
      expect(result.current.error).not.toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });
});
