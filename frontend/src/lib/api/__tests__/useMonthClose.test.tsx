import { renderHook, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type {
  FinanceCloseReadinessResponse,
  FinanceMonthCloseStatus,
} from "@/lib/api/types";
import {
  useMonthClose,
  useMonthCloseActions,
  useMonthCloseReadiness,
} from "@/lib/api/useMonthClose";
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

// Real-shaped close status (matches FinanceMonthCloseEntry.to_api()).
const CLOSE_STATUS: FinanceMonthCloseStatus = {
  month: "2026-03",
  status: "OPEN",
  allocation_method: null,
  allocation_rule_payload: {},
  locked_by: null,
  locked_at: null,
  unlocked_by: null,
  unlocked_at: null,
};

// Real-shaped readiness (matches FinanceCloseReadiness.to_api()).
const READINESS_BLOCKED: FinanceCloseReadinessResponse = {
  month: "2026-03",
  ready: false,
  blockers: [
    {
      blocker_type: "PENDING_MANUAL_OVERRIDES",
      severity: "HIGH",
      count: 2,
      message: "2 pending manual overrides require approval before locking 2026-03.",
    },
  ],
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const fetchMock = (): ReturnType<typeof vi.fn> => {
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

describe("useMonthClose", () => {
  it("requests GET /finance-close/{month} and returns the parsed status", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CLOSE_STATUS));
    const { result } = renderHook(() => useMonthClose({ month: "2026-03" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/finance-close/2026-03");
    expect(result.current.error).toBeNull();
    expect(result.current.data?.status).toBe("OPEN");
  });

  it("surfaces a typed ApiError (403) with no data on permission failure", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: view:revenue" }, 403),
    );
    const { result } = renderHook(() => useMonthClose({ month: "2026-03" }), {
      wrapper,
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});

describe("useMonthCloseReadiness", () => {
  it("requests GET /finance-close/{month}/readiness and returns blockers", async () => {
    fetchMock().mockResolvedValue(jsonResponse(READINESS_BLOCKED));
    const { result } = renderHook(
      () => useMonthCloseReadiness({ month: "2026-03" }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/finance-close/2026-03/readiness");
    expect(result.current.data?.ready).toBe(false);
    expect(result.current.data?.blockers[0]?.blocker_type).toBe(
      "PENDING_MANUAL_OVERRIDES",
    );
  });
});

describe("useMonthCloseActions", () => {
  it("POSTs the reason body to the lock endpoint", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ ...CLOSE_STATUS, status: "LOCKED", audit_event: {} }),
    );
    const { result } = renderHook(
      () => useMonthCloseActions({ month: "2026-03" }),
      { wrapper },
    );
    await result.current.lock("March close complete");
    const [url, init] = requireFetchArgs();
    expect(url).toBe("/finance-close/2026-03/lock");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      reason: "March close complete",
    });
  });

  it("POSTs the reason body to the unlock endpoint", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ ...CLOSE_STATUS, status: "OPEN", audit_event: {} }),
    );
    const { result } = renderHook(
      () => useMonthCloseActions({ month: "2026-03" }),
      { wrapper },
    );
    await result.current.unlock("Reopen for late correction");
    const [url, init] = requireFetchArgs();
    expect(url).toBe("/finance-close/2026-03/unlock");
    expect((init as RequestInit).method).toBe("POST");
    expect(JSON.parse((init as RequestInit).body as string)).toEqual({
      reason: "Reopen for late correction",
    });
  });

  it("propagates a typed 409 ApiError carrying the blocker detail", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse(
        {
          detail: {
            message: "Finance month has unresolved close blockers",
            blockers: READINESS_BLOCKED.blockers,
          },
        },
        409,
      ),
    );
    const { result } = renderHook(
      () => useMonthCloseActions({ month: "2026-03" }),
      { wrapper },
    );
    await expect(result.current.lock("try lock")).rejects.toMatchObject({
      name: "ApiError",
      status: 409,
    });
  });
});
