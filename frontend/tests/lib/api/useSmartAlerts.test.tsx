import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { useSmartAlerts } from "@/lib/api/useSmartAlerts";
import type { SmartAlertsSummary } from "@/lib/api/types";
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

// Real-shaped smart-alerts payload (matches MonthlySmartAlertSummary.to_api()
// + the route-level audit_events). Alert codes/severities mirror the backend
// test_smart_alerts_api.py "ATTENTION_REQUIRED" case.
const SMART_ALERTS_BODY: SmartAlertsSummary = {
  month: "2026-03",
  status: "ATTENTION_REQUIRED",
  highest_severity: "HIGH",
  alert_count: 2,
  alerts: [
    {
      code: "PAYMENT_NOT_MATCHED",
      severity: "HIGH",
      message: "AdSense payment is not matched for 2026-03.",
      source: "payment_match",
      confidence: "E_MISSING",
      details: { payment_match_status: "NO_PAYMENT", payment_gap_usd: "500.00" },
    },
    {
      code: "MONTH_NOT_LOCKED",
      severity: "MEDIUM",
      message: "Finance month 2026-03 is not locked.",
      source: "finance_close",
      confidence: "D_ESTIMATED",
      details: { close_status: "OPEN" },
    },
  ],
  audit_events: [],
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const lastFetchArgs = () => {
  return (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.at(-1);
};

/** Narrow the last fetch args away from `undefined`, failing the test if none. */
const requireFetchArgs = () => {
  const args = lastFetchArgs();
  if (!args) throw new Error("expected fetch to have been called");
  return args;
};

describe("useSmartAlerts", () => {
  it("requests the smart-alerts endpoint with the encoded month path", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(SMART_ALERTS_BODY),
    );
    const { result } = renderHook(() => useSmartAlerts({ month: "2026-03" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/revenue/months/2026-03/smart-alerts");
  });

  it("returns the parsed real-shaped data on success and clears loading", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse(SMART_ALERTS_BODY),
    );
    const { result } = renderHook(() => useSmartAlerts({ month: "2026-03" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.error).toBeNull();
    expect(result.current.data?.status).toBe("ATTENTION_REQUIRED");
    expect(result.current.data?.highest_severity).toBe("HIGH");
    expect(result.current.data?.alerts[0]?.code).toBe("PAYMENT_NOT_MATCHED");
  });

  it("surfaces a typed ApiError (403) with no data when finance permissions are missing", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "missing permission view:bank_reconciliation" }, 403),
    );
    const { result } = renderHook(() => useSmartAlerts({ month: "2026-03" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});
