const DEFAULT_BACKEND_TARGET = "http://127.0.0.1:8000";
export const TRUSTED_BACKEND_ORIGINS_ENV = "UMS_DEV_TRUSTED_BACKEND_ORIGINS";

// ============================================================================
// Purpose: Declare every tenant-scoped backend prefix the dashboard may call.
// Database/ORM: None.
// Standards: This is a Node-side Vite configuration contract; the browser must
//   never receive the trusted gateway secret or make authorization decisions.
// Blast Radius: Frontend development proxy route selection only.
// Connections:
//   - File: frontend/vite.config.ts -> consumes this list for Vite proxy setup.
//   - File: frontend/tests/devProxyRoutes.test.ts -> derives request coverage.
//   - File: backend/ums_smart_revenue/api/dependencies.py -> receives the
//     trusted-principal headers on the selected routes.
// ============================================================================
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

const GATEWAY_HEADER_SOURCES: [header: string, envVar: string, fallback: string][] = [
  ["X-User-ID", "VITE_DEV_GATEWAY_USER_ID", "00000000-0000-0000-0000-0000000000aa"],
  ["X-User-Email", "VITE_DEV_GATEWAY_USER_EMAIL", "dev@ums.local"],
  ["X-Role", "VITE_DEV_GATEWAY_ROLE", "assistant_analyst"],
  ["X-Scope-Type", "VITE_DEV_GATEWAY_SCOPE_TYPE", "global"],
  ["X-UMS-Trusted-Gateway-Token", "UMS_TRUSTED_GATEWAY_TOKEN", ""],
  ["X-UMS-Tenant", "VITE_DEV_GATEWAY_TENANT_SLUG", "ums"],
  ["X-Scope-ID", "VITE_DEV_GATEWAY_SCOPE_ID", ""],
];

// ============================================================================
// Purpose: Define the complete trusted-header namespace that must never pass
//   through from an untrusted browser request.
// Database/ORM: None.
// Standards: Match names case-insensitively, cover known fields explicitly,
//   and reserve the X-UMS-* namespace for gateway-controlled claims.
// Blast Radius: Frontend development proxy authentication boundary only.
// Connections:
//   - File: backend/ums_smart_revenue/api/dependencies.py -> consumes the
//     gateway identity and token after this scrub.
//   - File: frontend/tests/devProxyRoutes.test.ts -> attacker-header guard.
// ============================================================================
const TRUSTED_GATEWAY_HEADER_NAMES = [
  "X-User-ID",
  "X-User-Email",
  "X-Role",
  "X-Permissions",
  "X-Company-ID",
  "X-Scope-Type",
  "X-Scope-ID",
  "X-UMS-Trusted-Gateway-Token",
  "X-UMS-Tenant",
] as const;

const TRUSTED_GATEWAY_HEADER_PATTERNS: readonly RegExp[] = [
  /^x-user-/iu,
  /^x-role$/iu,
  /^x-permissions$/iu,
  /^x-company-id$/iu,
  /^x-scope-/iu,
  /^x-ums-/iu,
];

// ============================================================================
// Purpose: Resolve the trusted-principal header set once per Vite config load.
// Database/ORM: None.
// Standards: Blank optional values are omitted; the gateway token is read only
//   from the server-side, non-VITE_ environment namespace.
// Blast Radius: Frontend development proxy authentication headers only.
// Connections:
//   - File: backend/ums_smart_revenue/api/dependencies.py -> required header
//     contract for headers-mode authentication.
//   - File: frontend/tests/devProxyRoutes.test.ts -> complete-header guard.
// ============================================================================
export const resolveGatewayHeaders = (env: Record<string, string>): [string, string][] => {
  const resolved = GATEWAY_HEADER_SOURCES.map(
    ([header, envVar, fallback]) => [header, env[envVar] ?? fallback] as [string, string],
  );
  return resolved.filter(([, value]) => value !== "");
};

type ProxyRequest = {
  getHeaderNames: () => string[];
  removeHeader: (header: string) => void;
  setHeader: (header: string, value: string) => void;
};

type IncomingProxyRequest = {
  headers: Record<string, string | string[] | undefined>;
};

const isTrustedGatewayHeader = (header: string): boolean =>
  TRUSTED_GATEWAY_HEADER_PATTERNS.some((pattern) => pattern.test(header));

// ============================================================================
// Purpose: Remove caller-supplied trusted identity headers before injecting the
//   configured development principal onto an outbound proxy request.
// Database/ORM: None.
// Standards: Scrub known names and every case-insensitive matching name that
//   http-proxy copied from the browser; blank configured optionals stay absent.
// Blast Radius: Frontend development proxy authentication boundary only.
// Connections:
//   - File: frontend/vite.config.ts -> invokes this through proxy construction.
//   - File: backend/ums_smart_revenue/api/dependencies.py -> consumes only the
//     resulting trusted header set.
// ============================================================================
const clearInboundTrustedGatewayHeaders = (proxyReq: ProxyRequest): void => {
  const candidates = new Set<string>([
    ...TRUSTED_GATEWAY_HEADER_NAMES,
    ...proxyReq.getHeaderNames(),
  ]);
  for (const header of candidates) {
    if (isTrustedGatewayHeader(header)) {
      proxyReq.removeHeader(header);
    }
  }
};

