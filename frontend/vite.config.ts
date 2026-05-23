import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

const TENANT_SCOPED_ROUTES = ["/tenants"];

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), "");
  const backendTarget = env.VITE_DEV_BACKEND_URL ?? "http://127.0.0.1:8000";
  const gatewayUserId =
    env.VITE_DEV_GATEWAY_USER_ID ?? "00000000-0000-0000-0000-0000000000aa";
  const gatewayToken =
    env.VITE_DEV_GATEWAY_TOKEN ?? env.UMS_TRUSTED_GATEWAY_TOKEN ?? "";

  if (mode === "development" && !gatewayToken) {
    // Surface a single startup hint so missing trusted-gateway secrets do not
    // silently 401 every proxied tenant-scoped call during local development.
    // The token is read from Node env only — it never reaches the browser.
    console.warn(
      "[vite] VITE_DEV_GATEWAY_TOKEN (or UMS_TRUSTED_GATEWAY_TOKEN) is empty; " +
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
                if (gatewayUserId) proxyReq.setHeader("X-User-ID", gatewayUserId);
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
