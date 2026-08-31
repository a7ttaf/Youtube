import type { IncomingMessage, ServerResponse } from "node:http";

import type { ProxyOptions } from "vite";

export const DEFAULT_BACKEND_TARGET = "http://127.0.0.1:8000";
export const TRUSTED_BACKEND_ORIGINS_ENV = "UMS_DEV_TRUSTED_BACKEND_ORIGINS";

// Every browser API prefix that may receive the Node-side trusted principal in
// local development. Adding a prefix expands a trust boundary and therefore
// requires a matching test and operator-doc update.
export const TENANT_SCOPED_ROUTES = [
  "/tenants",
  "/session",
  "/revenue",
  "/finance-close",
  "/exports",
  "/connectors",
  "/adsense",
  "/channels",
  "/org-units",
  "/groups",
  "/audit",
  "/users",
] as const;

type GatewayHeaderSource = readonly [
  header: string,
  envVar: string,
  fallback: string,
];

const GATEWAY_HEADER_SOURCES: readonly GatewayHeaderSource[] = [
  ["X-User-ID", "VITE_DEV_GATEWAY_USER_ID", "00000000-0000-0000-0000-0000000000aa"],
  ["X-User-Email", "VITE_DEV_GATEWAY_USER_EMAIL", "dev@ums.local"],
  ["X-Role", "VITE_DEV_GATEWAY_ROLE", "assistant_analyst"],
  ["X-Scope-Type", "VITE_DEV_GATEWAY_SCOPE_TYPE", "global"],
  ["X-UMS-Trusted-Gateway-Token", "UMS_TRUSTED_GATEWAY_TOKEN", ""],
  ["X-UMS-Tenant", "VITE_DEV_GATEWAY_TENANT_SLUG", "ums"],
  ["X-Scope-ID", "VITE_DEV_GATEWAY_SCOPE_ID", ""],
] as const;

// ============================================================================
// Purpose: Define every known gateway claim plus reserved header namespaces
//   that browser requests may never control.
// Database/ORM: None.
// Standards: Match case-insensitively; explicit names remove absent optional
//   claims, while namespace patterns protect future claims before list updates.
// Blast Radius: Development gateway authentication headers only.
// Connections:
//   - File: backend/ums_smart_revenue/api/dependencies.py -> consumes claims.
//   - File: frontend/tests/devProxySecurity.test.ts -> broad namespace probes.
// ============================================================================
export const TRUSTED_GATEWAY_HEADERS = [
  ...GATEWAY_HEADER_SOURCES.map(([header]) => header),
  "X-Permissions",
  "X-Company-ID",
] as const;

const TRUSTED_GATEWAY_HEADER_PATTERNS: readonly RegExp[] = [
  /^x-user-/iu,
  /^x-role$/iu,
  /^x-permissions$/iu,
  /^x-company-id$/iu,
  /^x-scope-/iu,
  /^x-ums-/iu,
];

const isTrustedGatewayHeader = (header: string): boolean =>
  TRUSTED_GATEWAY_HEADER_PATTERNS.some((pattern) => pattern.test(header));

export type GatewayHeader = readonly [header: string, value: string];

const requiredEnvValue = (
  env: Record<string, string>,
  envVar: string,
  fallback: string,
): string => {
  const value = (env[envVar] ?? fallback).trim();
  if (!value) {
    throw new Error(`[vite] ${envVar} must be non-blank for the development gateway proxy`);
  }
  return value;
};

