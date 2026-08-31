// frontend/src/lib/api/client.ts
import { useMemo } from "react";

import { useTenant } from "@/contexts/TenantContext";

// The JSON media type this client both requests and detects. Named once so the
// Accept header, the Content-Type it sets on JSON bodies, and the response
// content-type sniff in parseBody can never drift apart.
const JSON_MEDIA_TYPE = "application/json";

/**
 * Typed error thrown by the API client for any non-2xx response (and for a 2xx
 * response whose declared-JSON body fails to parse). Carries the HTTP status,
 * the parsed (or raw-text) response body, and the resolved request URL so
 * callers can branch on `instanceof ApiError` + status (e.g. 403 scope guards).
 */
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

/**
 * Resolve a request path against the configured API origin.
 *
 * An already-absolute http(s) URL is returned untouched. Otherwise the path is
 * prefixed with VITE_API_BASE_URL (trailing slashes stripped); when no base is
 * configured this returns the original relative path unchanged, so same-origin
 * deployments keep byte-identical relative URLs. Exported so non-JSON callers
 * (e.g. binary download flows) can target the same API origin as JSON requests
 * instead of hard-coding a frontend-relative URL. This is URL normalization
 * only: a cross-origin value does not establish CORS or trusted-gateway auth.
 */
export const resolveUrl = (path: string): string => {
  if (/^https?:\/\//i.test(path)) return path;
  const raw = import.meta.env.VITE_API_BASE_URL ?? "";
  const base = raw.replace(/\/+$/, "");
  const normalisedPath = path.startsWith("/") ? path : `/${path}`;
  return `${base}${normalisedPath}`;
};

/**
 * Purpose: Build the Headers for one API request — default Accept to
 *   application/json, set Content-Type only when the body is JSON-encoded, and
 *   resolve X-UMS-Tenant from the provider's tenant slug.
 * Database/ORM: None (frontend) — header construction only.
 * Standards: Typed HeadersInit boundary; no request is issued here and no error
 *   is swallowed. Tenant identity comes from the resolved slug argument only: a
 *   caller-supplied X-UMS-Tenant is overwritten when a slug is resolved and
 *   deleted when it is not — it is never merged through. This helper adds the
 *   tenant header only; trusted-gateway identity is supplied by the gateway.
 * Blast Radius: Authorization / tenancy — this is the single choke point where
 *   every request in this client acquires its tenant scope, so the overwrite-
 *   or-delete rule is what stops the browser bundle forging a tenant, and the empty
 *   slug case is what stops a hardcoded fallback pinning every principal to
 *   "ums" during the pre-hydration window (see the inline note below).
 * Connections: TenantContext (slug source), request<T> + getBlob (consumers),
 *   vite.config.ts (dev-proxy counterpart).
 *   - File: frontend/src/contexts/TenantContext.tsx -> useTenant supplies the
 *     resolved slug; an empty value means bootstrap is still in flight.
 *   - File: frontend/src/lib/api/client.ts -> sole consumers are request<T> and
 *     getBlob, so both the JSON and download paths share these headers.
 *   - File: frontend/vite.config.ts -> the dev proxy injects the trusted-gateway
 *     token and may override X-UMS-Tenant, mirroring the production gateway.
 */
const buildHeaders = (
  init: HeadersInit | undefined,
  tenantSlug: string,
  hasJsonBody: boolean,
): Headers => {
  const headers = new Headers(init);
  if (!headers.has("Accept")) {
    headers.set("Accept", JSON_MEDIA_TYPE);
  }
  if (hasJsonBody && !headers.has("Content-Type")) {
    headers.set("Content-Type", JSON_MEDIA_TYPE);
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
};

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
 * Internal error carrying the raw response text when a declared-JSON body
 * fails to parse under strict mode; request<T> converts it into an ApiError so
 * callers keep a single error boundary.
 */
class JsonParseError extends Error {
  readonly name = "JsonParseError";
  constructor(
    message: string,
    public readonly rawText: string,
  ) {
    super(message);
  }
}

/** True for statuses that never carry a body (204 No Content, 205 Reset Content, 304 Not Modified). */
const isBodylessStatus = (status: number): boolean =>
  status === 204 || status === 205 || status === 304;

/**
 * Parse response text that was declared as JSON. On malformed input: throws
 * JsonParseError (preserving the raw text) when `strictJson` is set, otherwise
 * returns the raw text so ApiError.body keeps the payload for diagnostics.
 */
const parseJsonText = (text: string, strictJson?: boolean): unknown => {
  try {
    return JSON.parse(text);
  } catch (parseError) {
    if (strictJson) {
      const reason = parseError instanceof Error ? parseError.message : "parse failed";
      throw new JsonParseError(reason, text);
    }
    return text;
  }
};

/**
 * Read a Response body: undefined for body-less statuses and empty JSON
 * bodies, raw text for non-JSON content types, parsed JSON otherwise.
 * `strictJson` controls whether a malformed declared-JSON body rejects
 * (success path) or falls back to raw text (error path) — see
 * ParseBodyOptions.
 */
const parseBody = async (
  res: Response,
  options: ParseBodyOptions = {},
): Promise<unknown> => {
  if (isBodylessStatus(res.status)) {
    return undefined;
  }
  const contentType = res.headers.get("Content-Type") ?? "";
  const text = await res.text();
  if (!contentType.includes(JSON_MEDIA_TYPE)) {
    return text;
  }
  if (text.length === 0) {
    return undefined;
  }
  return parseJsonText(text, options.strictJson);
};

type RequestOptions = RequestInit & { bodyIsJson?: boolean };

/** True when `body` is a binary payload fetch accepts natively (Blob, ArrayBuffer, or a typed-array view). */
const isBinaryBody = (body: unknown): boolean =>
  body instanceof Blob || body instanceof ArrayBuffer || ArrayBuffer.isView(body);

/**
 * Type guard for payloads fetch already accepts as BodyInit verbatim (string,
 * FormData, URLSearchParams, or binary); these must NOT be JSON.stringify'd.
 */
const isRawBodyInit = (body: unknown): body is BodyInit =>
  typeof body === "string" ||
  body instanceof FormData ||
  body instanceof URLSearchParams ||
  isBinaryBody(body);

/**
 * Normalise an optional request body into RequestOptions: an undefined body
 * leaves `init` untouched, a raw BodyInit payload passes through verbatim, and
 * anything else is JSON.stringify'd with `bodyIsJson: true` so buildHeaders
 * sets the JSON Content-Type.
 */
const withJsonBody = (
  body: unknown,
  init: RequestInit = {},
): RequestOptions => {
  if (body === undefined) return init;
  if (isRawBodyInit(body)) {
    return { ...init, body, bodyIsJson: false };
  }
  return { ...init, body: JSON.stringify(body), bodyIsJson: true };
};

/**
 * Purpose: The tenant-scoped API client hook — the single entry point every
 *   screen uses to reach the backend. Returns a memoised
 *   {get, getBlob, post, put, patch, delete} surface bound to the current
 *   tenant slug, where each request resolves against the configured API origin,
 *   carries the headers buildHeaders produces, and surfaces failures as ApiError.
 * Database/ORM: None (frontend) — no client-side persistence or cache; each
 *   call is a fetch against the backend's own guarded routes.
 * Standards: Typed boundary throughout — request<T> strict-parses success
 *   bodies so raw non-JSON text can never masquerade as a typed `T`, and no
 *   error is swallowed: every non-2xx and every malformed-JSON 2xx throws.
 *   The memo is keyed on tenantSlug so the bound tenant scope cannot go stale.
 *   The browser does not manufacture trusted-gateway identity; deployments
 *   provide that through the gateway/proxy.
 * Blast Radius: Authorization / tenancy — binding happens here, so a wrong
 *   tenantSlug would mis-scope every request the app makes. The fail-closed
 *   ApiError boundary is what lets callers branch on status (e.g. 403 scope
 *   guards) instead of silently rendering an error body as data. No finance
 *   value is computed or mutated client-side; the backend stays authoritative
 *   for every permission decision.
 * Connections: TenantContext (slug binding), buildHeaders + ApiError + getBlob
 *   (internals), vite.config.ts (dev-proxy token injection).
 *   - File: frontend/src/contexts/TenantContext.tsx -> useTenant supplies the
 *     slug this client is bound to.
 *   - File: frontend/src/lib/api/client.ts -> buildHeaders injects the tenant
 *     header; ApiError is the shared failure boundary; getBlob is the non-JSON
 *     download path. Trusted-gateway identity remains outside the browser.
 *   - File: frontend/vite.config.ts -> the dev proxy adds the trusted-gateway
 *     token in Node, so the browser bundle never holds the secret.
 */
export const useApiClient = () => {
  const { tenantSlug } = useTenant();

  return useMemo(() => {
    /**
     * Core JSON request path: resolves the URL, applies tenant/JSON headers,
     * throws ApiError on non-2xx, and strict-parses the success body so raw
     * non-JSON text can never masquerade as a typed `T`.
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

    /**
     * Purpose: GET a non-JSON (binary/text) response as a Blob, applying the
     *   SAME tenant-scoping header conventions the JSON client uses, and returning
     *   both the blob and the raw Headers so callers can read response headers
     *   (e.g. the audit CSV export's X-Truncated). Kept separate from request<T>
     *   because that path strict-parses JSON and would mangle a CSV body.
     * Database/ORM: None (frontend) — no client-side persistence; the named
     *   route is served entirely by the backend.
     * Standards: Same fail-closed ApiError boundary as request<T> — a non-2xx
     *   throws before any body reaches the caller. X-UMS-Tenant is injected by
     *   buildHeaders from the resolved tenant slug, never from the caller. No
     *   Accept override is forced, so the server may honor its own content type.
     * Blast Radius: Tenant-scoped artifact access — this is the only download
     *   path in the client, so the tenant header and non-2xx throw preserve the
     *   backend boundary. Trusted-gateway authentication is supplied by the
     *   deployment proxy, not this browser helper. Read-only: no finance math.
     * Connections: AuditLogPanelHeader.tsx + ExportsView.tsx (Blob callers),
     *   buildHeaders + ApiError (shared header/failure boundary).
     *   - File: frontend/src/components/srcc/views/AuditLogPanelHeader.tsx ->
     *     saves the CSV blob and reads the truncation header.
     *   - File: frontend/src/components/srcc/views/ExportsView.tsx -> saves
     *     generated export artifacts through a temporary object URL.
     *   - File: frontend/src/lib/api/client.ts -> buildHeaders supplies the
     *     tenant header; ApiError is the shared failure boundary.
     */
    async function getBlob(
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
};
