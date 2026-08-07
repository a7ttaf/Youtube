import path from "node:path";
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

// Every backend API prefix the dashboard calls in dev must be proxied with the
// same injected trusted-principal headers as /tenants, so the browser bundle
// never holds the gateway secret and the backend's
// current_principal_from_headers dependency succeeds. Mirrors the production
// reverse-proxy model where the trusted gateway injects principal identity.
const TENANT_SCOPED_ROUTES = [
  "/tenants",
  // /session/me hydrates the authenticated principal's capabilities and uses
  // the same trusted-gateway auth as /tenants/me, so it must be proxied with the
  // injected principal headers in dev or the bootstrap call would 401.
  "/session",
  "/revenue",
  "/finance-close",
  "/exports",
  "/connectors",
  "/adsense",
  "/channels",
  // The Groups view's list/clear/archive calls hit /groups and must ride the
  // same injected trusted-principal proxying in dev as the other tenant routes.
  "/groups",
  // /audit/events bootstraps on the Audit view mount and uses the same
  // trusted-gateway auth, so it must be proxied with the injected principal
  // headers in dev or the bootstrap call would 401.
  "/audit",
];

// Repo root is one level above this file (frontend/vite.config.ts -> ..).
// Resolving relative to import.meta.url (not process.cwd()) makes the env
// lookup deterministic regardless of where the dev command is launched
// (`cd frontend && npm run dev` vs `npm --prefix frontend run dev` from root):
// without this, loadEnv resolved to frontend/.env and silently skipped the
// repo-root .env where UMS_TRUSTED_GATEWAY_TOKEN and the VITE_DEV_* dev
// defaults are documented, leaving the dev proxy 401-ing.
const REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

// The full trusted-principal header set the dev proxy injects, as
// [header, env var, fallback] triples. The set is what makes the backend's
// current_principal_from_headers dependency succeed in the default
// UMS_AUTHZ_SOURCE=headers mode (it requires X-User-ID, X-User-Email, X-Role,
// X-Scope-Type, and the gateway token). Per-header notes:
//   - SECURITY: the trusted-gateway token is read from a non-VITE_ env var only.
//     Any VITE_*-prefixed variable Vite reads here also becomes available to
//     client code via import.meta.env at build time, which would leak the token
//     into the browser bundle. Server-only names (UMS_TRUSTED_GATEWAY_TOKEN)
//     stay confined to Node and never reach the browser.
//   - X-UMS-Tenant is overridden or supplied by the proxy. The bootstrap call
//     from <TenantProvider> ships with no slug, and downstream calls may carry a
//     stale "ums" — the proxy is the authority during dev, matching the
//     production gateway contract, which is the source of truth for tenant
//     identity rather than the browser bundle. The "ums" fallback keeps local
//     dev pointed at the seeded tenant unless explicitly overridden.
//   - X-Scope-ID is optional: it applies to non-global dev identities only, and
//     its empty fallback omits it, which is safe for global-scope dev.
// The non-secret dev defaults (user id/email/role/scope_type/tenant slug) keep
// the VITE_DEV_ prefix only because they are intentionally non-secret.
const GATEWAY_HEADER_SOURCES: [header: string, envVar: string, fallback: string][] = [
  ["X-User-ID", "VITE_DEV_GATEWAY_USER_ID", "00000000-0000-0000-0000-0000000000aa"],
  ["X-User-Email", "VITE_DEV_GATEWAY_USER_EMAIL", "dev@ums.local"],
  ["X-Role", "VITE_DEV_GATEWAY_ROLE", "assistant_analyst"],
  ["X-Scope-Type", "VITE_DEV_GATEWAY_SCOPE_TYPE", "global"],
  ["X-UMS-Trusted-Gateway-Token", "UMS_TRUSTED_GATEWAY_TOKEN", ""],
  ["X-UMS-Tenant", "VITE_DEV_GATEWAY_TENANT_SLUG", "ums"],
  ["X-Scope-ID", "VITE_DEV_GATEWAY_SCOPE_ID", ""],
];

// Resolve that set once per config load, in declaration order. A blank value —
// an empty fallback, or an env var explicitly set to "" — drops its header, so
// the proxy sends exactly the headers the previous per-header guards sent.
function resolveGatewayHeaders(env: Record<string, string>): [string, string][] {
  const resolved = GATEWAY_HEADER_SOURCES.map(
    ([header, envVar, fallback]) => [header, env[envVar] ?? fallback] as [string, string],
  );
  return resolved.filter(([, value]) => value !== "");
}

export default defineConfig(({ mode }) => {
  // ============================================================================
  // Purpose: Load env from the repository root in Node only. VITE_-prefixed
  //          names are EXPOSED to the client bundle via import.meta.env, so
  //          secrets must use non-VITE_ names (UMS_TRUSTED_GATEWAY_TOKEN).
  //          Non-secret dev defaults (user id/email/role/scope_type) keep the
  //          VITE_DEV_ prefix only because they are intentionally non-secret.
  // Standards: Server-only secret read from process env; never embedded in code.
  //            envDir is pinned to REPO_ROOT so root `.env.example` documents
  //            the canonical location and `cd frontend && npm run dev` cannot
  //            silently load a different env file.
  // Blast Radius: Frontend dev proxy only — no production bundle exposure.
  // ============================================================================
  const env = loadEnv(mode, REPO_ROOT, "");
  const backendTarget = env.VITE_DEV_BACKEND_URL ?? "http://127.0.0.1:8000";
  // The frontend bootstraps with an empty tenant slug and discovers its real
  // tenant from /tenants/me; the injected header set (see
  // GATEWAY_HEADER_SOURCES) is the dev stand-in for the production gateway.
  const gatewayHeaders = resolveGatewayHeaders(env);
  // SECURITY: read separately for the startup hint only — same non-VITE_ name,
  // so the token still never reaches the browser bundle.
  const gatewayToken = env.UMS_TRUSTED_GATEWAY_TOKEN ?? "";

  if (mode === "development" && !gatewayToken) {
    // Surface a single startup hint so missing trusted-gateway secrets do not
    // silently 401 every proxied tenant-scoped call during local development.
    console.warn(
      "[vite] UMS_TRUSTED_GATEWAY_TOKEN is empty; " +
        `proxied routes (${TENANT_SCOPED_ROUTES.join(", ")}) will 401.`,
    );
  }

  return {
    plugins: [react(), tailwindcss()],
    // envDir must mirror the loadEnv() lookup above so Vite's own runtime
    // env handling (e.g. import.meta.env) reads from the same repo-root
    // .env files as the dev proxy code.
    envDir: REPO_ROOT,
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    server: {
      proxy: Object.fromEntries(
        TENANT_SCOPED_ROUTES.map((route) => [
          route,
          {
            target: backendTarget,
            changeOrigin: true,
            configure(proxy) {
              proxy.on("proxyReq", (proxyReq) => {
                // Inject the resolved trusted-principal header set (blank
                // values were already dropped) so the backend's
                // current_principal_from_headers dependency succeeds.
                for (const [header, value] of gatewayHeaders) {
                  proxyReq.setHeader(header, value);
                }
              });
            },
          },
        ]),
      ),
    },
  };
});