// ============================================================================
// Purpose: Resolve the complete trusted-principal header set for the local
//   development gateway and reject incomplete or contradictory scope config.
// Database/ORM: None.
// Standards: Fail closed before Vite listens; trim configured values; omit
//   X-Scope-ID only for a global principal, where the backend requires it absent.
// Blast Radius: Authorization headers on proxied development requests only.
// Connections:
//   - File: backend/ums_smart_revenue/api/dependencies.py -> consumes the six
//     required headers and conditionally consumes X-Scope-ID.
//   - File: frontend/vite.config.ts -> activates this only for dev serve mode.
// ============================================================================
export const resolveGatewayHeaders = (
  env: Record<string, string>,
): GatewayHeader[] => {
  const resolved = GATEWAY_HEADER_SOURCES.slice(0, -1).map(
    ([header, envVar, fallback]) =>
      [header, requiredEnvValue(env, envVar, fallback)] as const,
  );
  const scopeTypeIndex = resolved.findIndex(([header]) => header === "X-Scope-Type");
  const scopeType = resolved[scopeTypeIndex]?.[1].toLowerCase();
  if (!scopeType) {
    throw new Error("[vite] VITE_DEV_GATEWAY_SCOPE_TYPE must be non-blank");
  }
  resolved[scopeTypeIndex] = ["X-Scope-Type", scopeType];

  const scopeId = (env.VITE_DEV_GATEWAY_SCOPE_ID ?? "").trim();
  if (scopeType === "global") {
    if (scopeId) {
      throw new Error(
        "[vite] VITE_DEV_GATEWAY_SCOPE_ID must be blank when VITE_DEV_GATEWAY_SCOPE_TYPE is global",
      );
    }
    return resolved;
  }
  if (!scopeId) {
    throw new Error(
      `[vite] VITE_DEV_GATEWAY_SCOPE_ID must be non-blank for scope type ${scopeType}`,
    );
  }
  return [...resolved, ["X-Scope-ID", scopeId]];
};

const parseOrigin = (value: string, label: string): URL => {
  let parsed: URL;
  try {
    parsed = new URL(value);
  } catch {
    throw new Error(`[vite] ${label} must be an absolute http(s) origin`);
  }
  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    throw new Error(`[vite] ${label} must use http or https`);
  }
  if (parsed.username || parsed.password) {
    throw new Error(`[vite] ${label} must not contain credentials`);
  }
  if (parsed.pathname !== "/" || parsed.search || parsed.hash) {
    throw new Error(`[vite] ${label} must be an origin without a path, query, or fragment`);
  }
  return parsed;
};

const isLoopbackHostname = (hostname: string): boolean =>
  hostname === "localhost" || hostname === "127.0.0.1" || hostname === "[::1]";

const normalizeTrustedBackendOrigin = (rawValue: string): string => {
  const origin = parseOrigin(rawValue, TRUSTED_BACKEND_ORIGINS_ENV);
  if (!isLoopbackHostname(origin.hostname) && origin.protocol !== "https:") {
    throw new Error(
      `[vite] ${TRUSTED_BACKEND_ORIGINS_ENV} non-loopback origins must use https`,
    );
  }
  return origin.origin;
};

export const parseTrustedBackendOrigins = (rawValue: string): string[] =>
  rawValue
    .split(",")
    .map((value) => value.trim())
    .filter(Boolean)
    .map(normalizeTrustedBackendOrigin);

// ============================================================================
// Purpose: Prevent a mistyped or attacker-controlled backend target from
//   receiving the local trusted-gateway token.
// Database/ORM: None.
// Standards: Loopback is trusted by default; every non-loopback target needs
//   HTTPS plus an exact Node-only origin allowlist entry and may not embed
//   credentials.
// Blast Radius: Development gateway target selection only.
// Connections:
//   - File: README.md -> documents UMS_DEV_TRUSTED_BACKEND_ORIGINS.
//   - File: frontend/vite.config.ts -> validates before proxy construction.
// ============================================================================
export const resolveDevBackendTarget = (
  rawTarget: string,
  trustedOrigins: readonly string[] = [],
): string => {
  const target = parseOrigin(rawTarget.trim(), "VITE_DEV_BACKEND_URL");
  const targetIsLoopback = isLoopbackHostname(target.hostname);
  if (!targetIsLoopback && target.protocol !== "https:") {
    throw new Error("[vite] non-loopback VITE_DEV_BACKEND_URL targets must use https");
  }
  const normalizedTrustedOrigins = trustedOrigins.map(normalizeTrustedBackendOrigin);
  if (
    !targetIsLoopback &&
    !normalizedTrustedOrigins.includes(target.origin)
  ) {
    throw new Error(
      `[vite] refusing non-loopback VITE_DEV_BACKEND_URL ${target.origin}; ` +
        `add its exact origin to ${TRUSTED_BACKEND_ORIGINS_ENV} only if it may receive the gateway token`,
    );
  }
  return target.origin;
};

