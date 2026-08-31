import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import ts from "typescript";
import { describe, expect, it } from "vitest";

import {
  TENANT_SCOPED_ROUTES,
  TRUSTED_GATEWAY_HEADERS,
  buildTenantScopedProxy,
  proxyContextForRoute,
} from "../vite.config";

// ============================================================================
// Purpose: Guard the development proxy route and header contracts against
//   omissions without duplicating the implementation as the expected result.
// Database/ORM: None.
// Standards: Derive requested prefixes from the TypeScript compiler AST; keep
//   EXPECTED_ROUTES only as an explicit trust-boundary addition detector.
// Blast Radius: Test-only development proxy coverage.
// Connections:
//   - File: frontend/vite.config.ts -> exports proxy route/build helpers.
//   - File: frontend/src/lib/api -> contains the request literals scanned here.
//   - File: frontend/tests/devProxySecurity.test.ts -> real HTTP boundary tests.
// ============================================================================

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC_DIR = path.resolve(HERE, "..", "src");
const SOURCE_SUFFIXES = [".ts", ".tsx"];

// Change-detector only; the compiler-derived assertion is the coverage proof.
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

// Prefixes we know the application calls today. Their only job is to prove the
// scanner below still works: if a future syntax breaks it, the derived
// assertion would pass vacuously on an empty set, and this fails loudly
// instead. Shrinking this list to make a run green defeats the whole file.
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

// NUL marks a template interpolation and cannot collide with TypeScript source.
const INTERPOLATION = "\u0000";

/** Return whether a literal is the right operand of a possibly parenthesized +. */
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

/** Preserve static template fragments while marking every interpolation boundary. */
const templateValue = (node: ts.TemplateExpression): string =>
  [
    node.head.text,
    ...node.templateSpans.flatMap((span) => [INTERPOLATION, span.literal.text]),
  ].join("");

// ============================================================================
// Purpose: Extract string and template literals through the TypeScript compiler
//   rather than a handwritten approximation of the source grammar.
// Database/ORM: None.
// Standards: Let the compiler distinguish comments, regexes, division, and
//   strings; retain right-hand concatenation metadata so suffixes are not routes.
// Blast Radius: Test-only route coverage.
// Connections:
//   - File: frontend/src/lib/api -> request literals discovered below.
//   - File: frontend/vite.config.ts -> allowlisted proxy routes under test.
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
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      literals.push({
        value: node.text,
        continuesExpression: isRightOperandOfPlus(node),
      });
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
// Standards: Scan all frontend/src TypeScript through the compiler AST and fail
//   whenever a leading-slash request prefix lacks a proxy entry.
// Blast Radius: Test-only coverage; no runtime or authorization effect.
// Connections:
//   - File: frontend/vite.config.ts -> TENANT_SCOPED_ROUTES.
//   - File: frontend/src/lib/api -> request call sites.
// ============================================================================
export const discoverRequestedPrefixes = (): string[] => {
  const found = new Set<string>();
  for (const file of sourceFiles(SRC_DIR)) {
    for (const literal of scanStringLiterals(readFileSync(file, "utf8"))) {
      if (isRequestPathLiteral(literal)) {
        found.add(firstSegment(literal.value));
      }
    }
  }
  return [...found].sort();
};

/** Prefixes the application requests that the given proxy list does not cover. */
export const uncoveredPrefixes = (prefixes: string[], routes: readonly string[]): string[] =>
  prefixes.filter((prefix) => !routes.includes(prefix)).sort();

const REQUESTED_PREFIXES = discoverRequestedPrefixes();

const BACKEND_TARGET = "http://127.0.0.1:8000";

// Deliberately includes the gateway token: the point of the proxy is that this
// header is added in Node and never reaches the browser bundle.
const GATEWAY_HEADERS: [string, string][] = [
  ["X-User-ID", "00000000-0000-0000-0000-0000000000aa"],
  ["X-Role", "finance_admin"],
  ["X-UMS-Trusted-Gateway-Token", "test-token"],
];

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

/** Narrow one built proxy entry, failing loudly rather than skipping an odd shape. */
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

/** Run a route's configure hook and collect its ordered proxyReq mutations. */
const headerMutations = (
  entry: ConfigurableProxyEntry,
  route: string,
): { removed: string[]; injected: [string, string][] } => {
  const removed: string[] = [];
  const injected: [string, string][] = [];
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
    getHeaderNames: () => [],
    removeHeader: (header: string) => {
      removed.push(header);
    },
    setHeader: (header: string, value: string) => {
      injected.push([header, value]);
    },
  });
  return { removed, injected };
};

