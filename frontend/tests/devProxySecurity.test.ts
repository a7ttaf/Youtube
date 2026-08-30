import { once } from "node:events";
import {
  createServer as createHttpServer,
  request as httpRequest,
  type IncomingHttpHeaders,
  type Server as HttpServer,
} from "node:http";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, beforeEach, describe, expect, it } from "vitest";
import {
  createServer as createViteServer,
  type ConfigEnv,
  type UserConfig,
  type ViteDevServer,
} from "vite";

import viteConfig from "../vite.config";
import {
  TENANT_SCOPED_ROUTES,
  buildTenantScopedProxy,
  proxyContextForRoute,
  resolveDevBackendTarget,
  resolveDevGatewayProxy,
  resolveGatewayHeaders,
  shouldEnableDevGateway,
} from "../vite.config";

// ============================================================================
// Purpose: Exercise the trusted-header boundary through a real Vite HTTP proxy
//   and a recording backend, not only through proxy-option object inspection.
// Database/ORM: None.
// Standards: Bind both servers to loopback on ephemeral ports; send raw Host,
//   Origin, absolute-form, encoded-path, and attacker-header requests.
// Blast Radius: Test-only local sockets; no application database or network.
// Connections:
//   - File: frontend/devProxy.ts -> boundary implementation under test.
//   - File: frontend/vite.config.ts -> dev-only activation contract.
// ============================================================================

const HERE = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_ROOT = path.resolve(HERE, "..");

const BASE_ENV: Record<string, string> = {
  UMS_TRUSTED_GATEWAY_TOKEN: "configured-proxy-token",
  VITE_DEV_GATEWAY_USER_ID: "00000000-0000-0000-0000-0000000000cc",
  VITE_DEV_GATEWAY_USER_EMAIL: "configured@ums.local",
  VITE_DEV_GATEWAY_ROLE: "finance_admin",
  VITE_DEV_GATEWAY_SCOPE_TYPE: "global",
  VITE_DEV_GATEWAY_TENANT_SLUG: "ums",
};

type BackendHit = {
  headers: IncomingHttpHeaders;
  method: string | undefined;
  url: string | undefined;
};

type HttpResult = {
  body: string;
  headers: IncomingHttpHeaders;
  status: number;
};

const portOf = (server: HttpServer): number => {
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("expected an IPv4 backend address");
  }
  return address.port;
};

const vitePortOf = (server: ViteDevServer): number => {
  const address = server.httpServer?.address();
  if (!address || typeof address === "string") {
    throw new Error("expected an IPv4 Vite address");
  }
  return address.port;
};

const closeHttpServer = async (server: HttpServer | undefined): Promise<void> => {
  if (!server?.listening) {
    return;
  }
  await new Promise<void>((resolve, reject) => {
    server.close((error) => (error ? reject(error) : resolve()));
  });
};

const sendRequest = (
  port: number,
  requestPath: string,
  headers: Record<string, string> = {},
): Promise<HttpResult> =>
  new Promise((resolve, reject) => {
    const request = httpRequest(
      {
        headers: { Accept: "application/json", ...headers },
        host: "127.0.0.1",
        method: "GET",
        path: requestPath,
        port,
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            body: Buffer.concat(chunks).toString("utf8"),
            headers: response.headers,
            status: response.statusCode ?? 0,
          });
        });
      },
    );
    request.on("error", reject);
    request.end();
  });

