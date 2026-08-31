import { readFileSync, readdirSync } from "node:fs";
import {
  createServer as createHttpServer,
  request as httpRequest,
  type Server,
} from "node:http";
import type { AddressInfo } from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { describe, expect, it, vi } from "vitest";
import { createServer as createViteServer } from "vite";

import {
  TENANT_SCOPED_ROUTES,
  buildTenantScopedProxy,
  resolveDevBackendTarget,
  resolveGatewayHeaders,
} from "@/devProxy";

// ============================================================================
// Purpose: Guard the development proxy's route and trusted-header contracts.
// Database/ORM: None (frontend dev-server configuration and static source scan).
// Standards: Derive requested API prefixes from the TypeScript AST instead of
//   a hand parser, exclude the proxy declaration itself to avoid self-coverage,
//   assert every forwarded header, and keep authorization tests on the backend
//   boundary where the real policy is enforced.
// Blast Radius: Development proxy only. No production bundle or runtime data.
// Connections:
//   - File: frontend/vite.config.ts -> route, header, and target helpers.
//   - File: frontend/src/lib/api/client.ts -> JSON Accept header on API calls.
//   - File: backend/ums_smart_revenue/api/dependencies.py -> gateway-header
//     authentication contract; route denial cases live in tests/api/.
//   - File: frontend/README.md -> documents the route and import exceptions.
// ============================================================================

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC_DIR = path.resolve(HERE, "..", "src");
const DEV_PROXY_SOURCE = path.join(SRC_DIR, "devProxy.ts");
const SOURCE_SUFFIXES = [".ts", ".tsx"];

// Change-detector only — the derived assertion below is the coverage proof.
const EXPECTED_ROUTES = [
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
];

// Prove the AST walk cannot pass vacuously after a future syntax change.
const SCANNER_HEALTH_PREFIXES = [
  "/adsense",
  "/audit",
  "/channels",
  "/connectors",
  "/exports",
  "/finance-close",
  "/groups",
  "/org-units",
  "/revenue",
  "/session",
  "/tenants",
];

type ScannedLiteral = {
  value: string;
  /** True when the literal is the right operand of string concatenation. */
  continuesExpression: boolean;
};

const INTERPOLATION = "\u0000";

const isRightOperandOfPlus = (node: ts.Node): boolean => {
  let current = node;
  while (ts.isParenthesizedExpression(current.parent)) {
    current = current.parent;
  }
  const parent = current.parent;
  return (
    ts.isBinaryExpression(parent) &&
    parent.operatorToken.kind === ts.SyntaxKind.PlusToken &&
    parent.right === current
  );
};

const templateValue = (node: ts.TemplateExpression): string =>
  [
    node.head.text,
    ...node.templateSpans.flatMap((span) => [INTERPOLATION, span.literal.text]),
  ].join("");

// ============================================================================
// Purpose: Extract string and template literals from TypeScript source while
//   letting the compiler distinguish comments, regexes, division, and strings.
// Database/ORM: None.
// Standards: TypeScript's parser is the source grammar; malformed files still
//   produce the compiler's best-effort AST, and no raw-text regex can hide a
//   request path. Concatenated right operands remain fragments.
// Blast Radius: Test-only route coverage; no application behavior.
// Connections:
//   - File: typescript -> parser/token contract for .ts/.tsx source.
//   - File: frontend/src/lib/api -> requested path literals discovered below.
// ============================================================================
export const scanStringLiterals = (source: string): ScannedLiteral[] => {
  const sourceFile = ts.createSourceFile(
    "dev-proxy-scan.tsx",
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const literals: ScannedLiteral[] = [];
  const visit = (node: ts.Node): void => {
    if (ts.isStringLiteral(node)) {
      literals.push({ value: node.text, continuesExpression: isRightOperandOfPlus(node) });
    } else if (ts.isNoSubstitutionTemplateLiteral(node)) {
      literals.push({ value: node.text, continuesExpression: isRightOperandOfPlus(node) });
    } else if (ts.isTemplateExpression(node)) {
      literals.push({
        value: templateValue(node),
        continuesExpression: isRightOperandOfPlus(node),
      });
    }
    ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return literals;
};

/** Every .ts/.tsx file under frontend/src, recursively. */
const sourceFiles = (dir: string): string[] =>
  readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) {
      return sourceFiles(full);
    }
    return SOURCE_SUFFIXES.some((suffix) => entry.name.endsWith(suffix)) ? [full] : [];
  });