describe("dev proxy route coverage (derived from frontend/src)", () => {
  it("still finds the API calls we know exist, so the scan cannot pass vacuously", () => {
    for (const prefix of SCANNER_HEALTH_PREFIXES) {
      expect(REQUESTED_PREFIXES, `scanner lost ${prefix}`).toContain(prefix);
    }
  });

  it("treats a `+`-concatenated fragment as a fragment, not a backend prefix", () => {
    // The exact shape from useExplanation.ts. `/months` is a suffix of the
    // /revenue path, and must never be reported as an unproxied prefix.
    // Build `${` via fromCharCode so the fixture avoids template literals,
    // regular-string `${` (JS-0038), and literal concatenation (JS-0096/0246).
    const interpolation = String.fromCharCode(36, 123);
    const source = [
      "const p =",
      ["  `/revenue/channels/", interpolation, "encodeURIComponent(id)}` +"].join(""),
      ["  `/months/", interpolation, "encodeURIComponent(month)}/explain`;"].join(""),
    ].join("\n");
    const scanned = scanStringLiterals(source);
    expect(scanned.map((literal) => literal.continuesExpression)).toEqual([false, true]);
    expect(REQUESTED_PREFIXES).not.toContain("/months");
  });

  it("ignores API paths that appear only in comments", () => {
    const source = '// GET /reports/raw-files is documented here\nconst x = 1;\n';
    expect(scanStringLiterals(source)).toEqual([]);
  });

  it("does not hide a request literal after division following a string", () => {
    const source = 'const ratio = "x" / 2;\nconst requestPath = "/hidden";';
    expect(scanStringLiterals(source)).toContainEqual({
      continuesExpression: false,
      value: "/hidden",
    });
  });

  it("proxies every backend prefix the application requests", () => {
    expect(uncoveredPrefixes(REQUESTED_PREFIXES, TENANT_SCOPED_ROUTES)).toEqual([]);
  });

  it("would have caught the historical /org-units omission", () => {
    // The list exactly as it stood before W0.2. This is the whole point of the
    // rewrite: the previous revision of this file compared TENANT_SCOPED_ROUTES
    // to a hand-copy of itself, so against THIS list it was still green.
    // `/users` does not appear in the expectation because nothing under src/
    // calls it yet — it is proxied ahead of the UI that will.
    const preW02Routes = [
      "/tenants",
      "/session",
      "/revenue",
      "/finance-close",
      "/exports",
      "/connectors",
      "/adsense",
      "/channels",
      "/groups",
      "/audit",
    ];
    expect(uncoveredPrefixes(REQUESTED_PREFIXES, preW02Routes)).toEqual(["/org-units"]);
  });

  // REJECT side of the matrix. One case per prefix the application actually
  // calls: drop that prefix from the list and the check must name it. This is
  // what the old hand-copied assertion could not do — it compared the list to
  // itself, so an omission was invisible on both sides.
  it.each(REQUESTED_PREFIXES)(
    "fails when %s is omitted from TENANT_SCOPED_ROUTES",
    (prefix) => {
      const withoutPrefix = TENANT_SCOPED_ROUTES.filter((route) => route !== prefix);
      expect(uncoveredPrefixes(REQUESTED_PREFIXES, withoutPrefix)).toEqual([prefix]);
    },
  );
});

describe("dev proxy route list", () => {
  it("proxies exactly the tenant-scoped routes (change-detector for additions)", () => {
    expect(TENANT_SCOPED_ROUTES).toEqual(EXPECTED_ROUTES);
  });

  it("includes /org-units so the Registry view's Company/Sector names resolve", () => {
    // Regression guard: RegistryView calls useOrgUnits() on mount. Unproxied,
    // that GET is answered by the dev server itself with an empty 404, the
    // columns show raw ids for the whole session, and the mapping form's
    // company picker holds nothing but its placeholder — with no error
    // surfaced anywhere.
    expect(TENANT_SCOPED_ROUTES).toContain("/org-units");
  });

  it("includes /users, which rides the same trusted-gateway lane", () => {
    // Listed ahead of the UI that will call it, so the first screen to need it
    // does not have to rediscover the unproxied-route 404. It is therefore
    // expected NOT to appear in REQUESTED_PREFIXES yet.
    expect(TENANT_SCOPED_ROUTES).toContain("/users");
  });

  it("builds a header-injecting entry for every route", () => {
    const proxy = buildTenantScopedProxy(TENANT_SCOPED_ROUTES, BACKEND_TARGET, GATEWAY_HEADERS);
    expect(Object.keys(proxy)).toEqual(EXPECTED_ROUTES.map(proxyContextForRoute));
    for (const route of EXPECTED_ROUTES) {
      const entry = asConfigurableEntry(proxy[proxyContextForRoute(route)], route);
      expect(entry.target).toBe(BACKEND_TARGET);
      expect(entry.changeOrigin).toBe(true);
      expect(headerMutations(entry, route)).toEqual({
        removed: TRUSTED_GATEWAY_HEADERS,
        injected: GATEWAY_HEADERS,
      });
    }
  });
});