describe("development gateway config", () => {
  it("activates only for the development serve command", () => {
    expect(shouldEnableDevGateway("serve", "development")).toBe(true);
    expect(shouldEnableDevGateway("serve", "development", true)).toBe(false);
    expect(shouldEnableDevGateway("serve", "production")).toBe(false);
    expect(shouldEnableDevGateway("build", "development")).toBe(false);
    expect(resolveDevGatewayProxy("build", "production", {})).toBeUndefined();
    expect(resolveDevGatewayProxy("serve", "production", {})).toBeUndefined();
    expect(resolveDevGatewayProxy("serve", "development", {}, true)).toBeUndefined();
  });

  it("keeps the actual default config proxy-free for development-mode preview", async () => {
    if (typeof viteConfig !== "function") {
      throw new Error("expected vite.config default export to be a config callback");
    }
    const previewEnv: ConfigEnv = {
      command: "serve",
      isPreview: true,
      isSsrBuild: false,
      mode: "development",
    };
    const resolved = (await viteConfig(previewEnv)) as UserConfig;
    expect(resolved.server?.proxy).toBeUndefined();
  });

  it("fails closed before startup when the proxy token is blank", () => {
    expect(() =>
      resolveDevGatewayProxy("serve", "development", {
        ...BASE_ENV,
        UMS_TRUSTED_GATEWAY_TOKEN: "   ",
      }),
    ).toThrow(/UMS_TRUSTED_GATEWAY_TOKEN.*non-blank/iu);
  });

  it("fails closed when a non-global scope has no configured scope id", () => {
    expect(() =>
      resolveGatewayHeaders({
        ...BASE_ENV,
        VITE_DEV_GATEWAY_SCOPE_TYPE: "company",
        VITE_DEV_GATEWAY_SCOPE_ID: " ",
      }),
    ).toThrow(/VITE_DEV_GATEWAY_SCOPE_ID.*company/iu);
  });

  it("rejects a scope id on a global identity instead of sending a contradictory principal", () => {
    expect(() =>
      resolveGatewayHeaders({
        ...BASE_ENV,
        VITE_DEV_GATEWAY_SCOPE_ID: "company-a",
      }),
    ).toThrow(/SCOPE_ID.*blank.*global/iu);
  });

  it("requires explicit trust before forwarding the token to a non-loopback target", () => {
    expect(resolveDevBackendTarget("http://127.0.0.1:8000")).toBe(
      "http://127.0.0.1:8000",
    );
    expect(() => resolveDevBackendTarget("https://api.example.test")).toThrow(
      /refusing non-loopback/iu,
    );
    expect(
      resolveDevBackendTarget("https://api.example.test", ["https://api.example.test"]),
    ).toBe("https://api.example.test");
    expect(() => resolveDevBackendTarget("https://user:secret@api.example.test")).toThrow(
      /credentials/iu,
    );
    expect(() => resolveDevBackendTarget("http://127.0.0.1:8000/api")).toThrow(
      /without a path/iu,
    );
  });
});
describe("real development gateway proxy", () => {
  const hits: BackendHit[] = [];
  let backend: HttpServer | undefined;
  let vite: ViteDevServer | undefined;
  let backendPort = 0;
  let vitePort = 0;

  beforeAll(async () => {
    backend = createHttpServer((request, response) => {
      hits.push({ headers: request.headers, method: request.method, url: request.url });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ proxied: true }));
    });
    backend.listen(0, "127.0.0.1");
    await once(backend, "listening");
    backendPort = portOf(backend);
    const backendTarget = `http://127.0.0.1:${backendPort}`;

    const globalHeaders = resolveGatewayHeaders(BASE_ENV);
    const scopedHeaders = resolveGatewayHeaders({
      ...BASE_ENV,
      VITE_DEV_GATEWAY_SCOPE_TYPE: "company",
      VITE_DEV_GATEWAY_SCOPE_ID: "company-a",
    });
    const proxy = {
      ...buildTenantScopedProxy(["/users"], backendTarget, globalHeaders),
      ...buildTenantScopedProxy(
        TENANT_SCOPED_ROUTES.filter((route) => route !== "/users"),
        backendTarget,
        scopedHeaders,
      ),
    };
    vite = await createViteServer({
      configFile: false,
      logLevel: "silent",
      root: FRONTEND_ROOT,
      server: {
        host: "127.0.0.1",
        port: 0,
        proxy,
        strictPort: true,
      },
    });
    await vite.listen();
    vitePort = vitePortOf(vite);
  }, 30_000);

  beforeEach(() => {
    hits.length = 0;
  });

  afterAll(async () => {
    await vite?.close();
    await closeHttpServer(backend);
  });

  it.each(["/users", "/users/123?include=access", "/session/me"])(
    "proxies the valid exact route %s",
    async (requestPath) => {
      const result = await sendRequest(vitePort, requestPath);
      expect(result.status).toBe(200);
      expect(hits).toHaveLength(1);
      expect(hits[0]?.url).toBe(requestPath);
    },
  );

  it.each([
    "/users.evil",
    "/users-evil",
    "/users%2Fevil",
    "/untrusted?next=/users",
  ])("does not proxy prefix or query smuggling: %s", async (requestPath) => {
    await sendRequest(vitePort, requestPath);
    expect(hits).toEqual([]);
  });

  it.each([
    "/users/%2e%2e/tenants",
    "/users/%252e%252e/tenants",
    "/users/safe%2F..%2Ftenants",
    "/users/%5ctenants",
  ])("rejects encoded path confusion before the backend: %s", async (requestPath) => {
    const result = await sendRequest(vitePort, requestPath);
    expect(result.status).toBe(400);
    expect(hits).toEqual([]);
  });

  it("does not proxy an absolute-form request target", async () => {
    const result = await sendRequest(vitePort, "http://attacker.invalid/users");
    expect(result.status).not.toBe(200);
    expect(hits).toEqual([]);
  });

  it("lets Vite reject an untrusted Host before the proxy", async () => {
    const result = await sendRequest(vitePort, "/users", { Host: "attacker.invalid" });
    expect(result.status).toBe(403);
    expect(hits).toEqual([]);
  });

  it("rejects a cross-origin browser request even with a valid loopback Host", async () => {
    const result = await sendRequest(vitePort, "/users", {
      Origin: "https://attacker.invalid",
      "Sec-Fetch-Site": "cross-site",
    });
    expect(result.status).toBe(403);
    expect(hits).toEqual([]);
  });

  it("strips every attacker principal header and omits scope id for global config", async () => {
    const result = await sendRequest(vitePort, "/users", {
      "X-Role": "super_owner",
      "X-Scope-ID": "attacker-company",
      "X-Scope-Type": "company",
      "X-UMS-Tenant": "attacker-tenant",
      "X-UMS-Trusted-Gateway-Token": "attacker-token",
      "X-User-Email": "attacker@example.test",
      "X-User-ID": "attacker-user",
    });

    expect(result.status).toBe(200);
    expect(hits).toHaveLength(1);
    const headers = hits[0]?.headers;
    expect(headers?.["x-user-id"]).toBe(BASE_ENV.VITE_DEV_GATEWAY_USER_ID);
    expect(headers?.["x-user-email"]).toBe(BASE_ENV.VITE_DEV_GATEWAY_USER_EMAIL);
    expect(headers?.["x-role"]).toBe(BASE_ENV.VITE_DEV_GATEWAY_ROLE);
    expect(headers?.["x-scope-type"]).toBe("global");
    expect(headers?.["x-scope-id"]).toBeUndefined();
    expect(headers?.["x-ums-tenant"]).toBe(BASE_ENV.VITE_DEV_GATEWAY_TENANT_SLUG);
    expect(headers?.["x-ums-trusted-gateway-token"]).toBe(
      BASE_ENV.UMS_TRUSTED_GATEWAY_TOKEN,
    );
  });

  it("rewrites Host, preserves a same-origin Origin, and injects required scoped identity", async () => {
    const browserOrigin = `http://127.0.0.1:${vitePort}`;
    const result = await sendRequest(vitePort, "/session/me", {
      Origin: browserOrigin,
      "Sec-Fetch-Site": "same-origin",
      "X-Scope-ID": "attacker-company",
    });

    expect(result.status).toBe(200);
    expect(hits).toHaveLength(1);
    const headers = hits[0]?.headers;
    expect(headers?.host).toBe(`127.0.0.1:${backendPort}`);
    expect(headers?.origin).toBe(browserOrigin);
    expect(headers?.["x-scope-type"]).toBe("company");
    expect(headers?.["x-scope-id"]).toBe("company-a");
  });

  it("uses exact-segment proxy contexts for every allowlisted route", () => {
    const proxy = resolveDevGatewayProxy("serve", "development", BASE_ENV);
    expect(Object.keys(proxy ?? {})).toEqual(TENANT_SCOPED_ROUTES.map(proxyContextForRoute));
  });
});
