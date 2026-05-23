// frontend/src/lib/api/client.ts
import { useMemo } from "react";

import { useTenant } from "@/contexts/TenantContext";

export class ApiError extends Error {
  readonly name = "ApiError";
  constructor(
    message: string,
    public readonly status: number,
    public readonly body: unknown,
    public readonly url: string,
  ) {
    super(message);
  }
}

function resolveUrl(path: string): string {
  const raw = import.meta.env.VITE_API_BASE_URL ?? "";
  const base = raw.replace(/\/+$/, "");
  const normalisedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalisedPath}`;
}

function buildHeaders(
  init: HeadersInit | undefined,
  tenantSlug: string,
  hasJsonBody: boolean,
): Headers {
  const headers = new Headers(init);
  if (hasJsonBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  headers.set("X-UMS-Tenant", tenantSlug);
  return headers;
}

async function parseBody(res: Response): Promise<unknown> {
  if (res.status === 204) return undefined;
  const contentType = res.headers.get("Content-Type") ?? "";
  if (contentType.includes("application/json")) return res.json();
  return res.text();
}

type RequestOptions = RequestInit & { bodyIsJson?: boolean };

function withJsonBody(
  body: unknown,
  init: RequestInit = {},
): RequestOptions {
  if (body === undefined) return init;
  if (
    typeof body === "string" ||
    body instanceof FormData ||
    body instanceof URLSearchParams ||
    body instanceof Blob ||
    body instanceof ArrayBuffer ||
    ArrayBuffer.isView(body)
  ) {
    return { ...init, body: body as BodyInit, bodyIsJson: false };
  }
  return { ...init, body: JSON.stringify(body), bodyIsJson: true };
}

export function useApiClient() {
  const { tenantSlug } = useTenant();

  return useMemo(() => {
    async function request<T>(
      method: string,
      path: string,
      init: RequestOptions = {},
    ): Promise<T> {
      const url = resolveUrl(path);
      const { bodyIsJson = false, ...requestInit } = init;
      const headers = buildHeaders(requestInit.headers, tenantSlug, bodyIsJson);
      const res = await fetch(url, { ...requestInit, method, headers });
      if (!res.ok) {
        const body = await parseBody(res);
        throw new ApiError(`${res.status} ${res.statusText}`, res.status, body, url);
      }
      return (await parseBody(res)) as T;
    }

    return {
      get: <T>(path: string, init?: RequestInit) =>
        request<T>("GET", path, init),
      post: <T>(path: string, body?: unknown, init?: RequestInit) =>
        request<T>("POST", path, withJsonBody(body, init)),
      put: <T>(path: string, body?: unknown, init?: RequestInit) =>
        request<T>("PUT", path, withJsonBody(body, init)),
      patch: <T>(path: string, body?: unknown, init?: RequestInit) =>
        request<T>("PATCH", path, withJsonBody(body, init)),
      delete: <T>(path: string, init?: RequestInit) =>
        request<T>("DELETE", path, init),
    };
  }, [tenantSlug]);
}
