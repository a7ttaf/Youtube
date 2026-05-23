import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const TENANT_SCOPED_ROUTES = ["/tenants"];

export default defineConfig(({ mode }) => {
  // ============================================================================
  // Purpose: Load env from the project root in Node only. VITE_-prefixed names
  //          are EXPOSED to the client bundle via import.meta.env, so secrets
  //          must use non-VITE_ names (UMS_TRUSTED_GATEWAY_TOKEN). Non-secret
  //          dev defaults (user id/email/role/scope_type) may keep VITE_DEV_*.
  // Standards: Server-only secret read from process env; never embedded in code.
  // Blast Radius: Frontend dev proxy only — no production bundle exposure.
  // ============================================================================
  const env = loadEnv(mode, process.cwd(), "");
  const backendTarget = env.VITE_DEV_BACKEND_URL ?? "http://127.0.0.1:8000";
  const gatewayUserId =
    env.VITE_DEV_GATEWAY_USER_ID ?? "00000000-0000-0000-0000-0000000000aa";
  const gatewayUserEmail =
    env.VITE_DEV_GATEWAY_USER_EMAIL ?? "dev@ums.local";
  const gatewayRole = env.VITE_DEV_GATEWAY_ROLE ?? "assistant_analyst";
  const gatewayScopeType = env.VITE_DEV_GATEWAY_SCOPE_TYPE ?? "global";
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
              });
            },
          },
        ]),
      ),
    },
  };
});
