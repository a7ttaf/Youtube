import { renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ConnectorCredentialHealthResponse } from "@/lib/api/types";
import { useConnectorCredentialHealth } from "@/lib/api/useConnectorHealth";
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

// Real-shaped credential-health page: the four persisted refresh-telemetry
// columns plus the derived health_state, under the {credentials: [...]} envelope.
const HEALTH: ConnectorCredentialHealthResponse = {
  credentials: [
    {
      id: "11111111-1111-1111-1111-111111111111",
      connector_key: "youtube_reporting",
      account_id: "acct-1",
      status: "ACTIVE",
      has_secret_ref: true,
      last_refresh_attempt_at: "2026-03-01T12:00:00Z",
      token_expiry_at: "2030-01-01T00:00:00Z",
      last_refresh_status: "succeeded",
      last_refresh_error_class: null,
      health_state: "healthy",
    },
  ],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
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

describe("useConnectorCredentialHealth", () => {
  it("auto-fetches GET /connectors/credentials/health and returns the credentials array", async () => {
    fetchMock().mockResolvedValue(jsonResponse(HEALTH));
    const { result } = renderHook(() => useConnectorCredentialHealth(), {
      wrapper,
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe("/connectors/credentials/health");
    // The hook unwraps the {credentials: [...]} envelope to the array.
    expect(result.current.data).toHaveLength(1);
    expect(result.current.data?.[0]?.connector_key).toBe("youtube_reporting");
    expect(result.current.data?.[0]?.health_state).toBe("healthy");
    expect(result.current.data?.[0]?.last_refresh_status).toBe("succeeded");
    expect(result.current.data?.[0]?.token_expiry_at).toBe(
      "2030-01-01T00:00:00Z",
    );
    expect(result.current.error).toBeNull();
  });

  it("encodes limit/offset query params when provided", async () => {
    fetchMock().mockResolvedValue(jsonResponse(HEALTH));
    const { result } = renderHook(
      () => useConnectorCredentialHealth({ limit: 10, offset: 20 }),
      { wrapper },
    );

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(requireFetchArgs()[0]).toBe(
      "/connectors/credentials/health?limit=10&offset=20",
    );
  });

  it("captures a typed ApiError (403) and clears data", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: connectors.view_health" }, 403),
    );
    const { result } = renderHook(() => useConnectorCredentialHealth(), {
      wrapper,
    });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});