/** Return the first path segment of a literal such as /revenue/months/x. */
const firstSegment = (literal: string): string => {
  const untilPlaceholder = literal.slice(1).split(INTERPOLATION, 1)[0] ?? "";
  return `/${untilPlaceholder.split(/[/?#]/u, 1)[0] ?? ""}`;
};

/** True for a leading-slash API path, not a concatenated path fragment. */
const isRequestPathLiteral = (literal: ScannedLiteral): boolean =>
  literal.value.startsWith("/") &&
  !literal.continuesExpression &&
  literal.value.length >= 2 &&
  /^[a-z]/iu.test(literal.value[1] ?? "");

// ============================================================================
// Purpose: Derive the backend prefixes the frontend actually requests.
// Database/ORM: None.
// Standards: Scan all application frontend/src files through the TypeScript AST
//   (excluding devProxy.ts, which declares the expected list), then fail closed
//   when any leading-slash request prefix lacks a proxy entry.
// Blast Radius: Test-only coverage; no runtime or authorization effect.
// Connections:
//   - File: frontend/vite.config.ts -> TENANT_SCOPED_ROUTES.
//   - File: frontend/src/lib/api -> request call sites.
// ============================================================================
export const discoverRequestedPrefixes = (): string[] => {
  const found = new Set<string>();
  for (const file of sourceFiles(SRC_DIR)) {
    if (file === DEV_PROXY_SOURCE) {
      continue;
    }
    for (const literal of scanStringLiterals(readFileSync(file, "utf8"))) {
      if (isRequestPathLiteral(literal)) {
        found.add(firstSegment(literal.value));
      }
    }
  }
  return [...found].sort();
};

/** Prefixes requested by the app but absent from a candidate proxy list. */
export const uncoveredPrefixes = (prefixes: string[], routes: readonly string[]): string[] =>
  prefixes.filter((prefix) => !routes.includes(prefix)).sort();

const REQUESTED_PREFIXES = discoverRequestedPrefixes();
const BACKEND_TARGET = "http://127.0.0.1:8000";

const ALL_GATEWAY_HEADERS: [string, string][] = [
  ["X-User-ID", "00000000-0000-0000-0000-0000000000aa"],
  ["X-User-Email", "dev@ums.local"],
  ["X-Role", "finance_admin"],
  ["X-Scope-Type", "company"],
  ["X-UMS-Trusted-Gateway-Token", "test-token"],
  ["X-UMS-Tenant", "ums"],
  ["X-Scope-ID", "company-tv"],
];

const ALL_GATEWAY_ENV: Record<string, string> = {
  VITE_DEV_GATEWAY_USER_ID: ALL_GATEWAY_HEADERS[0][1],
  VITE_DEV_GATEWAY_USER_EMAIL: ALL_GATEWAY_HEADERS[1][1],
  VITE_DEV_GATEWAY_ROLE: ALL_GATEWAY_HEADERS[2][1],
  VITE_DEV_GATEWAY_SCOPE_TYPE: ALL_GATEWAY_HEADERS[3][1],
  UMS_TRUSTED_GATEWAY_TOKEN: ALL_GATEWAY_HEADERS[4][1],
  VITE_DEV_GATEWAY_TENANT_SLUG: ALL_GATEWAY_HEADERS[5][1],
  VITE_DEV_GATEWAY_SCOPE_ID: ALL_GATEWAY_HEADERS[6][1],
};

