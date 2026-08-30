import path from "node:path";
import { fileURLToPath, URL } from "node:url";

import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig, loadEnv } from "vite";

import {
  DEFAULT_BACKEND_TARGET,
  TENANT_SCOPED_ROUTES,
  TRUSTED_BACKEND_ORIGINS_ENV,
  buildTenantScopedProxy,
  resolveDevBackendTarget,
  resolveGatewayHeaders,
} from "./src/devProxy";

export {
  DEFAULT_BACKEND_TARGET,
  TENANT_SCOPED_ROUTES,
  TRUSTED_BACKEND_ORIGINS_ENV,
  buildTenantScopedProxy,
  resolveDevBackendTarget,
  resolveGatewayHeaders,
} from "./src/devProxy";

// Repo root is one level above this file (frontend/vite.config.ts -> ..).
// Resolving relative to import.meta.url (not process.cwd()) makes the env
// lookup deterministic regardless of where the dev command is launched
// (`cd frontend && npm run dev` vs `npm --prefix frontend run dev`).
const REPO_ROOT = path.resolve(
  path.dirname(fileURLToPath(import.meta.url)),
  "..",
);

// ============================================================================
// Purpose: Load repo-root env and derive the dev-proxy target, trust allowlist,
//   and injected trusted-principal headers.
// Database/ORM: None.
// Standards: Keep the token in the Node-only environment namespace, validate
//   the target before proxy construction, and warn rather than fabricate a
//   missing credential.
// Blast Radius: Frontend development server configuration only.
// Connections:
//   - File: frontend/src/devProxy.ts -> validates target and builds headers.
//   - File: README.md -> documents the repo-root environment contract.
// ============================================================================
const loadDevProxy = (mode: string) => {
  const env = loadEnv(mode, REPO_ROOT, "");
  const trustedOrigins = (env[TRUSTED_BACKEND_ORIGINS_ENV] ?? "").split(",");
  const backendTarget = resolveDevBackendTarget(
    env.VITE_DEV_BACKEND_URL ?? DEFAULT_BACKEND_TARGET,
    trustedOrigins,
  );
  const gatewayHeaders = resolveGatewayHeaders(env);
  const gatewayToken = env.UMS_TRUSTED_GATEWAY_TOKEN ?? "";

  if (mode === "development" && !gatewayToken) {
    console.warn(
      "[vite] UMS_TRUSTED_GATEWAY_TOKEN is empty; " +
        `proxied routes (${TENANT_SCOPED_ROUTES.join(", ")}) will 401.`,
    );
  }
  return { backendTarget, gatewayHeaders, trustedOrigins };
};

export default defineConfig(({ mode }) => {
  // ============================================================================
  // Purpose: Load Node-only environment and construct the development proxy.
  // Database/ORM: None.
  // Standards: Server-only gateway secret; VITE_* values are non-secret and
  //   never substitute for the trusted token. Backend target validation runs
  //   before proxy entries are created.
  // Blast Radius: Frontend development server only; production bundle and
  //   backend authorization are unaffected.
  // Connections:
  //   - File: frontend/src/devProxy.ts -> route/header/target contracts.
  //   - File: backend/ums_smart_revenue/api/dependencies.py -> trusted header
  //     consumer in headers mode.
  // ============================================================================
  const { backendTarget, gatewayHeaders, trustedOrigins } = loadDevProxy(mode);

  return {
    plugins: [react(), tailwindcss()],
    envDir: REPO_ROOT,
    resolve: {
      alias: {
        "@": fileURLToPath(new URL("./src", import.meta.url)),
      },
    },
    server: {
      proxy: buildTenantScopedProxy(
        TENANT_SCOPED_ROUTES,
        backendTarget,
        gatewayHeaders,
        trustedOrigins,
      ),
    },
  };
});
