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