type ProxyReqHandler = (proxyReq: {
  getHeaderNames: () => string[];
  removeHeader: (header: string) => void;
  setHeader: (header: string, value: string) => void;
}) => void;

type ConfigurableProxyEntry = {
  target: string;
  changeOrigin: boolean;
  configure: (proxy: { on: (event: string, fn: ProxyReqHandler) => void }) => void;
};

/** Narrow one built proxy entry, failing loudly rather than skipping its shape. */
const asConfigurableEntry = (entry: unknown, route: string): ConfigurableProxyEntry => {
  if (typeof entry !== "object" || entry === null) {
    throw new Error(`expected an object proxy entry for ${route}`);
  }
  const candidate = entry as Partial<ConfigurableProxyEntry>;
  if (typeof candidate.target !== "string" || typeof candidate.configure !== "function") {
    throw new Error(`expected ${route} to declare a target and a configure hook`);
  }
  return candidate as ConfigurableProxyEntry;
};

type ProxyProbe = {
  configured: [string, string][];
  operations: string[];
  remaining: [string, string][];
};

/** Run a route's configure hook against preloaded inbound headers. */
const probeProxyRequest = (
  entry: ConfigurableProxyEntry,
  route: string,
  preloaded: [string, string][] = [],
): ProxyProbe => {
  const collected: [string, string][] = [];
  const operations: string[] = [];
  const headers = new Map<string, [string, string]>(
    preloaded.map(([header, value]) => [header.toLowerCase(), [header, value]]),
  );
  let handler: ProxyReqHandler | undefined;
  entry.configure({
    on: (event, fn) => {
      if (event === "proxyReq") {
        handler = fn;
      }
    },
  });
  if (!handler) {
    throw new Error(`expected ${route} to register a proxyReq handler`);
  }
  handler({
    getHeaderNames: () => [...headers.values()].map(([header]) => header),
    removeHeader: (header: string) => {
      operations.push(`remove:${header}`);
      headers.delete(header.toLowerCase());
    },
    setHeader: (header: string, value: string) => {
      operations.push(`set:${header}`);
      collected.push([header, value]);
      headers.set(header.toLowerCase(), [header, value]);
    },
  });
  return {
    configured: collected,
    operations,
    remaining: [...headers.values()],
  };
};

/** Run a clean request probe and return only the configured headers. */
const injectedHeaders = (entry: ConfigurableProxyEntry, route: string): [string, string][] =>
  probeProxyRequest(entry, route).configured;

const ATTACKER_PRELOADED_HEADERS: [string, string][] = [
  ["x-user-id", "attacker-user"],
  ["X-User-Impersonated", "attacker-shadow"],
  ["x-user-email", "attacker@example.test"],
  ["x-role", "super_owner"],
  ["X-Permissions", "finance.close"],
  ["x-company-id", "attacker-company"],
  ["x-scope-type", "global"],
  ["X-Scope-Forged", "attacker-scope"],
  ["x-scope-id", "attacker-scope-id"],
  ["x-ums-trusted-gateway-token", "attacker-token"],
  ["x-ums-tenant", "attacker-tenant"],
  ["X-UMS-Impersonation", "attacker-identity"],
  ["X-Unrelated-Header", "preserve-me"],
];

type BackendRequest = {
  method?: string;
  url?: string;
  headers: Record<string, string | string[] | undefined>;
};

type HttpResponse = {
  statusCode: number;
  body: string;
};

const listenOnLoopback = (server: Server): Promise<number> =>
  new Promise((resolve, reject) => {
    let onError: (error: Error) => void;

    /** Resolve the ephemeral port and detach the failure listener. */
    const onListening = (): void => {
      server.off("error", onError);
      const address = server.address();
      if (!address || typeof address === "string") {
        reject(new Error("expected an ephemeral TCP address"));
        return;
      }
      resolve((address as AddressInfo).port);
    };

    /** Reject a failed listen attempt and detach the success listener. */
    onError = (error: Error): void => {
      server.off("listening", onListening);
      reject(error);
    };

    server.once("error", onError);
    server.once("listening", onListening);
    server.listen(0, "127.0.0.1");
  });

