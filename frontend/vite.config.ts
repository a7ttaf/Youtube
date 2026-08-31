import path from "node:path";
import { fileURLToPath } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv, type Plugin } from "vite";

import {
  DEFAULT_BACKEND_TARGET,
  TENANT_SCOPED_ROUTES,
  TRUSTED_BACKEND_ORIGINS_ENV,
  buildTenantScopedProxy,
  parseTrustedBackendOrigins,
  resolveDevBackendTarget,
  resolveGatewayHeaders,
} from "./devProxy";

export {
  DEFAULT_BACKEND_TARGET,
  TENANT_SCOPED_ROUTES,
  TRUSTED_BACKEND_ORIGINS_ENV,
  TRUSTED_GATEWAY_HEADERS,
  buildTenantScopedProxy,
  isSafeRouteUrl,
  parseTrustedBackendOrigins,
  proxyContextForRoute,
  resolveDevBackendTarget,
  resolveGatewayHeaders,
} from "./devProxy";

// Repo root is one level above this file (frontend/vite.config.ts -> ..).
// Resolving relative to import.meta.url (not process.cwd()) makes the env
// lookup deterministic regardless of where the dev command is launched
// (`cd frontend && npm run dev` vs `npm --prefix frontend run dev` from root):
// without this, loadEnv resolved to frontend/.env and silently skipped the
// repo-root .env where UMS_TRUSTED_GATEWAY_TOKEN and the VITE_DEV_* dev
// defaults are documented, leaving the dev proxy 401-ing.
const FRONTEND_ROOT = path.dirname(fileURLToPath(import.meta.url));
const REPO_ROOT = path.resolve(FRONTEND_ROOT, "..");

export const shouldEnableDevGateway = (
  command: string,
  mode: string,
  isPreview = false,
): boolean => command === "serve" && mode === "development" && !isPreview;

/** True only for explicit loopback bind hosts; wildcard/network binds are unsafe. */
export const isLoopbackDevServerHost = (host: string | boolean | undefined): boolean => {
  if (typeof host !== "string") {
    return false;
  }
  const normalized = host.trim().replace(/^\[|\]$/gu, "").replace(/\.$/u, "")
    .toLowerCase();
  if (normalized === "localhost" || normalized === "::1") {
    return true;
  }
  const octets = normalized.split(".");
  return octets.length === 4 &&
    octets[0] === "127" &&
    octets.every((octet) => /^\d{1,3}$/u.test(octet) && Number(octet) <= 255);
};

// ============================================================================
// Purpose: Recheck Vite's final resolved bind host after CLI/inline overrides.
// Database/ORM: None.
// Standards: Fail closed before listen whenever the trusted dev proxy would be
//   reachable through a wildcard or non-loopback interface.
// Blast Radius: Development server startup and trusted-header proxy exposure.
// Connections:
//   - File: frontend/devProxy.ts -> proxy injects trusted gateway claims.
//   - File: frontend/tests/devProxySecurity.test.ts -> real override probes.
// ============================================================================
const resolvedDevGatewayHostGuard = (): Plugin => {
  const assertLoopback = (host: string | boolean | undefined): void => {
    if (!isLoopbackDevServerHost(host)) {
      throw new Error(
        `trusted development gateway requires an explicit loopback Vite host; received ${String(host)}`,
      );
    }
  };
  return {
    name: "ums-dev-gateway-loopback-host-guard",
    configResolved(config) {
      // FIX: Inline/CLI host overrides win after the file's server.host value;
      // validate the resolved value before an externally reachable listener exists.
      assertLoopback(config.server.host);
    },
    configureServer(server) {
      assertLoopback(server.config.server.host);
    },
  };
};

// ============================================================================
// Purpose: Resolve the complete development proxy only for Vite's development
//   serve command; build and preview modes receive no trusted-header proxy.
// Database/ORM: None.
// Standards: Fail closed on blank token/scope configuration and validate the
//   backend target before any request can carry a trusted header.
// Blast Radius: Frontend development server only.
// Connections:
//   - File: frontend/devProxy.ts -> owns boundary validation and proxy hooks.
//   - File: frontend/tests/devProxySecurity.test.ts -> proves activation modes.
// ============================================================================
export const resolveDevGatewayProxy = (
  command: string,
  mode: string,
  env: Record<string, string>,
  isPreview = false,
) => {
  if (!shouldEnableDevGateway(command, mode, isPreview)) {
    return undefined;
  }
  const trustedOrigins = parseTrustedBackendOrigins(
    env[TRUSTED_BACKEND_ORIGINS_ENV] ?? "",
  );
  const backendTarget = resolveDevBackendTarget(
    env.VITE_DEV_BACKEND_URL ?? DEFAULT_BACKEND_TARGET,
    trustedOrigins,
  );
  const gatewayHeaders = resolveGatewayHeaders(env);
  return buildTenantScopedProxy(
    TENANT_SCOPED_ROUTES,
    backendTarget,
    gatewayHeaders,
    trustedOrigins,
  );
};

export default defineConfig(({ command, mode, isPreview }) => {
  // ============================================================================
  // Purpose: Load env from the repository root in Node only. VITE_-prefixed
  //          names are EXPOSED to the client bundle via import.meta.env, so
  //          secrets must use non-VITE_ names (UMS_TRUSTED_GATEWAY_TOKEN).
  //          Non-secret dev defaults (user id/email/role/scope_type) keep the
  //          VITE_DEV_ prefix only because they are intentionally non-secret.
  // Database/ORM: None.
  // Standards: Server-only secret read from process env; never embedded in code.
  //            envDir is pinned to REPO_ROOT so root `.env.example` documents
  //            the canonical location and `cd frontend && npm run dev` cannot
  //            silently load a different env file.
  // Blast Radius: Frontend dev proxy only — no production bundle exposure.
  // Connections:
  //   - File: frontend/devProxy.ts -> validates routes, target, origin, and headers.
  //   - File: .env.example -> documents the development gateway inputs.
  // ============================================================================
  const devProxy = shouldEnableDevGateway(command, mode, isPreview)
    ? resolveDevGatewayProxy(command, mode, loadEnv(mode, REPO_ROOT, ""), isPreview)
    : undefined;

  return {
    plugins: [
      ...(devProxy ? [resolvedDevGatewayHostGuard()] : []),
      react(),
      tailwindcss(),
    ],
    envDir: REPO_ROOT,
    resolve: {
      alias: {
        "@": path.resolve(FRONTEND_ROOT, "src"),
      },
    },
    server: {
      host: "127.0.0.1",
      ...(devProxy ? { proxy: devProxy } : {}),
    },
  };
});
