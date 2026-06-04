import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  AdsensePaymentListResponse,
  AdsenseSyncResponse,
} from "@/lib/api/types";
import {
  useAdsensePayments,
  useAdsenseSyncActions,
} from "@/lib/api/useAdsense";
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

const SYNC_RESULT: AdsenseSyncResponse = {
  synced_count: 1,
  items: PAYMENTS.items,
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

/** Resolve a promise from outside via a deferred, for ordering concurrent calls. */
function deferred<T>() { // skipcq: JS-0067
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

function methodOf(init: unknown): string { // skipcq: JS-0067
  return ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase();
}

describe("useAdsensePayments", () => {
  it("auto-fetches GET /adsense/payments with the month filter and returns items", async () => {
    fetchMock().mockResolvedValue(jsonResponse(PAYMENTS));
    const { result } = renderHook(
      () => useAdsensePayments({ month: "2026-03" }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/adsense/payments?month=2026-03");
    expect(result.current.data?.items).toHaveLength(1);
    expect(result.current.data?.items[0]?.payment_amount).toBe("930");
    expect(result.current.error).toBeNull();
  });

  it("omits the month param when no month is given", async () => {
    fetchMock().mockResolvedValue(jsonResponse(PAYMENTS));
    const { result } = renderHook(() => useAdsensePayments(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/adsense/payments");
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse(
        { detail: "Missing permission: revenue.view_finalized_payments" },
        403,
      ),
    );
    const { result } = renderHook(
      () => useAdsensePayments({ month: "2026-03" }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});

describe("useAdsenseSyncActions", () => {
  it("POSTs /adsense/sync-payments and resolves with the synced count", async () => {
    fetchMock().mockResolvedValue(jsonResponse(SYNC_RESULT));
    const { result } = renderHook(() => useAdsenseSyncActions(), { wrapper });

    // syncPayments resolves with the sync result OR null (dropped duplicate);
    // the happy path here gets the result, but the type must admit null.
    let resolved: AdsenseSyncResponse | null | undefined;
    await act(async () => {
      resolved = await result.current.syncPayments({
        connector_key: "adsense",
        source_report_id: null,
        reason: "Manual March payment",
        payments: [
          {
            source_account_id: "pub-1",
            month: "2026-03",
            payment_name: "AdSense payment March 2026",
            payment_date: "2026-03-21",
            payment_amount: "930",
            payment_currency: "USD",
            payment_status: "PAID",
            raw_payload: {},
          },
        ],
      });
    });

    const [url, init] = requireFetchArgs();
    expect(url).toBe("/adsense/sync-payments");
    expect(methodOf(init)).toBe("POST");
    expect(resolved?.synced_count).toBe(1);
    expect(result.current.data?.synced_count).toBe(1);
    expect(result.current.error).toBeNull();
  });

  it("captures a typed ApiError (409 locked month) and rejects", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Finance month is locked" }, 409),
    );
    const { result } = renderHook(() => useAdsenseSyncActions(), { wrapper });

    await act(async () => {
      await expect(
        result.current.syncPayments({
          connector_key: "adsense",
          source_report_id: null,
          reason: "Manual March payment",
          payments: [
            {
              source_account_id: "pub-1",
              month: "2026-03",
              payment_name: "AdSense payment March 2026",
              payment_date: "2026-03-21",
              payment_amount: "930",
              payment_currency: "USD",
              payment_status: "PAID",
              raw_payload: {},
            },
          ],
        }),
      ).rejects.toMatchObject({ name: "ApiError", status: 409 });
    });

    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 409 });
  });

  it("drops a same-tick duplicate sync: exactly one POST, one upsert batch", async () => {
    const first = deferred<Response>();
    // Only ONE response is queued; a leaked second POST would have no mock.
    fetchMock().mockReturnValueOnce(first.promise);
    const { result } = renderHook(() => useAdsenseSyncActions(), { wrapper });

    const ONLY = { ...SYNC_RESULT, synced_count: 1 };
    const SYNC_BODY = {
      connector_key: "adsense",
      source_report_id: null,
      reason: "Manual March payment",
      payments: PAYMENTS.items.map((item) => ({
        source_account_id: item.source_account_id,
        month: item.month,
        payment_name: item.payment_name,
        payment_date: item.payment_date,
        payment_amount: item.payment_amount,
        payment_currency: item.payment_currency,
        payment_status: item.payment_status,
        raw_payload: item.raw_payload,
      })),
    };
    let firstResolved: AdsenseSyncResponse | null | undefined;
    let secondResolved: AdsenseSyncResponse | null | undefined;

    await act(async () => {
      // Double-click before re-render: both run off the same render closure;
      // a leaked second POST would double-upsert the payment batch.
      const p1 = result.current.syncPayments(SYNC_BODY).catch(() => undefined);
      const p2 = result.current.syncPayments(SYNC_BODY).catch(() => undefined);
      first.resolve(jsonResponse(ONLY));
      [firstResolved, secondResolved] = await Promise.all([p1, p2]);
    });

    // Exactly one POST fired; the duplicate was dropped, not queued.
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(firstResolved).toMatchObject({ synced_count: 1 });
    expect(secondResolved).toBeNull();
    expect(result.current.data?.synced_count).toBe(1);
    expect(result.current.loading).toBe(false);
  });

  it("allows a fresh sync after the in-flight one settles", async () => {
    // A fresh Response per call: a Response body can only be read once.
    fetchMock().mockImplementation(() => jsonResponse(SYNC_RESULT));
    const { result } = renderHook(() => useAdsenseSyncActions(), { wrapper });

    const SYNC_BODY = {
      connector_key: "adsense",
      source_report_id: null,
      reason: "Manual March payment",
      payments: PAYMENTS.items.map((item) => ({
        source_account_id: item.source_account_id,
        month: item.month,
        payment_name: item.payment_name,
        payment_date: item.payment_date,
        payment_amount: item.payment_amount,
        payment_currency: item.payment_currency,
        payment_status: item.payment_status,
        raw_payload: item.raw_payload,
      })),
    };
    // First sync settles and clears the in-flight latch.
    await act(async () => {
      await result.current.syncPayments(SYNC_BODY);
    });
    // A later, non-overlapping sync must NOT be dropped.
    await act(async () => {
      await result.current.syncPayments(SYNC_BODY);
    });

    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(result.current.data?.synced_count).toBe(1);
  });
});