const closeHttpServer = (server: Server): Promise<void> =>
  new Promise((resolve, reject) => {
    if (!server.listening) {
      resolve();
      return;
    }
    server.close((error) => (error ? reject(error) : resolve()));
  });

const portOf = (server: { address: () => string | AddressInfo | null }): number => {
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("expected a listening TCP address");
  }
  return address.port;
};

// ============================================================================
// Purpose: Exercise the real Vite proxy and an HTTP backend across normal and
//   Expect: 100-continue requests, proving headers are safe before setupOutgoing.
// Database/ORM: None; the backend fixture records only request metadata.
// Standards: Use real TCP servers, drain request bodies, assert trusted-header
//   replacement and unrelated-header preservation, and prove the token stays
//   out of browser-served source.
// Blast Radius: Test-only development proxy coverage; no production or data
//   state is changed.
// Connections:
//   - File: frontend/src/devProxy.ts -> proxy boundary under test.
//   - File: frontend/vite.config.ts -> production Vite wiring of the proxy.
//   - File: backend/ums_smart_revenue/api/dependencies.py -> trusted-header
//     consumer represented by the receiving HTTP fixture.
// ============================================================================
const requestThroughVite = (
  port: number,
  route: string,
  expectContinue: boolean,
): Promise<HttpResponse> =>
  new Promise((resolve, reject) => {
    const body = expectContinue ? JSON.stringify({ probe: "expect-continue" }) : "";
    const headers: Record<string, string> = Object.fromEntries(ATTACKER_PRELOADED_HEADERS);
    if (expectContinue) {
      headers.Expect = "100-continue";
      headers["Content-Type"] = "application/json";
      headers["Content-Length"] = String(Buffer.byteLength(body));
    }
    const request = httpRequest(
      {
        host: "127.0.0.1",
        port,
        path: route,
        method: expectContinue ? "POST" : "GET",
        headers,
      },
      (response) => {
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => chunks.push(chunk));
        response.on("end", () => {
          resolve({
            statusCode: response.statusCode ?? 0,
            body: Buffer.concat(chunks).toString("utf8"),
          });
        });
      },
    );
    request.once("error", reject);
    if (expectContinue) {
      request.once("continue", () => request.end(body));
      request.flushHeaders();
    } else {
      request.end();
    }
  });

type ProxyBoundaryCase = {
  route: string;
  expectContinue: boolean;
};

type ViteServer = Awaited<ReturnType<typeof createViteServer>>;

type RunningViteProxy = {
  server: ViteServer;
  port: number;
};

type ViteProxyStartupDependencies = {
  createServer: (
    config: Parameters<typeof createViteServer>[0],
  ) => Promise<ViteServer>;
  resolvePort: typeof portOf;
};

const DEFAULT_VITE_PROXY_STARTUP_DEPENDENCIES: ViteProxyStartupDependencies = {
  createServer: createViteServer,
  resolvePort: portOf,
};

/** Create the real HTTP backend fixture that records forwarded request metadata. */
const createRecordingBackend = (backendRequests: BackendRequest[]): Server =>
  createHttpServer((request, response) => {
    request.resume();
    request.once("end", () => {
      backendRequests.push({
        method: request.method,
        url: request.url,
        headers: { ...request.headers },
      });
      response.writeHead(200, { "Content-Type": "application/json" });
      response.end(JSON.stringify({ ok: true }));
    });
  });

/** Close an owned Vite server and watcher before preserving its startup failure. */
const rethrowAfterViteStartupCleanup = async (
  server: ViteServer | undefined,
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
      "Vite proxy startup failed and cleanup also failed",
    );
  }
  throw startupError;
};

