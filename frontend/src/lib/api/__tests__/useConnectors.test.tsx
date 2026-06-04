import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type {
  ConnectorCredentialListResponse,
  ConnectorJobResponse,
} from "@/lib/api/types";
import {
  useConnectorCredentials,
  useConnectorJobActions,
} from "@/lib/api/useConnectors";
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

const CREDENTIALS: ConnectorCredentialListResponse = {
  items: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      connector_key: "youtube_reporting",
      account_id: "acct-1",
      status: "ACTIVE",
      has_secret_ref: true,
    },
  ],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
};

const JOB_RESULT: ConnectorJobResponse = {
  connector_key: "youtube_reporting",
  account_id: "acct-1",
  execution_status: "recorded_not_executed",
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

describe("useConnectorCredentials", () => {
  it("auto-fetches GET /connectors/credentials on mount and returns the items", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CREDENTIALS));
    const { result } = renderHook(() => useConnectorCredentials(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(lastFetchArgs()![0]).toBe("/connectors/credentials");
    expect(result.current.data?.items).toHaveLength(1);
    expect(result.current.data?.items[0]?.connector_key).toBe(
      "youtube_reporting",
    );
    expect(result.current.data?.items[0]?.has_secret_ref).toBe(true);
    expect(result.current.error).toBeNull();
  });

  it("encodes limit/offset query params when provided", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CREDENTIALS));
    const { result } = renderHook(
      () => useConnectorCredentials({ limit: 10, offset: 20 }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(lastFetchArgs()![0]).toBe("/connectors/credentials?limit=10&offset=20");
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: connectors.manage" }, 403),
    );
    const { result } = renderHook(() => useConnectorCredentials(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});

describe("useConnectorJobActions", () => {
  it("POSTs /connectors/jobs and resolves with the recorded-not-executed result", async () => {
    fetchMock().mockResolvedValue(jsonResponse(JOB_RESULT, 202));
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });

    let resolved: ConnectorJobResponse | undefined;
    await act(async () => {
      resolved = await result.current.requestJob({
        connector_key: "youtube_reporting",
        account_id: "acct-1",
        reason: "Manual resync",
      });
    });

    const [url, init] = lastFetchArgs()!;
    expect(url).toBe("/connectors/jobs");
    expect(methodOf(init)).toBe("POST");
    expect(resolved?.execution_status).toBe("recorded_not_executed");
    expect(result.current.data?.execution_status).toBe("recorded_not_executed");
    expect(result.current.error).toBeNull();
  });

  it("captures a typed ApiError (403) and rejects", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: connectors.run_jobs" }, 403),
    );
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });

    await act(async () => {
      await expect(
        result.current.requestJob({
          connector_key: "youtube_reporting",
          account_id: "acct-1",
          reason: "Manual resync",
        }),
      ).rejects.toMatchObject({ name: "ApiError", status: 403 });
    });

    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});
