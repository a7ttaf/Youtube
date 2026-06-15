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
  report_month: "2026-03",
  dry_run: false,
  execution_status: "submitted",
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

describe("useConnectorCredentials", () => {
  it("auto-fetches GET /connectors/credentials on mount and returns the items", async () => {
    fetchMock().mockResolvedValue(jsonResponse(CREDENTIALS));
    const { result } = renderHook(() => useConnectorCredentials(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/connectors/credentials");
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
    expect(requireFetchArgs()[0]).toBe("/connectors/credentials?limit=10&offset=20");
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

    // requestJob resolves with the job result OR null (dropped duplicate); the
    // happy path here gets the result, but the type must admit null.
    let resolved: ConnectorJobResponse | null | undefined;
    await act(async () => {
      resolved = await result.current.requestJob({
        connector_key: "youtube_reporting",
        account_id: "acct-1",
        report_month: "2026-03",
        reason: "Manual resync",
      });
    });

    const [url, init] = requireFetchArgs();
    expect(url).toBe("/connectors/jobs");
    expect(methodOf(init)).toBe("POST");
    expect(resolved?.execution_status).toBe("submitted");
    expect(result.current.data?.execution_status).toBe("submitted");
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
          report_month: "2026-03",
          reason: "Manual resync",
        }),
      ).rejects.toMatchObject({ name: "ApiError", status: 403 });
    });

    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("drops a same-tick duplicate submit: exactly one POST, one audit", async () => {
    const first = deferred<Response>();
    // Only ONE response is queued; a leaked second POST would have no mock.
    fetchMock().mockReturnValueOnce(first.promise);
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });

    const ONLY = { ...JOB_RESULT, account_id: "only" };
    const JOB_BODY = {
      connector_key: "youtube_reporting",
      account_id: "acct-1",
      report_month: "2026-03",
      reason: "Manual resync",
    };
    let firstResolved: ConnectorJobResponse | null | undefined;
    let secondResolved: ConnectorJobResponse | null | undefined;

    await act(async () => {
      // Double-click before re-render: both run off the same render closure.
      const p1 = result.current.requestJob(JOB_BODY).catch(() => undefined);
      const p2 = result.current.requestJob(JOB_BODY).catch(() => undefined);
      first.resolve(jsonResponse(ONLY, 202));
      [firstResolved, secondResolved] = await Promise.all([p1, p2]);
    });

    // Exactly one POST fired; the duplicate was dropped, not queued.
    expect(fetchMock()).toHaveBeenCalledTimes(1);
    expect(firstResolved).toMatchObject({ account_id: "only" });
    expect(secondResolved).toBeNull();
    expect(result.current.data?.account_id).toBe("only");
    expect(result.current.loading).toBe(false);
  });

  it("includes report_month + dry_run in the POST body and resolves submitted", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse(
        {
          connector_key: "youtube_reporting",
          account_id: "acct-1",
          report_month: "2026-03",
          dry_run: true,
          execution_status: "submitted",
          audit_event: {},
        },
        202,
      ),
    );
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });
    let resolved: ConnectorJobResponse | null | undefined;
    await act(async () => {
      resolved = await result.current.requestJob({
        connector_key: "youtube_reporting",
        account_id: "acct-1",
        report_month: "2026-03",
        dry_run: true,
        reason: "Manual pull",
      });
    });
    const [, init] = requireFetchArgs();
    const body = JSON.parse(String((init as RequestInit).body ?? "{}"));
    expect(body.report_month).toBe("2026-03");
    expect(body.dry_run).toBe(true);
    expect(resolved?.execution_status).toBe("submitted");
  });

  it("rejects with a typed 503 when the executor is disabled", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Connector job executor is disabled" }, 503),
    );
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });
    await act(async () => {
      await expect(
        result.current.requestJob({
          connector_key: "youtube_reporting",
          account_id: "acct-1",
          report_month: "2026-03",
          reason: "While disabled",
        }),
      ).rejects.toMatchObject({ name: "ApiError", status: 503 });
    });
    expect(result.current.error).toMatchObject({ status: 503 });
  });

  it("allows a fresh job request after the in-flight one settles", async () => {
    // A fresh Response per call: a Response body can only be read once.
    fetchMock().mockImplementation(() => jsonResponse(JOB_RESULT, 202));
    const { result } = renderHook(() => useConnectorJobActions(), { wrapper });

    const JOB_BODY = {
      connector_key: "youtube_reporting",
      account_id: "acct-1",
      report_month: "2026-03",
      reason: "Manual resync",
    };
    // First request settles and clears the in-flight latch.
    await act(async () => {
      await result.current.requestJob(JOB_BODY);
    });
    // A later, non-overlapping request must NOT be dropped.
    await act(async () => {
      await result.current.requestJob(JOB_BODY);
    });

    expect(fetchMock()).toHaveBeenCalledTimes(2);
    expect(result.current.data?.execution_status).toBe("submitted");
  });
});
