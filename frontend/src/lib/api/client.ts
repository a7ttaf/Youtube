// frontend/src/lib/api/client.ts
import { useMemo } from "react";

import { useTenant } from "@/contexts/TenantContext";

export class ApiError extends Error {
  readonly name = "ApiError";

  /**
   * Creates a new instance of ApiError representing an API request failure.
   * @param message - The error message describing the failure.
   * @param status - The HTTP status code returned by the API.
   * @param body - The response body returned by the API.
   * @param url - The URL of the API request that triggered the error.
   */
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
export function resolveUrl(path: string): string {
  if (/^https?:\/\//i.test(path)) return path;
  const raw = import.meta.env.VITE_API_BASE_URL ?? "";
  const base = raw.replace(/\/+$/, "");
  const normalisedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalisedPath}`;
}

/**
 * Builds HTTP headers for a request, setting default Accept and Content-Type headers
 * and injecting tenant information when available.
 *
 * @param init - Initial HeadersInit object or undefined to start from scratch.
 * @param tenantSlug - Tenant identifier to include in the X-UMS-Tenant header.
 * @param hasJsonBody - Whether the request has a JSON body; sets Content-Type to application/json if true.
 * @returns Constructed Headers object with appropriate defaults and tenant header.
 */
const buildHeaders = (
  init: HeadersInit | undefined,
  tenantSlug: string,
  hasJsonBody: boolean,
): Headers => {
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

/**
 * Represents an error that occurs when parsing JSON text fails.
 */
class JsonParseError extends Error {
  readonly name = "JsonParseError";
  /**
   * Constructs a new JsonParseError.
   *
   * @param message - The error message.
   * @param rawText - The raw JSON text that caused the parse error.
   */
  constructor(
    message: string,
    public readonly rawText: string,
  ) {
    super(message);
  }
}

/**
 * Parses the body of an HTTP response.
 *
 * @param res The HTTP response to parse.
 * @param options Options for parsing the response body.
 * @returns A promise resolving to the parsed JSON object, raw text, or undefined.
 */
async function parseBody(
  res: Response,
  options: ParseBodyOptions = {},
): Promise<unknown> {
  const noContentStatuses = new Set([204, 205, 304]);
  if (noContentStatuses.has(res.status)) {
    return undefined;
  }
  const contentType = res.headers.get("Content-Type") ?? "";
  const text = await res.text();
  if (!contentType.includes("application/json")) {
    return text;
  }
  if (text.length === 0) {
    return undefined;
  }
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

/**
 * Prepares a RequestInit object by attaching the body as JSON or raw and flags if it's JSON.
 *
 * @param body The request body, which can be undefined, FormData, or a JSON-serializable object.
 * @param init Optional RequestInit to merge in additional request options.
 * @returns A RequestInit object including the serialized body and a bodyIsJson flag.
 */
export function withJsonBody<T>(body: T, init: RequestInit = {}): RequestOptions {
  if (body === undefined) {
    return { ...init, body: body as BodyInit, bodyIsJson: false };
  }
  const rawTypes = [FormData, URLSearchParams, Blob, ArrayBuffer];
  const isRaw = typeof body === "string" || rawTypes.some(t => body instanceof t) || ArrayBuffer.isView(body);
  if (isRaw) {
    return { ...init, body: body as BodyInit, bodyIsJson: false };
  }
  return { ...init, body: JSON.stringify(body), bodyIsJson: true };
}

/**
 * Creates and returns an API client hook with a request method for making HTTP requests.
 *
 * @returns An object containing the request function to perform HTTP requests.
 */
export function useApiClient() {
  const { tenantSlug } = useTenant();

  return useMemo(() => {
    /**
     * Performs an HTTP request using the API client.
     *
     * @param method The HTTP method to use (e.g., "GET", "POST").
     * @param path The request path or URL.
     * @param init Request initialization options, including headers and body.
     * @returns A promise resolving to the response data of type T.
     */
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
