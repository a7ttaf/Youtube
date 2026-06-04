import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { NumberExplanation } from "@/lib/api/types";
import { useExplanation } from "@/lib/api/useExplanation";
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

/** Resolve a promise from outside via a deferred, for ordering concurrent calls. */
function deferred<T>() {
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

  it("keeps the newer result when an older in-flight Explain resolves last", async () => {
    const first = deferred<Response>();
    const second = deferred<Response>();
    fetchMock()
      .mockReturnValueOnce(first.promise)
      .mockReturnValueOnce(second.promise);
    const { result } = renderHook(() => useExplanation(), { wrapper });

    const FIRST = { ...GROSS_EXPLANATION, value: "111.11" };
    const SECOND = { ...GROSS_EXPLANATION, value: "222.22" };

    await act(async () => {
      // Fire both Explains synchronously so they share the same render closure.
      const p1 = result.current
        .run({
          channelId: "demo-channel-alpha",
          month: "2026-03",
          metric: "adjusted_gross_revenue_usd",
        })
        .catch(() => undefined);
      const p2 = result.current
        .run({
          channelId: "demo-channel-beta",
          month: "2026-03",
          metric: "adjusted_gross_revenue_usd",
        })
        .catch(() => undefined);
      // Resolve the SECOND run first, then the (superseded) FIRST last.
      second.resolve(jsonResponse(SECOND));
      first.resolve(jsonResponse(FIRST));
      await Promise.all([p1, p2]);
    });

    // The stale first response must not overwrite the newer second one.
    expect(result.current.data?.value).toBe("222.22");
    expect(result.current.loading).toBe(false);
  });
});
