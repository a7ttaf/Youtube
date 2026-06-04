import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NumberExplanation } from "@/lib/api/types";
import { useExplanation } from "@/lib/api/useExplanation";
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
});

// Real-shaped gross explanation (matches NumberExplanationEntry.to_api() gross).
const GROSS_EXPLANATION: NumberExplanation = {
  month: "2026-03",
  entity_type: "channel",
  entity_id: "demo-channel-alpha",
  metric: "adjusted_gross_revenue_usd",
  value: "1234.56",
  currency: "USD",
  formula: "baseline_gross_revenue_usd + approved_manual_override_total_usd",
  confidence: { label: "HIGH", score: "0.9500" },
  components: [
    {
      key: "baseline_gross_revenue_usd",
      label: "Baseline gross revenue",
      value: "1234.56",
      source_kind: "youtube_cms",
      source_report_id: "report-1",
    },
    {
      key: "approved_manual_override_total_usd",
      label: "Approved manual overrides",
      value: "0",
      count: 0,
    },
  ],
  warnings: [],
  audit_event: {
    event_type: "REVENUE_VIEWED",
    entity_type: "number_explanation",
    entity_id: "demo-channel-alpha:2026-03:adjusted_gross_revenue_usd",
    scope_type: "channel",
    scope_id: "demo-channel-alpha",
    reason: null,
    sensitive: true,
  },
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

/** Resolve a promise from outside via a deferred, for ordering concurrent calls. */
function deferred<T>() { // skipcq: JS-0067
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

describe("useExplanation", () => {
  it("starts idle (no data, no loading, no error) before run()", () => {
    fetchMock().mockResolvedValue(jsonResponse(GROSS_EXPLANATION));
    const { result } = renderHook(() => useExplanation(), { wrapper });
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
    // No fetch fires until the user triggers run().
    expect(fetchMock()).not.toHaveBeenCalled();
  });

  it("POSTs to the explain endpoint with the metric query param and returns the explanation", async () => {
    fetchMock().mockResolvedValue(jsonResponse(GROSS_EXPLANATION));
    const { result } = renderHook(() => useExplanation(), { wrapper });

    await act(async () => {
      await result.current.run({
        channelId: "demo-channel-alpha",
        month: "2026-03",
        metric: "adjusted_gross_revenue_usd",
      });
    });

    const [url, init] = requireFetchArgs();
    expect(url).toBe(
      "/revenue/channels/demo-channel-alpha/months/2026-03/explain?metric=adjusted_gross_revenue_usd",
    );
    expect((init as RequestInit).method).toBe("POST");
    expect(result.current.data?.value).toBe("1234.56");
    expect(result.current.loading).toBe(false);
    expect(result.current.error).toBeNull();
  });

  it("path- and query-encodes the channel id, month, and metric", async () => {
    fetchMock().mockResolvedValue(jsonResponse(GROSS_EXPLANATION));
    const { result } = renderHook(() => useExplanation(), { wrapper });

    await act(async () => {
      await result.current.run({
        channelId: "demo/alpha",
        month: "2026-03",
        metric: "net_revenue_usd",
      });
    });

    expect(requireFetchArgs()[0]).toBe(
      "/revenue/channels/demo%2Falpha/months/2026-03/explain?metric=net_revenue_usd",
    );
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: view:finalized-payments" }, 403),
    );
    const { result } = renderHook(() => useExplanation(), { wrapper });

    await act(async () => {
      await expect(
        result.current.run({
          channelId: "demo-channel-alpha",
          month: "2026-03",
          metric: "net_revenue_usd",
        }),
      ).rejects.toMatchObject({ name: "ApiError", status: 403 });
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("shows the latest result across two sequential Explains (supersession token)", async () => {
    // Each Explain fully settles before the next begins (the in-flight latch
    // clears in finally), so both POST and the supersession token lets the second
    // result replace the first. A fresh Response per call (bodies read once).
    fetchMock()
      .mockResolvedValueOnce(jsonResponse({ ...GROSS_EXPLANATION, value: "111.11" }))
      .mockResolvedValueOnce(jsonResponse({ ...GROSS_EXPLANATION, value: "222.22" }));
    const { result } = renderHook(() => useExplanation(), { wrapper });

    await act(async () => {
      await result.current.run({
        channelId: "demo-channel-alpha",
        month: "2026-03",
        metric: "adjusted_gross_revenue_usd",
      });
    });
    expect(result.current.data?.value).toBe("111.11");

    await act(async () => {
      await result.current.run({
        channelId: "demo-channel-beta",
        month: "2026-03",
        metric: "adjusted_gross_revenue_usd",
      });
    });

    // Both Explains were dispatched (sequential, not same-tick) and the newer
    // result is the one shown.
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(result.current.data?.value).toBe("222.22");
    expect(result.current.loading).toBe(false);
  });

  it("drops a same-tick duplicate Explain: exactly one POST, one explanation", async () => {
    const first = deferred<Response>();
    // Only ONE response is queued; a second POST would have no mock and surface
    // the dedupe regression as an undefined-response failure.
    fetchMock().mockReturnValueOnce(first.promise);
    const { result } = renderHook(() => useExplanation(), { wrapper });

    const ONLY = { ...GROSS_EXPLANATION, value: "999.99" };
    let firstResolved: NumberExplanation | null | undefined;
    let secondResolved: NumberExplanation | null | undefined;

    await act(async () => {
      // Double-click before re-render: both calls run off the same render
      // closure (the state `loading` guard cannot catch the second).
      const p1 = result.current
        .run({
          channelId: "demo-channel-alpha",
          month: "2026-03",
          metric: "adjusted_gross_revenue_usd",
        })
        .catch(() => undefined);
      const p2 = result.current
        .run({
          channelId: "demo-channel-alpha",
          month: "2026-03",
          metric: "adjusted_gross_revenue_usd",
        })
        .catch(() => undefined);
      first.resolve(jsonResponse(ONLY));
      [firstResolved, secondResolved] = await Promise.all([p1, p2]);
    });

    // Exactly one POST was dispatched; the duplicate was dropped, not queued, so
    // only one REVENUE_VIEWED audit event is written server-side.
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    // The surviving call resolves with the explanation; the dropped one with null.
    expect(firstResolved).toMatchObject({ value: "999.99" });
    expect(secondResolved).toBeNull();
    expect(result.current.data?.value).toBe("999.99");
    expect(result.current.loading).toBe(false);
  });

  it("reset() discards an in-flight result (stays null) and does not stick the latch", async () => {
    const inFlight = deferred<Response>();
    // First call hangs (deferred); the post-reset call resolves immediately.
    fetchMock()
      .mockReturnValueOnce(inFlight.promise)
      .mockImplementation(() => jsonResponse({ ...GROSS_EXPLANATION, value: "777.77" }));
    const { result } = renderHook(() => useExplanation(), { wrapper });

    // Start a request and leave it in flight.
    let inFlightRun!: Promise<NumberExplanation | null>;
    act(() => {
      inFlightRun = result.current
        .run({
          channelId: "demo-channel-alpha",
          month: "2026-03",
          metric: "adjusted_gross_revenue_usd",
        })
        .catch(() => null);
    });

    // The operator changes filters: reset() abandons the in-flight request.
    act(() => {
      result.current.reset();
    });

    // The old request settles AFTER reset(): its token is now stale, so it must
    // be discarded and must NOT commit data.
    await act(async () => {
      inFlight.resolve(jsonResponse({ ...GROSS_EXPLANATION, value: "111.11" }));
      await inFlightRun;
    });
    expect(result.current.data).toBeNull();
    expect(result.current.loading).toBe(false);

    // The latch was cleared by reset(): a subsequent run() is not blocked.
    await act(async () => {
      await result.current.run({
        channelId: "demo-channel-beta",
        month: "2026-03",
        metric: "adjusted_gross_revenue_usd",
      });
    });
    expect(result.current.data?.value).toBe("777.77");
    expect(fetchMock()).toHaveBeenCalledTimes(2);
  });

  it("a stale completion cannot unlock the latch a newer Explain owns", async () => {
    // Race: run A -> reset() releases the latch -> run B reclaims it -> A
    // settles late. A's finally must NOT release B's latch, so a same-tick
    // duplicate of B is still dropped (no third POST / duplicate audit event).
    const requestA = deferred<Response>();
    const requestB = deferred<Response>();
    fetchMock()
      .mockReturnValueOnce(requestA.promise)
      .mockReturnValueOnce(requestB.promise)
      .mockImplementation(() => jsonResponse(GROSS_EXPLANATION));
    const { result } = renderHook(() => useExplanation(), { wrapper });

    // A starts and stays in flight.
    let runA!: Promise<NumberExplanation | null>;
    act(() => {
      runA = result.current
        .run({
          channelId: "demo-channel-alpha",
          month: "2026-03",
          metric: "adjusted_gross_revenue_usd",
        })
        .catch(() => null);
    });
    // Filters change: reset() abandons A and releases the latch.
    act(() => {
      result.current.reset();
    });
    // B starts and now OWNS the latch.
    let runB!: Promise<NumberExplanation | null>;
    act(() => {
      runB = result.current
        .run({
          channelId: "demo-channel-beta",
          month: "2026-03",
          metric: "adjusted_gross_revenue_usd",
        })
        .catch(() => null);
    });
    // A settles late while B is still in flight: it must not unlock B's latch.
    await act(async () => {
      requestA.resolve(jsonResponse({ ...GROSS_EXPLANATION, value: "111.11" }));
      await runA;
    });
    // A same-tick duplicate of B must still be dropped (latch intact).
    let duplicateOfB!: Promise<NumberExplanation | null>;
    act(() => {
      duplicateOfB = result.current.run({
        channelId: "demo-channel-beta",
        month: "2026-03",
        metric: "adjusted_gross_revenue_usd",
      });
    });
    await act(async () => {
      await expect(duplicateOfB).resolves.toBeNull();
      requestB.resolve(jsonResponse({ ...GROSS_EXPLANATION, value: "222.22" }));
      await runB;
    });

    // Exactly two POSTs (A and B); the duplicate never fired.
    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(result.current.data?.value).toBe("222.22");
  });

  it("allows a fresh Explain after the in-flight request settles", async () => {
    // A fresh Response per call: a Response body can only be read once.
    fetchMock().mockImplementation(() => jsonResponse(GROSS_EXPLANATION));
    const { result } = renderHook(() => useExplanation(), { wrapper });

    // First Explain completes and clears the in-flight latch.
    await act(async () => {
      await result.current.run({
        channelId: "demo-channel-alpha",
        month: "2026-03",
        metric: "adjusted_gross_revenue_usd",
      });
    });
    // A later, non-overlapping Explain must NOT be dropped.
    await act(async () => {
      await result.current.run({
        channelId: "demo-channel-alpha",
        month: "2026-03",
        metric: "adjusted_gross_revenue_usd",
      });
    });

    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(result.current.data?.value).toBe(GROSS_EXPLANATION.value);
  });
});