/** Start a real Vite proxy on loopback and return its verified listening port. */
const startRealViteProxy = async (
  backendPort: number,
  gatewayHeaders: [string, string][],
  dependencies: ViteProxyStartupDependencies = DEFAULT_VITE_PROXY_STARTUP_DEPENDENCIES,
): Promise<RunningViteProxy> => {
  // FIX: Startup owns the Vite server until handoff; close it and its watcher
  // before rethrowing any listen or port-resolution failure.
  let server: ViteServer | undefined;
  try {
    server = await dependencies.createServer({
      root: path.resolve(HERE, ".."),
      configFile: false,
      logLevel: "silent",
      resolve: {
        alias: {
          "@": path.resolve(HERE, "..", "src"),
        },
      },
      server: {
        host: "127.0.0.1",
        port: 0,
        strictPort: true,
        proxy: buildTenantScopedProxy(
          TENANT_SCOPED_ROUTES,
          `http://127.0.0.1:${backendPort}`,
          gatewayHeaders,
        ),
      },
    });
    await server.listen();
    if (!server.httpServer) {
      throw new Error("expected Vite to expose its listening HTTP server");
    }
    return { server, port: dependencies.resolvePort(server.httpServer) };
  } catch (startupError) {
    return rethrowAfterViteStartupCleanup(server, startupError);
  }
};

/** Assert that every configured gateway value replaced the browser's value. */
const expectConfiguredGatewayHeaders = (
  forwarded: BackendRequest | undefined,
  gatewayHeaders: readonly [string, string][],
  route: string,
): void => {
  for (const [header, value] of gatewayHeaders) {
    expect(forwarded?.headers[header.toLowerCase()], `${route} ${header}`).toBe(value);
  }
};

/** Assert that unrelated headers survive while every attacker claim is scrubbed. */
const expectAttackerHeadersScrubbed = (forwarded: BackendRequest | undefined): void => {
  for (const [header, attackerValue] of ATTACKER_PRELOADED_HEADERS) {
    if (header === "X-Unrelated-Header") {
      expect(forwarded?.headers[header.toLowerCase()]).toBe("preserve-me");
    } else {
      expect(forwarded?.headers[header.toLowerCase()]).not.toBe(attackerValue);
    }
  }
};

/** Assert the complete backend observation for one normal or Expect request. */
const expectForwardedRequest = (
  forwarded: BackendRequest | undefined,
  testCase: ProxyBoundaryCase,
  gatewayHeaders: readonly [string, string][],
): void => {
  expect(forwarded?.method, testCase.route).toBe(testCase.expectContinue ? "POST" : "GET");
  expect(forwarded?.url, testCase.route).toBe(testCase.route);
  expect(forwarded?.headers.expect, testCase.route).toBe(
    testCase.expectContinue ? "100-continue" : undefined,
  );
  expectConfiguredGatewayHeaders(forwarded, gatewayHeaders, testCase.route);
  expectAttackerHeadersScrubbed(forwarded);
};

/** Send every real-boundary case and verify the backend received one safe request. */
const exerciseRealProxyCases = async (
  vitePort: number,
  cases: readonly ProxyBoundaryCase[],
  backendRequests: BackendRequest[],
  gatewayHeaders: readonly [string, string][],
): Promise<void> => {
  for (const [index, testCase] of cases.entries()) {
    const result = await requestThroughVite(vitePort, testCase.route, testCase.expectContinue);
    expect(result.statusCode, testCase.route).toBe(200);
    expect(backendRequests).toHaveLength(index + 1);
    expectForwardedRequest(backendRequests.at(-1), testCase, gatewayHeaders);
  }
};

/** Close a Vite fixture only when startup completed successfully. */
const closeViteProxy = async (vite: RunningViteProxy | undefined): Promise<void> => {
  if (vite) {
    await vite.server.close();
  }
};

