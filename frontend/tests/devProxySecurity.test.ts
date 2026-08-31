import { once } from "node:events";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import {
  createServer as createHttpServer,
  request as httpRequest,
  type IncomingHttpHeaders,
  type Server as HttpServer,
} from "node:http";
import type { AddressInfo } from "node:net";
import { tmpdir } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { afterAll, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import {
  createServer as createViteServer,
  preview as createVitePreview,
  type ConfigEnv,
  type InlineConfig,
  type PreviewServer,
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

const ATTACKER_GATEWAY_HEADERS: Record<string, string> = {
  "X-Company": "attacker-company-root",
  "X-Company-ID": "attacker-company",
  "X-Company-Shadow": "attacker-company-shadow",
  "X-Permission": "finance.override",
  "X-Permission-Shadow": "finance.shadow",
  "X-Permissions": "finance.close",
  "X-Permissions-Shadow": "finance.close.shadow",
  "X-Role": "super_owner",
  "X-Role-Shadow": "super_owner-shadow",
  "X-Scope-Forged": "attacker-scope",
  "X-Scope-ID": "attacker-company",
  "X-Scope-Type": "company",
  "X-UMS-Impersonation": "attacker-identity",
  "X-UMS-Tenant": "attacker-tenant",
  "X-UMS-Trusted-Gateway-Token": "attacker-token",
  "X-Unrelated-Header": "preserve-me",
  "X-User-Email": "attacker@example.test",
  "X-User-ID": "attacker-user",
  "X-User-Impersonated": "attacker-shadow",
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

type AddressableServer = {
  address: () => string | AddressInfo | null;
};

const portOf = (server: AddressableServer): number => {
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("expected an IPv4 backend address");
  }
  return address.port;
};

const httpServerPort = (server: AddressableServer | null | undefined): number => {
  const address = server?.address();
  if (!address || typeof address === "string") {
    throw new Error("expected an IPv4 HTTP server address");
  }
  return address.port;
};

type ViteStartupDependencies = {
  createServer: (config: InlineConfig) => Promise<ViteDevServer>;
  resolvePort: typeof httpServerPort;
};

type RunningViteServer = {
  port: number;
  server: ViteDevServer;
};

const DEFAULT_VITE_STARTUP_DEPENDENCIES: ViteStartupDependencies = {
  createServer: createViteServer,
  resolvePort: httpServerPort,
};

/** Close a Vite server and watcher before preserving a startup failure. */
const rethrowAfterViteStartupCleanup = async (
  server: ViteDevServer | undefined,
  startupError: unknown,
): Promise<never> => {
  if (!server) {
    throw startupError;
  }
  try {
    await server.close();
  } catch (cleanupError) {
    throw new AggregateError(
      [startupError, cleanupError],
      "Vite startup failed and cleanup also failed",
    );
  }
  throw startupError;
};

/** Start Vite and transfer ownership only after its listening port is verified. */
const startViteServer = async (
  config: InlineConfig,
  dependencies: ViteStartupDependencies = DEFAULT_VITE_STARTUP_DEPENDENCIES,
): Promise<RunningViteServer> => {
  // FIX: A failed listen or port read must not leak Vite's server or watcher.
  let server: ViteDevServer | undefined;
  try {
    server = await dependencies.createServer(config);
    await server.listen();
    return { port: dependencies.resolvePort(server.httpServer), server };
  } catch (startupError) {
    return rethrowAfterViteStartupCleanup(server, startupError);
  }
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

const sendExpectRequest = (
  port: number,
  requestPath: string,
  headers: Record<string, string>,
): Promise<HttpResult> =>
  new Promise((resolve, reject) => {
    const body = JSON.stringify({ probe: "expect-continue" });
    const request = httpRequest(
      {
        headers: {
          Accept: "application/json",
          ...headers,
          "Content-Length": String(Buffer.byteLength(body)),
          "Content-Type": "application/json",
          Expect: "100-continue",
        },
        host: "127.0.0.1",
        method: "POST",
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
    request.once("continue", () => request.end(body));
    request.flushHeaders();
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

  it("preserves creation failures without attempting cleanup before ownership", async () => {
    const startupError = new Error("synthetic Vite creation failure");
    const createServer = vi.fn(async () => {
      throw startupError;
    });
    const resolvePort = vi.fn(httpServerPort);

    await expect(
      startViteServer(
        { configFile: false },
        { createServer, resolvePort },
      ),
    ).rejects.toBe(startupError);
    expect(resolvePort).not.toHaveBeenCalled();
  });

  it("closes an owned Vite server when listen fails", async () => {
    const startupError = new Error("synthetic Vite listen failure");
    const close = vi.fn(async () => undefined);
    const server = {
      close,
      httpServer: null,
      listen: vi.fn(async () => {
        throw startupError;
      }),
    } as unknown as ViteDevServer;

    await expect(
      startViteServer(
        { configFile: false },
        {
          createServer: vi.fn(async () => server),
          resolvePort: httpServerPort,
        },
      ),
    ).rejects.toBe(startupError);
    expect(close).toHaveBeenCalledOnce();
  });

  it("closes the real Vite HTTP server and watcher when port resolution fails", async () => {
    const startupError = new Error("synthetic Vite port failure");
    let createdServer: ViteDevServer | undefined;
    const createServer: ViteStartupDependencies["createServer"] = async (config) => {
      createdServer = await createViteServer(config);
      return createdServer;
    };
    const resolvePort = vi.fn(() => {
      throw startupError;
    });

    await expect(
      startViteServer(
        {
          configFile: false,
          logLevel: "silent",
          root: FRONTEND_ROOT,
          server: { host: "127.0.0.1", strictPort: false },
        },
        { createServer, resolvePort },
      ),
    ).rejects.toBe(startupError);
    if (!createdServer) {
      throw new Error("expected the startup probe to create a Vite server");
    }
    const watcher = createdServer.watcher as typeof createdServer.watcher & {
      closed: boolean;
    };
    expect(createdServer.httpServer?.listening).toBe(false);
    expect(watcher.closed).toBe(true);
  });

  it("fails closed before startup when the proxy token is blank", () => {
    expect(() =>
      resolveDevGatewayProxy("serve", "development", {
        ...BASE_ENV,
        UMS_TRUSTED_GATEWAY_TOKEN: "   ",
      }),
    ).toThrow(/UMS_TRUSTED_GATEWAY_TOKEN.*non-blank/iu);
  });

  it("rejects whitespace-only configured identity claims before startup", () => {
    expect(() =>
      resolveDevGatewayProxy("serve", "development", {
        ...BASE_ENV,
        VITE_DEV_GATEWAY_ROLE: " \t ",
      }),
    ).toThrow(/VITE_DEV_GATEWAY_ROLE.*non-blank/iu);
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

  it("requires HTTPS and explicit trust before forwarding to a non-loopback target", () => {
    expect(resolveDevBackendTarget("http://127.0.0.1:8000")).toBe(
      "http://127.0.0.1:8000",
    );
    expect(
      resolveDevBackendTarget("http://127.0.0.1:8000", ["http://127.0.0.1:8000"]),
    ).toBe("http://127.0.0.1:8000");
    expect(resolveDevBackendTarget("http://127.42.17.9:8000")).toBe(
      "http://127.42.17.9:8000",
    );
    expect(resolveDevBackendTarget("http://localhost.:8000")).toBe(
      "http://localhost.:8000",
    );
    expect(() => resolveDevBackendTarget("https://api.example.test")).toThrow(
      /refusing non-loopback/iu,
    );
    expect(
      resolveDevBackendTarget("https://api.example.test", ["https://api.example.test"]),
    ).toBe("https://api.example.test");
    expect(() =>
      resolveDevBackendTarget("http://api.example.test", ["http://api.example.test"]),
    ).toThrow(/non-loopback.*https/iu);
    expect(() =>
      resolveDevBackendTarget("https://api.example.test", ["http://api.example.test"]),
    ).toThrow(/non-loopback.*https/iu);
    expect(() => resolveDevBackendTarget("https://user:secret@api.example.test")).toThrow(
      /credentials/iu,
    );
    expect(() => resolveDevBackendTarget("http://127.0.0.1:8000/api")).toThrow(
      /without a path/iu,
    );
    expect(() => resolveDevBackendTarget("http://127.0.0.1:8000?next=/users")).toThrow(
      /without a path/iu,
    );
    expect(() => resolveDevBackendTarget("http://127.0.0.1:8000#users")).toThrow(
      /without a path/iu,
    );
  });

  it("canonicalizes IDNA before exact trusted-origin comparison", () => {
    const unicodeOrigin = new URL("https://例え.テスト").origin;
    const asciiOrigin = new URL("https://xn--r8jz45g.xn--zckzah").origin;
    expect(unicodeOrigin).toBe(asciiOrigin);
    expect(resolveDevBackendTarget(unicodeOrigin, [asciiOrigin])).toBe(asciiOrigin);
  });
});

describe("actual Vite serve and preview activation", () => {
  it("proxies adversarial development traffic but keeps development-mode preview inert", async () => {
    const hits: BackendHit[] = [];
    const backend = createHttpServer((request, response) => {
      request.resume();
      request.once("end", () => {
        hits.push({ headers: request.headers, method: request.method, url: request.url });
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ proxied: true }));
      });
    });
    backend.listen(0, "127.0.0.1");
    await once(backend, "listening");
    const backendTarget = `http://127.0.0.1:${portOf(backend)}`;
    const previewRoot = await mkdtemp(path.join(tmpdir(), "ums-vite-preview-"));
    await writeFile(
      path.join(previewRoot, "index.html"),
      "<!doctype html><title>preview without gateway</title>",
      "utf8",
    );

    for (const [key, value] of Object.entries({
      ...BASE_ENV,
      UMS_DEV_TRUSTED_BACKEND_ORIGINS: "",
      VITE_DEV_BACKEND_URL: backendTarget,
      VITE_DEV_GATEWAY_SCOPE_ID: "",
    })) {
      vi.stubEnv(key, value);
    }

    let serve: RunningViteServer | undefined;
    let preview: PreviewServer | undefined;
    try {
      const configFile = path.resolve(FRONTEND_ROOT, "vite.config.ts");
      serve = await startViteServer({
        configFile,
        logLevel: "silent",
        mode: "development",
        root: FRONTEND_ROOT,
        server: { host: "127.0.0.1", strictPort: false },
      });
      const served = await sendRequest(
        serve.port,
        "/users",
        ATTACKER_GATEWAY_HEADERS,
      );
      expect(served.status).toBe(200);
      expect(hits).toHaveLength(1);
      expect(hits[0]?.headers["x-role"]).toBe(BASE_ENV.VITE_DEV_GATEWAY_ROLE);
      expect(hits[0]?.headers["x-role-shadow"]).toBeUndefined();

      await serve.server.close();
      serve = undefined;
      hits.length = 0;

      preview = await createVitePreview({
        build: { outDir: previewRoot },
        configFile,
        logLevel: "silent",
        mode: "development",
        preview: { host: "127.0.0.1", strictPort: false },
        root: FRONTEND_ROOT,
      });
      const previewResult = await sendRequest(
        httpServerPort(preview.httpServer),
        "/users",
        ATTACKER_GATEWAY_HEADERS,
      );
      expect([200, 404]).toContain(previewResult.status);
      expect(previewResult.body).not.toContain('"proxied":true');
      expect(hits).toEqual([]);
      expect(preview.config.preview.proxy).toBeUndefined();
    } finally {
      await preview?.close();
      await serve?.server.close();
      await closeHttpServer(backend);
      await rm(previewRoot, { force: true, recursive: true });
      vi.unstubAllEnvs();
    }
  }, 30_000);
});

describe("real development gateway proxy", () => {
  const hits: BackendHit[] = [];
  let backend: HttpServer | undefined;
  let vite: ViteDevServer | undefined;
  let backendPort = 0;
  let vitePort = 0;

  beforeAll(async () => {
    backend = createHttpServer((request, response) => {
      request.resume();
      request.once("end", () => {
        hits.push({ headers: request.headers, method: request.method, url: request.url });
        response.writeHead(200, { "Content-Type": "application/json" });
        response.end(JSON.stringify({ proxied: true }));
      });
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
    const startedVite = await startViteServer({
      configFile: false,
      logLevel: "silent",
      root: FRONTEND_ROOT,
      server: {
        host: "127.0.0.1",
        port: 5173,
        proxy,
        strictPort: false,
      },
    });
    vite = startedVite.server;
    vitePort = startedVite.port;
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

  it("strips the broad trusted-header namespace and omits scope id for global config", async () => {
    const result = await sendRequest(vitePort, "/users", ATTACKER_GATEWAY_HEADERS);

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
    expect(headers?.["x-company-id"]).toBeUndefined();
    expect(headers?.["x-company"]).toBeUndefined();
    expect(headers?.["x-company-shadow"]).toBeUndefined();
    expect(headers?.["x-permission"]).toBeUndefined();
    expect(headers?.["x-permission-shadow"]).toBeUndefined();
    expect(headers?.["x-permissions"]).toBeUndefined();
    expect(headers?.["x-permissions-shadow"]).toBeUndefined();
    expect(headers?.["x-role-shadow"]).toBeUndefined();
    expect(headers?.["x-scope-forged"]).toBeUndefined();
    expect(headers?.["x-ums-impersonation"]).toBeUndefined();
    expect(headers?.["x-user-impersonated"]).toBeUndefined();
    expect(headers?.["x-unrelated-header"]).toBe("preserve-me");
  });

  it("scrubs and replaces trusted headers on a real Expect: 100-continue request", async () => {
    const result = await sendExpectRequest(
      vitePort,
      "/connectors",
      ATTACKER_GATEWAY_HEADERS,
    );

    expect(result.status).toBe(200);
    expect(hits).toHaveLength(1);
    const hit = hits[0];
    expect(hit?.method).toBe("POST");
    expect(hit?.url).toBe("/connectors");
    expect(hit?.headers.expect).toBe("100-continue");
    expect(hit?.headers["x-user-id"]).toBe(BASE_ENV.VITE_DEV_GATEWAY_USER_ID);
    expect(hit?.headers["x-role"]).toBe(BASE_ENV.VITE_DEV_GATEWAY_ROLE);
    expect(hit?.headers["x-scope-type"]).toBe("company");
    expect(hit?.headers["x-scope-id"]).toBe("company-a");
    expect(hit?.headers["x-ums-trusted-gateway-token"]).toBe(
      BASE_ENV.UMS_TRUSTED_GATEWAY_TOKEN,
    );
    expect(hit?.headers["x-company-id"]).toBeUndefined();
    expect(hit?.headers["x-company"]).toBeUndefined();
    expect(hit?.headers["x-company-shadow"]).toBeUndefined();
    expect(hit?.headers["x-permission"]).toBeUndefined();
    expect(hit?.headers["x-permission-shadow"]).toBeUndefined();
    expect(hit?.headers["x-permissions"]).toBeUndefined();
    expect(hit?.headers["x-permissions-shadow"]).toBeUndefined();
    expect(hit?.headers["x-role-shadow"]).toBeUndefined();
    expect(hit?.headers["x-scope-forged"]).toBeUndefined();
    expect(hit?.headers["x-ums-impersonation"]).toBeUndefined();
    expect(hit?.headers["x-user-impersonated"]).toBeUndefined();
    expect(hit?.headers["x-unrelated-header"]).toBe("preserve-me");
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
