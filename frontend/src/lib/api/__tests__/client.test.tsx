// frontend/src/lib/api/__tests__/client.test.ts
import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { ApiError, useApiClient } from "@/lib/api/client";
import { TenantProvider } from "@/contexts/TenantContext";

function wrapper({ children }: { children: React.ReactNode }) {
  return <TenantProvider>{children}</TenantProvider>;
}

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.unstubAllEnvs();
});

function jsonResponse(body: unknown, init: ResponseInit = {}) {
  return new Response(JSON.stringify(body), {
    status: 200,
    headers: { "Content-Type": "application/json" },
    ...init,
  });
}

function textResponse(body: string, init: ResponseInit = {}) {
  return new Response(body, {
    status: 200,
    headers: { "Content-Type": "text/plain" },
    ...init,
  });
}

function lastFetchArgs() {
  return (globalThis.fetch as unknown as ReturnType<typeof vi.fn>).mock.calls.at(-1);
}

describe("useApiClient header injection", () => {
  it("injects X-UMS-Tenant: ums when caller passes no headers", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x");
    const [, init] = lastFetchArgs()!;
    const headers = new Headers(init?.headers);
    expect(headers.get("X-UMS-Tenant")).toBe("ums");
  });

  it("overrides caller-supplied X-UMS-Tenant with the provider slug", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x", { headers: { "X-UMS-Tenant": "evil" } });
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.get("X-UMS-Tenant")).toBe("ums");
  });

  it.each([
    ["Headers instance", new Headers([["X-Other", "1"]])],
    ["array of tuples", [["X-Other", "1"]] as HeadersInit],
    ["plain object", { "X-Other": "1" } as HeadersInit],
  ])("normalises %s and still ships X-UMS-Tenant: ums", async (_, headersInit) => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x", { headers: headersInit });
    const sent = new Headers(lastFetchArgs()![1]?.headers);
    expect(sent.get("X-UMS-Tenant")).toBe("ums");
    expect(sent.get("X-Other")).toBe("1");
  });
});

describe("useApiClient URL resolution", () => {
  it("resolves to a relative URL when VITE_API_BASE_URL is unset", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/tenants/me");
    expect(lastFetchArgs()![0]).toBe("/tenants/me");
  });

  it("strips trailing slash from VITE_API_BASE_URL", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com/");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/tenants/me");
    expect(lastFetchArgs()![0]).toBe("https://api.example.com/tenants/me");
  });

  it("passes through an absolute https:// path without prepending the base", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("https://other.example.com/tenants/me");
    expect(lastFetchArgs()![0]).toBe("https://other.example.com/tenants/me");
  });

  it("passes through an absolute http:// path without prepending the base", async () => {
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com");
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("http://127.0.0.1:8000/tenants/me");
    expect(lastFetchArgs()![0]).toBe("http://127.0.0.1:8000/tenants/me");
  });
});

describe("useApiClient Accept header default", () => {
  it("sets Accept: application/json by default on GET", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x");
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.get("Accept")).toBe("application/json");
  });

  it("preserves a caller-supplied Accept header instead of overwriting it", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x", { headers: { Accept: "text/csv" } });
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.get("Accept")).toBe("text/csv");
  });
});

describe("useApiClient Content-Type handling", () => {
  it("does not set Content-Type on GET", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.get("/x");
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.has("Content-Type")).toBe(false);
  });

  it("sets Content-Type: application/json on POST with a plain object body", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await result.current.post("/x", { foo: 1 });
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.get("Content-Type")).toBe("application/json");
    expect(lastFetchArgs()![1]?.body).toBe(JSON.stringify({ foo: 1 }));
  });

  it("does not set Content-Type on POST with FormData (lets browser set multipart)", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ ok: true }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    const fd = new FormData();
    fd.append("k", "v");
    await result.current.post("/x", fd);
    const headers = new Headers(lastFetchArgs()![1]?.headers);
    expect(headers.has("Content-Type")).toBe(false);
  });
});

describe("useApiClient response handling", () => {
  it("returns the parsed JSON body on 200", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ id: "abc", slug: "ums", display_name: "UMS" }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    const payload = await result.current.get<{ id: string }>("/tenants/me");
    expect(payload.id).toBe("abc");
  });

  it("resolves to undefined on 204", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response(null, { status: 204 }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    const payload = await result.current.delete("/x");
    expect(payload).toBeUndefined();
  });

  it("throws ApiError with parsed JSON body on 4xx", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      jsonResponse({ detail: "Tenant slug must not be blank" }, { status: 400 }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await expect(result.current.get("/tenants/me")).rejects.toMatchObject({
      name: "ApiError",
      status: 400,
      body: { detail: "Tenant slug must not be blank" },
    });
  });

  it("throws ApiError with raw text body on 5xx text response", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      textResponse("upstream timed out", { status: 503 }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await expect(result.current.get("/x")).rejects.toMatchObject({
      name: "ApiError",
      status: 503,
      body: "upstream timed out",
    });
  });

  it("propagates fetch rejection (TypeError) unwrapped", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockRejectedValue(
      new TypeError("Failed to fetch"),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await expect(result.current.get("/x")).rejects.toBeInstanceOf(TypeError);
    await expect(result.current.get("/x")).rejects.not.toBeInstanceOf(ApiError);
  });

  it("returns undefined for an empty 200 body that claims application/json", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("", {
        status: 200,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    const payload = await result.current.get("/x");
    expect(payload).toBeUndefined();
  });

  it("wraps a malformed application/json 5xx body in ApiError with the raw text body", async () => {
    (globalThis.fetch as ReturnType<typeof vi.fn>).mockResolvedValue(
      new Response("<html>500 internal</html>", {
        status: 500,
        headers: { "Content-Type": "application/json" },
      }),
    );
    const { result } = renderHook(() => useApiClient(), { wrapper });
    await expect(result.current.get("/x")).rejects.toMatchObject({
      name: "ApiError",
      status: 500,
      body: "<html>500 internal</html>",
    });
  });
});