describe("dev proxy route coverage (derived from frontend/src)", () => {
  it("still finds the API calls we know exist, so the scan cannot pass vacuously", () => {
    for (const prefix of SCANNER_HEALTH_PREFIXES) {
      expect(REQUESTED_PREFIXES, `scanner lost ${prefix}`).toContain(prefix);
    }
  });

  it("treats a + concatenated fragment as a fragment, not a backend prefix", () => {
    const source = [
      "const p =",
      `  \`/revenue/channels/\${encodeURIComponent(id)}\` +`,
      `  \`/months/\${encodeURIComponent(month)}/explain\`;`,
    ].join("\n");
    const scanned = scanStringLiterals(source);
    expect(scanned.map((literal) => literal.continuesExpression)).toEqual([false, true]);
    expect(REQUESTED_PREFIXES).not.toContain("/months");
  });

  it("ignores API paths that appear only in comments", () => {
    const source = "// GET /reports/raw-files is documented here\nconst x = 1;\n";
    expect(scanStringLiterals(source)).toEqual([]);
  });

  it("does not hide a request path after division following a string literal", () => {
    const source = 'const ratio = "x" / 2;\nconst requestPath = "/hidden";';
    expect(scanStringLiterals(source)).toContainEqual({
      value: "/hidden",
      continuesExpression: false,
    });
  });

  it("ignores regex literals while retaining later request strings", () => {
    const source = 'const pattern = /"\\/hidden"/;\nconst requestPath = "/visible";';
    expect(scanStringLiterals(source)).toContainEqual({
      value: "/visible",
      continuesExpression: false,
    });
    expect(scanStringLiterals(source).some((literal) => literal.value === "/hidden")).toBe(false);
  });

  it("proxies every backend prefix the application requests", () => {
    expect(uncoveredPrefixes(REQUESTED_PREFIXES, TENANT_SCOPED_ROUTES)).toEqual([]);
  });

  it.each(REQUESTED_PREFIXES)("fails when %s is omitted from the route list", (prefix) => {
    const withoutPrefix = TENANT_SCOPED_ROUTES.filter((route) => route !== prefix);
    expect(uncoveredPrefixes(REQUESTED_PREFIXES, withoutPrefix)).toEqual([prefix]);
  });
});

