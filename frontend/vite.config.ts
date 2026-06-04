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
  "/revenue",
  "/finance-close",
  "/exports",
  "/connectors",
  "/adsense",
  "/channels",
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

export default defineConfig(({ mode }) => { // skipcq: JS-R1005
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
  const gatewayUserId =
    env.VITE_DEV_GATEWAY_USER_ID ?? "00000000-0000-0000-0000-0000000000aa";
  const gatewayUserEmail =
    env.VITE_DEV_GATEWAY_USER_EMAIL ?? "dev@ums.local";
  const gatewayRole = env.VITE_DEV_GATEWAY_ROLE ?? "assistant_analyst";
  const gatewayScopeType = env.VITE_DEV_GATEWAY_SCOPE_TYPE ?? "global";
  // Tenant slug injected by the dev proxy mirrors the production reverse-proxy
  // model: the trusted gateway is the source of truth for tenant identity,
  // not the browser bundle. The frontend bootstraps with an empty slug and
  // discovers its real tenant from /tenants/me; this default keeps local
  // dev pointed at the seeded "ums" tenant unless explicitly overridden.
  const gatewayTenantSlug = env.VITE_DEV_GATEWAY_TENANT_SLUG ?? "ums";
  // SECURITY: Read trusted-gateway token from a non-VITE_ env var only. Any
  // VITE_*-prefixed variable Vite reads here also becomes available to client
  // code via import.meta.env at build time, which would leak the token into
  // the browser bundle. Server-only names (UMS_TRUSTED_GATEWAY_TOKEN) stay
  // confined to Node and never reach the browser.
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
              proxy.on("proxyReq", (proxyReq) => { // skipcq: JS-R1005
                // Inject the full trusted-principal header set so the backend
                // current_principal_from_headers dependency succeeds in the
                // default UMS_AUTHZ_SOURCE=headers mode (it requires X-User-ID,
                // X-User-Email, X-Role, X-Scope-Type, and the gateway token).
                if (gatewayUserId) proxyReq.setHeader("X-User-ID", gatewayUserId);
                if (gatewayUserEmail)
                  proxyReq.setHeader("X-User-Email", gatewayUserEmail);
                if (gatewayRole) proxyReq.setHeader("X-Role", gatewayRole);
                if (gatewayScopeType)
                  proxyReq.setHeader("X-Scope-Type", gatewayScopeType);
                if (gatewayToken)
                  proxyReq.setHeader("X-UMS-Trusted-Gateway-Token", gatewayToken);
                // Override or supply X-UMS-Tenant from the proxy. The
                // bootstrap call from <TenantProvider> ships with no slug,
                // and downstream calls may carry a stale "ums" — the proxy
                // is the authority during dev, matching the production
                // gateway contract.
                if (gatewayTenantSlug)
                  proxyReq.setHeader("X-UMS-Tenant", gatewayTenantSlug);
              });
            },
          },
        ]),
      ),
    },
  };
});