const escapeRegex = (value: string): string =>
  value.replace(/[.*+?^${}()|[\]\\]/gu, "\\$&");

export const proxyContextForRoute = (route: string): string =>
  `^${escapeRegex(route)}(?:/|\\?|$)`;

const pathPart = (requestUrl: string): string => requestUrl.split("?", 1)[0] ?? "";

const hasUnsafeSegments = (value: string): boolean => {
  if (
    !value.startsWith("/") ||
    value.startsWith("//") ||
    value.includes("\\") ||
    value.includes("\0") ||
    value.slice(1).includes("//") ||
    /%(?:2f|5c)/iu.test(value)
  ) {
    return true;
  }
  return value.split("/").some((segment) => segment === "." || segment === "..");
};

// ============================================================================
// Purpose: Validate the raw request path against one exact allowlisted segment
//   while rejecting malformed, separator-changing, and traversal encodings.
// Database/ORM: None.
// Standards: Decode repeatedly so nested encodings cannot become dangerous at
//   a later intermediary; a still-changing value after five layers fails closed.
// Blast Radius: Development proxy routing only.
// Connections:
//   - File: frontend/tests/devProxySecurity.test.ts -> real encoded-path cases.
//   - File: frontend/vite.config.ts -> supplies the allowlisted route set.
// ============================================================================
export const isSafeRouteUrl = (requestUrl: string, route: string): boolean => {
  let candidate = pathPart(requestUrl);
  for (let decodeDepth = 0; decodeDepth < 5; decodeDepth += 1) {
    if (hasUnsafeSegments(candidate)) {
      return false;
    }
    const firstSegment = `/${candidate.slice(1).split("/", 1)[0] ?? ""}`;
    if (firstSegment !== route) {
      return false;
    }
    let decoded: string;
    try {
      decoded = decodeURIComponent(candidate);
    } catch {
      return false;
    }
    if (decoded === candidate) {
      return true;
    }
    candidate = decoded;
  }
  return false;
};

const singleHeader = (value: string | string[] | undefined): string | undefined =>
  Array.isArray(value) ? (value.length === 1 ? value[0] : undefined) : value;

// ============================================================================
// Purpose: Reject browser requests whose Origin/Fetch Metadata does not match
//   the loopback Vite request Host before trusted headers are attached.
// Database/ORM: None.
// Standards: CLI requests may omit Origin; a present Origin must be a canonical
//   same-origin http(s) value and cross-site Sec-Fetch-Site always fails closed.
// Blast Radius: Development proxy authorization boundary only.
// Connections:
//   - File: frontend/tests/devProxySecurity.test.ts -> Host/origin counterexamples.
//   - File: frontend/vite.config.ts -> binds the dev server to 127.0.0.1.
// ============================================================================
const requestUsesTrustedOrigin = (request: IncomingMessage): boolean => {
  const fetchSite = singleHeader(request.headers["sec-fetch-site"]);
  if (fetchSite && fetchSite !== "same-origin" && fetchSite !== "none") {
    return false;
  }
  const origin = singleHeader(request.headers.origin);
  if (!origin) {
    return true;
  }
  const host = singleHeader(request.headers.host)?.trim().toLowerCase();
  if (!host) {
    return false;
  }
  let parsed: URL;
  try {
    parsed = new URL(origin);
  } catch {
    return false;
  }
  const requestProtocol =
    "encrypted" in request.socket && request.socket.encrypted ? "https:" : "http:";
  return (
    parsed.protocol === requestProtocol &&
    !parsed.username &&
    !parsed.password &&
    parsed.pathname === "/" &&
    !parsed.search &&
    !parsed.hash &&
    parsed.host.toLowerCase() === host
  );
};