describe("dev proxy trust and header contracts", () => {
  it("resolves all seven configured forwarded headers", () => {
    expect(resolveGatewayHeaders(ALL_GATEWAY_ENV)).toEqual(ALL_GATEWAY_HEADERS);
  });

  it("omits blank optional headers, including a missing gateway token", () => {
    const headers = resolveGatewayHeaders({ ...ALL_GATEWAY_ENV, UMS_TRUSTED_GATEWAY_TOKEN: "" });
    expect(headers).not.toContainEqual(["X-UMS-Trusted-Gateway-Token", ""]);
    expect(headers).toContainEqual(["X-User-ID", ALL_GATEWAY_HEADERS[0][1]]);
  });

  it("accepts loopback targets and rejects a remote target without explicit trust", () => {
    expect(resolveDevBackendTarget("http://127.0.0.1:8000")).toBe("http://127.0.0.1:8000");
    expect(resolveDevBackendTarget("http://[::1]:8000")).toBe("http://[::1]:8000");
    expect(() => resolveDevBackendTarget("https://api.example.test")).toThrow(/loopback/iu);
  });

  it("accepts only an exact explicitly trusted remote origin", () => {
    expect(
      resolveDevBackendTarget("https://api.example.test/v1", ["https://api.example.test"]),
    ).toBe("https://api.example.test/v1");
    expect(() =>
      resolveDevBackendTarget("https://api.example.test/v1", ["https://other.example.test"]),
    ).toThrow(/loopback/iu);
    expect(() =>
      resolveDevBackendTarget("http://api.example.test/v1", ["http://api.example.test"]),
    ).toThrow(/https/iu);
    expect(() =>
      resolveDevBackendTarget("https://api.example.test/v1", ["http://api.example.test"]),
    ).toThrow(/https/iu);
    expect(() => resolveDevBackendTarget("https://user:password@api.example.test")).toThrow(
      /credentials/iu,
    );
  });

  it("canonicalizes IDNA origins before applying the exact trust check", () => {
    const unicodeOrigin = new URL("https://例え.テスト").origin;
    const asciiOrigin = new URL("https://xn--r8jz45g.xn--zckzah").origin;
    expect(unicodeOrigin).toBe(asciiOrigin);
    expect(resolveDevBackendTarget("https://例え.テスト/v1", [asciiOrigin])).toBe(
      `${asciiOrigin}/v1`,
    );
  });

  it("injects every configured header into every tenant-scoped proxy entry", () => {
    const proxy = buildTenantScopedProxy(TENANT_SCOPED_ROUTES, BACKEND_TARGET, ALL_GATEWAY_HEADERS);
    expect(Object.keys(proxy)).toEqual(EXPECTED_ROUTES);
    for (const route of EXPECTED_ROUTES) {
      const entry = asConfigurableEntry(proxy[route], route);
      expect(entry.target).toBe(BACKEND_TARGET);
      expect(entry.changeOrigin).toBe(true);
      expect(injectedHeaders(entry, route)).toEqual(ALL_GATEWAY_HEADERS);
    }
  });

  it("refuses to construct a token-forwarding proxy for an untrusted target", () => {
    expect(() =>
      buildTenantScopedProxy(TENANT_SCOPED_ROUTES, "https://api.example.test", ALL_GATEWAY_HEADERS),
    ).toThrow(/loopback/iu);
    const trustedProxy = buildTenantScopedProxy(
      TENANT_SCOPED_ROUTES,
      "https://api.example.test",
      ALL_GATEWAY_HEADERS,
      ["https://api.example.test"],
    );
    expect(asConfigurableEntry(trustedProxy["/tenants"], "/tenants").target).toBe(
      "https://api.example.test",
    );
  });

  it("scrubs attacker-preloaded trusted headers on every route before setting values", () => {
    const blankOptionalHeaders = resolveGatewayHeaders({
      ...ALL_GATEWAY_ENV,
      UMS_TRUSTED_GATEWAY_TOKEN: "",
      VITE_DEV_GATEWAY_SCOPE_ID: "",
    });
    const proxy = buildTenantScopedProxy(TENANT_SCOPED_ROUTES, BACKEND_TARGET, blankOptionalHeaders);
    const configuredByName = new Map(
      blankOptionalHeaders.map(([header, value]) => [header.toLowerCase(), value]),
    );

    for (const route of EXPECTED_ROUTES) {
      const result = probeProxyRequest(
        asConfigurableEntry(proxy[route], route),
        route,
        ATTACKER_PRELOADED_HEADERS,
      );
      const remaining = new Map(result.remaining.map(([header, value]) => [header.toLowerCase(), value]));
      expect(result.configured).toEqual(blankOptionalHeaders);
      expect(remaining.get("x-unrelated-header")).toBe("preserve-me");
      expect(remaining.size).toBe(blankOptionalHeaders.length + 1);
      for (const [header, attackerValue] of ATTACKER_PRELOADED_HEADERS) {
        if (header === "X-Unrelated-Header") {
          continue;
        }
        const normalizedHeader = header.toLowerCase();
        if (configuredByName.has(normalizedHeader)) {
          expect(remaining.get(normalizedHeader), `${route} retained attacker ${header}`).toBe(
            configuredByName.get(normalizedHeader),
          );
        } else {
          expect(remaining.has(normalizedHeader), `${route} retained ${header}`).toBe(false);
        }
        expect(remaining.get(normalizedHeader)).not.toBe(attackerValue);
      }
      const firstSet = result.operations.findIndex((operation) => operation.startsWith("set:"));
      const lastRemove = result.operations.reduce(
        (last, operation, index) => (operation.startsWith("remove:") ? index : last),
        -1,
      );
      expect(lastRemove).toBeGreaterThanOrEqual(0);
      expect(lastRemove).toBeLessThan(firstSet);
    }
  });

  it("preserves a Vite creation error when no server was returned to close", async () => {
    const startupError = new Error("synthetic Vite creation failure");
    const createServer = vi.fn(async () => {
      throw startupError;
    });
    const resolvePort = vi.fn(portOf);

    await expect(
      startRealViteProxy(8000, ALL_GATEWAY_HEADERS, { createServer, resolvePort }),
    ).rejects.toBe(startupError);
    expect(resolvePort).not.toHaveBeenCalled();
  });

  it("closes the Vite server and watcher when listen fails before handoff", async () => {
    const startupError = new Error("synthetic Vite listen failure");
    const close = vi.fn(async () => undefined);
    const server = {
      close,
      httpServer: null,
      listen: vi.fn(async () => {
        throw startupError;
      }),
    } as unknown as ViteServer;
    const createServer = vi.fn(async () => server);

    await expect(
      startRealViteProxy(8000, ALL_GATEWAY_HEADERS, { createServer, resolvePort: portOf }),
    ).rejects.toBe(startupError);
    expect(close).toHaveBeenCalledOnce();
  });

  it("closes the Vite server and watcher when port resolution fails before handoff", async () => {
    const startupError = new Error("synthetic Vite port failure");
    let createdServer: ViteServer | undefined;
    const createServer: ViteProxyStartupDependencies["createServer"] = async (config) => {
      createdServer = await createViteServer(config);
      return createdServer;
    };
    const resolvePort = vi.fn(() => {
      throw startupError;
    });

    await expect(
      startRealViteProxy(8000, ALL_GATEWAY_HEADERS, { createServer, resolvePort }),
    ).rejects.toBe(startupError);
    if (!createdServer) {
      throw new Error("expected the startup fixture to create a Vite server");
    }
    const watcher = createdServer.watcher as typeof createdServer.watcher & { closed: boolean };
    expect(resolvePort).toHaveBeenCalledWith(createdServer.httpServer);
    expect(createdServer.httpServer?.listening).toBe(false);
    expect(watcher.closed).toBe(true);
  });

  it("scrubs Expect requests at the real Vite/backend boundary", async () => {
    const backendRequests: BackendRequest[] = [];
    const backend = createRecordingBackend(backendRequests);
    const backendPort = await listenOnLoopback(backend);
    const gatewayToken = "real-vite-server-only-secret";
    const gatewayHeaders: [string, string][] = [
      ["X-User-ID", "real-user"],
      ["X-User-Email", "real@example.test"],
      ["X-Role", "finance_admin"],
      ["X-Scope-Type", "company"],
      ["X-UMS-Trusted-Gateway-Token", gatewayToken],
      ["X-UMS-Tenant", "ums"],
      ["X-Scope-ID", "company-tv"],
    ];
    const cases: ProxyBoundaryCase[] = [
      { route: "/tenants", expectContinue: false },
      { route: "/revenue", expectContinue: false },
      { route: "/connectors", expectContinue: true },
      { route: "/finance-close", expectContinue: true },
    ];
    let vite: RunningViteProxy | undefined;
    try {
      vite = await startRealViteProxy(backendPort, gatewayHeaders);
      await exerciseRealProxyCases(vite.port, cases, backendRequests, gatewayHeaders);

      const browserModule = await requestThroughVite(vite.port, "/src/main.tsx", false);
      expect(browserModule.statusCode).toBe(200);
      expect(browserModule.body).not.toContain(gatewayToken);
    } finally {
      await closeViteProxy(vite);
      await closeHttpServer(backend);
    }
  }, 15_000);
});
