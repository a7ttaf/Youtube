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

function methodOf(init: unknown): string {
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
    expect(lastFetchArgs()![0]).toBe("/adsense/payments?month=2026-03");
    expect(result.current.data?.items).toHaveLength(1);
    expect(result.current.data?.items[0]?.payment_amount).toBe("930");
    expect(result.current.error).toBeNull();
  });

  it("omits the month param when no month is given", async () => {
    fetchMock().mockResolvedValue(jsonResponse(PAYMENTS));
    const { result } = renderHook(() => useAdsensePayments(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(lastFetchArgs()![0]).toBe("/adsense/payments");
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

    let resolved: AdsenseSyncResponse | undefined;
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

    const [url, init] = lastFetchArgs()!;
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
});