const rejectRequest = (
  response: ServerResponse,
  statusCode: number,
  message: string,
): string => {
  response.statusCode = statusCode;
  response.setHeader("Content-Type", "text/plain; charset=utf-8");
  response.end(message);
  return "/";
};

// ============================================================================
// Purpose: Replace browser-controlled gateway claims before http-proxy copies
//   the incoming header map into an outbound request.
// Database/ORM: None.
// Standards: Reserve the complete trusted namespaces, preserve unrelated
//   headers, and inject only values validated from Node-side configuration.
// Blast Radius: Development authorization headers on proxied requests only.
// Connections:
//   - File: frontend/tests/devProxySecurity.test.ts -> exercises normal and
//     Expect: 100-continue flows through a real Vite proxy.
//   - File: backend/ums_smart_revenue/api/dependencies.py -> trusted consumer.
// ============================================================================
const applyTrustedGatewayHeaders = (
  request: IncomingMessage,
  gatewayHeaders: readonly GatewayHeader[],
): void => {
  for (const header of Object.keys(request.headers)) {
    if (isTrustedGatewayHeader(header)) {
      delete request.headers[header];
    }
  }
  for (const [header, value] of gatewayHeaders) {
    request.headers[header.toLowerCase()] = value;
  }
};

// ============================================================================
// Purpose: Build exact-segment Vite proxy entries and replace every caller
//   supplied trusted identity, role, scope, tenant, and token header.
// Database/ORM: None.
// Standards: Reject encoded path confusion and cross-origin browser requests;
//   scrub and inject claims before http-proxy copies incoming headers, retaining
//   proxyReq replacement as defense in depth. Preserve the browser Origin while
//   changeOrigin rewrites Host to the validated backend.
// Blast Radius: Development authorization boundary; no production activation.
// Connections:
//   - File: frontend/tests/devProxySecurity.test.ts -> drives a real Vite proxy.
//   - File: backend/ums_smart_revenue/api/dependencies.py -> trusted consumer.
// ============================================================================
export const buildTenantScopedProxy = (
  routes: readonly string[],
  rawBackendTarget: string,
  gatewayHeaders: readonly GatewayHeader[],
  trustedBackendOrigins: readonly string[] = [],
): Record<string, ProxyOptions> => {
  const backendTarget = resolveDevBackendTarget(rawBackendTarget, trustedBackendOrigins);
  return Object.fromEntries(
    routes.map((route) => [
      proxyContextForRoute(route),
      {
        target: backendTarget,
        changeOrigin: true,
        bypass(request: IncomingMessage, response: ServerResponse | undefined) {
          if (!response) {
            return false;
          }
          const requestUrl = request.url ?? "";
          if (!isSafeRouteUrl(requestUrl, route)) {
            return rejectRequest(response, 400, "Invalid development gateway path");
          }
          if (!requestUsesTrustedOrigin(request)) {
            return rejectRequest(response, 403, "Untrusted development gateway origin");
          }
          // FIX: proxyReq is not emitted on every Expect: 100-continue path.
          // Replace trusted claims here before http-proxy copies req.headers.
          applyTrustedGatewayHeaders(request, gatewayHeaders);
          return undefined;
        },
        configure(proxy) {
          proxy.on("proxyReq", (proxyRequest) => {
            // FIX: Deleting the complete trusted set before injection prevents
            // optional headers (especially X-Scope-ID for global identities)
            // from surviving from the caller when no replacement is configured.
            const candidateHeaders = new Set([
              ...TRUSTED_GATEWAY_HEADERS,
              ...proxyRequest.getHeaderNames(),
            ]);
            for (const header of candidateHeaders) {
              if (isTrustedGatewayHeader(header)) {
                proxyRequest.removeHeader(header);
              }
            }
            for (const [header, value] of gatewayHeaders) {
              proxyRequest.setHeader(header, value);
            }
          });
        },
      } satisfies ProxyOptions,
    ]),
  );
};
