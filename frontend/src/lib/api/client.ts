// frontend/src/lib/api/client.ts
import { useMemo } from "react";

import { useTenant } from "@/contexts/TenantContext";

export class ApiError extends Error { // skipcq: JS-D1001
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

/**
 * Resolve a request path against the configured API origin.
 *
 * An already-absolute http(s) URL is returned untouched. Otherwise the path is
 * prefixed with VITE_API_BASE_URL (trailing slashes stripped); when no base is
 * configured this returns the original relative path unchanged, so same-origin
 * deployments keep byte-identical relative URLs. Exported so non-JSON surfaces
 * (e.g. binary download anchors) can target the same API origin the JSON client
 * uses instead of hard-coding a relative href against the frontend origin.
 */
export function resolveUrl(path: string): string { // skipcq: JS-0067
  if (/^https?:\/\//i.test(path)) return path;
  const raw = import.meta.env.VITE_API_BASE_URL ?? "";
  const base = raw.replace(/\/+$/, "");
  const normalisedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalisedPath}`;
}

function buildHeaders( // skipcq: JS-0067, JS-D1001
  init: HeadersInit | undefined,
  tenantSlug: string,
  hasJsonBody: boolean,
): Headers {
  const headers = new Headers(init);
  if (!headers.has("Accept")) {
    headers.set("Accept", "application/json");
  }
  if (hasJsonBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", "application/json");
  }
  // Only inject X-UMS-Tenant when the provider has a resolved slug. An empty
  // sentinel means we are still in the pre-hydration bootstrap window — the
  // trusted gateway / dev proxy is the authoritative source for tenant
  // identity in that window. Sending a hardcoded fallback here would pin
  // every non-UMS user's /tenants/me to "ums" and 403 their principal load
  // in database-auth mode. Caller-supplied X-UMS-Tenant is also stripped so
  // the browser bundle can never forge a tenant scope.
  if (tenantSlug) {
    headers.set("X-UMS-Tenant", tenantSlug);
  } else {
    headers.delete("X-UMS-Tenant");
  }
  return headers;
}

type ParseBodyOptions = {
  // When true, a malformed `application/json` body REJECTS via JsonParseError.
  // Used on the success path so consumers cannot accidentally process raw
  // HTML/error text as if it were a typed `T`. The error path keeps the
  // permissive default so ApiError.body still carries the raw text for
  // diagnostics (matches the prior CodeRabbit "preserve ApiError body"
  // review on PR #41).
  strictJson?: boolean;
};

class JsonParseError extends Error { // skipcq: JS-D1001
  readonly name = "JsonParseError";
  constructor(
    message: string,
    public readonly rawText: string,
  ) {
    super(message);
  }
}

async function parseBody( // skipcq: JS-0067, JS-D1001, JS-R1005
  res: Response,
  options: ParseBodyOptions = {},
): Promise<unknown> {
  if (res.status === 204 || res.status === 205 || res.status === 304) {
    return undefined;
  }
  const contentType = res.headers.get("Content-Type") ?? "";
  const text = await res.text();
  if (!contentType.includes("application/json")) return text;
  if (text.length === 0) return undefined;
  try {
    return JSON.parse(text);
  } catch (parseError) {
    if (options.strictJson) {
      const reason = parseError instanceof Error ? parseError.message : "parse failed";
      throw new JsonParseError(reason, text);
    }
    return text;
  }
}

type RequestOptions = RequestInit & { bodyIsJson?: boolean };

function withJsonBody( // skipcq: JS-0067, JS-D1001, JS-R1005
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

export function useApiClient() { // skipcq: JS-0067, JS-D1001
  const { tenantSlug } = useTenant();

  return useMemo(() => {
    async function request<T>( // skipcq: JS-D1001
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
      try {
        return (await parseBody(res, { strictJson: true })) as T;
      } catch (parseError) {
        if (parseError instanceof JsonParseError) {
          // Surface malformed-JSON success bodies through the same ApiError
          // boundary callers already handle, instead of returning raw text
          // typed as `T`. The raw text is preserved on ApiError.body for
          // diagnostics; the status stays the original 2xx so consumers can
          // distinguish "server said OK but lied about JSON" from network 5xx.
          throw new ApiError(
            `${res.status} malformed JSON response: ${parseError.message}`,
            res.status,
            parseError.rawText,
            url,
          );
        }
        throw parseError;
      }
    }

    // ========================================================================
    // Purpose: GET a non-JSON (binary/text) response as a Blob, applying the SAME
    //   tenant/auth header conventions the JSON client uses, and returning both the
    //   blob and the raw Headers so callers can read response headers (e.g. the
    //   audit CSV export's X-Truncated). Kept separate from request<T> because that
    //   path strict-parses JSON and would mangle a CSV body.
    // Standards: Same fail-closed ApiError boundary on non-2xx; same X-UMS-Tenant
    //   injection via buildHeaders. No Accept override is forced so the server may
    //   honor its own content type.
    // Blast Radius: Read-only download surface. No finance math, no mutation.
    // ========================================================================
    async function getBlob( // skipcq: JS-D1001
      path: string,
      init: RequestInit = {},
    ): Promise<{ blob: Blob; headers: Headers }> {
      const url = resolveUrl(path);
      const headers = buildHeaders(init.headers, tenantSlug, false);
      const res = await fetch(url, { ...init, method: "GET", headers });
      if (!res.ok) {
        const body = await parseBody(res);
        throw new ApiError(`${res.status} ${res.statusText}`, res.status, body, url);
      }
      const blob = await res.blob();
      return { blob, headers: res.headers };
    }

    return {
      get: <T>(path: string, init?: RequestInit) =>
        request<T>("GET", path, init),
      getBlob,
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