// ============================================================================
// Purpose: Scrub browser-controlled trusted headers on the incoming request
//   before Vite/http-proxy copies req.headers into setupOutgoing options.
// Database/ORM: None.
// Standards: Mutate only the Node request header map, remove the complete
//   trusted namespace first, and inject only the validated configured values.
// Blast Radius: Frontend development proxy authentication boundary only.
// Connections:
//   - File: frontend/vite.config.ts -> Vite proxy bypass boundary invokes this.
//   - File: backend/ums_smart_revenue/api/dependencies.py -> consumes the
//     resulting trusted header set.
// ============================================================================
const applyTrustedGatewayHeaders = (
  request: IncomingProxyRequest,
  gatewayHeaders: readonly [string, string][],
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

const isLoopbackHostname = (hostname: string): boolean => {
  const normalized = hostname.replace(/^\[|\]$/gu, "").replace(/\.$/u, "").toLowerCase();
  if (normalized === "localhost" || normalized === "::1") {
    return true;
  }
  const octets = normalized.split(".");
  return (
    octets.length === 4 &&
    octets[0] === "127" &&
    octets.every((octet) => /^\d{1,3}$/u.test(octet) && Number(octet) <= 255)
  );
};

// ============================================================================
// Purpose: Decide whether a backend origin is safe to receive the trusted
//   gateway token from the development proxy.
// Database/ORM: None.
// Standards: Verified loopback origins may use HTTP; non-loopback origins must
//   use HTTPS and match an explicit exact-origin allowlist. Reject malformed
//   URLs and URL credentials so a typo cannot silently redirect the secret.
// Blast Radius: Frontend development startup only; production gateways are
//   outside Vite and are not changed.
// Connections:
//   - File: README.md -> documents VITE_DEV_BACKEND_URL and the explicit
//     UMS_DEV_TRUSTED_BACKEND_ORIGINS escape hatch.
//   - File: frontend/tests/devProxyRoutes.test.ts -> target trust tests.
// ============================================================================
export const resolveDevBackendTarget = (
  backendTarget: string,
  trustedOrigins: readonly string[] = [],
): string => {
  let target: URL;
  try {
    target = new URL(backendTarget.trim());
  } catch {
    throw new Error("VITE_DEV_BACKEND_URL must be an absolute http(s) URL");
  }

  if (!(target.protocol === "http:" || target.protocol === "https:")) {
    throw new Error("VITE_DEV_BACKEND_URL must use http or https");
  }
  if (target.username || target.password) {
    throw new Error("VITE_DEV_BACKEND_URL must not include URL credentials");
  }
  const targetIsLoopback = isLoopbackHostname(target.hostname);
  if (targetIsLoopback) {
    return target.href.replace(/\/$/u, "");
  }
  if (target.protocol !== "https:") {
    throw new Error("Non-loopback VITE_DEV_BACKEND_URL targets must use HTTPS");
  }

  const trustedOriginSet = new Set(
    trustedOrigins
      .map((origin) => origin.trim())
      .filter(Boolean)
      .map((origin) => {
        let parsed: URL;
        try {
          parsed = new URL(origin);
        } catch {
          throw new Error(`${TRUSTED_BACKEND_ORIGINS_ENV} contains an invalid origin`);
        }
        const parsedIsLoopback = isLoopbackHostname(parsed.hostname);
        if (
          !(parsed.protocol === "http:" || parsed.protocol === "https:") ||
          (!parsedIsLoopback && parsed.protocol !== "https:") ||
          parsed.username ||
          parsed.password ||
          parsed.pathname !== "/" ||
          parsed.search ||
          parsed.hash
        ) {
          throw new Error(
            `${TRUSTED_BACKEND_ORIGINS_ENV} contains an invalid origin; ` +
              "non-loopback origins must use HTTPS",
          );
        }
        return parsed.origin;
      }),
  );
  if (!trustedOriginSet.has(target.origin)) {
    throw new Error(
      "VITE_DEV_BACKEND_URL must target loopback or an origin listed in " +
        `${TRUSTED_BACKEND_ORIGINS_ENV}`,
    );
  }
  return target.href.replace(/\/$/u, "");
};

// ============================================================================
// Purpose: Build one Vite proxy entry for every tenant-scoped route and inject
//   the resolved trusted-principal headers on each outbound request.
// Database/ORM: None; this is Node-side Vite configuration.
// Standards: Validate the backend target before creating any entry, scrub and
//   inject before http-proxy copies req.headers, retain proxyReq defense-in-depth,
//   keep the gateway token server-side, preserve changeOrigin, and never make
//   authorization decisions in the frontend proxy.
// Blast Radius: Frontend development proxy only; no production bundle or
//   database state is affected.
// Connections:
//   - File: backend/ums_smart_revenue/api/dependencies.py -> receives the
//     trusted-principal header contract.
//   - File: frontend/tests/devProxyRoutes.test.ts -> route/header guard.
// ============================================================================
export const buildTenantScopedProxy = (
  routes: readonly string[],
  backendTarget: string,
  gatewayHeaders: [string, string][],
  trustedOrigins: readonly string[] = [],
): Record<string, object> => {
  const resolvedBackendTarget = resolveDevBackendTarget(backendTarget, trustedOrigins);
  return Object.fromEntries(
    routes.map((route) => [
      route,
      {
        target: resolvedBackendTarget,
        changeOrigin: true,
        bypass(request: IncomingProxyRequest) {
          applyTrustedGatewayHeaders(request, gatewayHeaders);
        },
        configure(proxy: { on: (event: string, fn: (req: ProxyRequest) => void) => void }) {
          proxy.on("proxyReq", (proxyReq: ProxyRequest) => {
            clearInboundTrustedGatewayHeaders(proxyReq);
            for (const [header, value] of gatewayHeaders) {
              proxyReq.setHeader(header, value);
            }
          });
        },
      },
    ]),
  );
};

export { DEFAULT_BACKEND_TARGET };
