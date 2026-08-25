import { readFileSync, readdirSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { describe, expect, it } from "vitest";

import { TENANT_SCOPED_ROUTES, buildTenantScopedProxy } from "../vite.config";

// ============================================================================
// Purpose: Guard the dev proxy's route list against OMISSION, which is the bug
//   class that produced the /org-units defect. A tenant-scoped backend prefix
//   that is NOT in this list is not merely "unproxied" — the request never
//   leaves the dev server. The client sends `Accept: application/json`
//   (client.ts buildHeaders), which Vite's html fallback declines, so the dev
//   server answers with a bare 404, a zero-length body and no Content-Type,
//   and the calling view degrades silently (RegistryView renders raw org-unit
//   ids instead of Company/Sector names and leaves the mapping form's company
//   picker empty). Measured against a real Vite createServer, not assumed:
//   Accept: application/json -> 404 / contentType null / 0 bytes, while
//   Accept: text/html -> 200 / text/html / index.html.
// Database/ORM: None (frontend dev-server config).
// Standards: The load-bearing assertion is DERIVED, not hand-copied. Every
//   leading-slash path literal the application actually issues is scanned out
//   of frontend/src and required to be covered by TENANT_SCOPED_ROUTES, so a
//   new API call to an unproxied prefix fails here instead of failing silently
//   in a browser. The previous revision of this file compared the route list to
//   a duplicate of itself, which could only ever catch removal — it would have
//   passed on the exact defect it was written after.
//   The hand-written EXPECTED_ROUTES list is kept, demoted to what it honestly
//   is: a change-detector for ADDITIONS. Adding a route means injecting
//   trusted-principal headers onto one more prefix, which is a deliberate trust
//   decision that should have to touch a test. It is not evidence of coverage.
//   Two collection hazards this file deliberately avoids:
//     - Its name must not match vitest's default `exclude`, which carries
//       `**/vite.config.*`; naming it after the module under test would have
//       silently collected nothing.
//     - It imports the route list and the builder rather than invoking the
//       default-exported config factory. Vite's own asset transform rewrites
//       `new URL("./src", import.meta.url)` (vite.config.ts, the `@` alias)
//       against `self.location`, so under vitest that factory throws
//       "The URL must be of scheme file" — a transform artifact, not a defect
//       in the config.
// Blast Radius: Dev-server only. No production bundle and no runtime
//   authorization path — outside dev the real gateway injects these headers.
// Connections:
//   - File: frontend/vite.config.ts -> TENANT_SCOPED_ROUTES and
//     buildTenantScopedProxy, the values under test.
//   - File: frontend/src/lib/api/client.ts -> buildHeaders sets the
//     Accept: application/json that makes an unproxied route a bare 404.
//   - File: frontend/src/lib/api/useOrgUnits.ts -> GET /org-units, the call
//     that exposed the gap (RegistryView.tsx:1216 mounts it; the same data
//     feeds the mapping form's company select at RegistryView.tsx:840-845).
//   - File: backend/ums_smart_revenue/api/org_units.py -> the backend route the
//     proxy must reach; backend/ums_smart_revenue/api/users.py for /users.
//   - File: frontend/README.md -> documents the route list and the four
//     mounted-but-unproxied backend prefixes.
// ============================================================================

const HERE = path.dirname(fileURLToPath(import.meta.url));
const SRC_DIR = path.resolve(HERE, "..", "src");
const SOURCE_SUFFIXES = [".ts", ".tsx"];

// Change-detector only — see Standards above. Not the coverage assertion.
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
  /**
   * True when the literal is concatenated onto a preceding expression — the
   * token before it is `+`. Such a literal is a path *fragment*, not a request
   * path: useExplanation.ts builds
   *   `/revenue/channels/${id}` + `/months/${month}/explain` + `?metric=...`
   * so `/months` must not be mistaken for a top-level backend prefix.
   */
  continuesExpression: boolean;
};

const isWhitespace = (ch: string): boolean =>
  ch === " " || ch === "\t" || ch === "\n" || ch === "\r";

/** True where a `/` starts a regex literal rather than division, by the usual heuristic. */
const startsRegex = (previous: string): boolean =>
  previous === "" || !/[)\]}\w$]/.test(previous);

// Placeholder standing in for a `${...}` interpolation inside a template
// literal. NUL cannot occur in real source, and firstSegment treats it as a
// segment terminator, so `/exports${qs}` yields `/exports` rather than a
// fragment carrying the interpolation's text.
const INTERPOLATION = "\u0000";

/** Skip a line or block comment; returns the index just past it. */
const skipComment = (source: string, index: number): number => {
  if (source[index + 1] === "/") {
    let cursor = index;
    while (cursor < source.length && source[cursor] !== "\n") {
      cursor += 1;
    }
    return cursor;
  }
  let cursor = index + 2;
  while (cursor < source.length && !(source[cursor] === "*" && source[cursor + 1] === "/")) {
    cursor += 1;
  }
  return cursor + 2;
};

/** Skip a regex literal, honouring escapes and character classes. */
const skipRegex = (source: string, index: number): number => {
  let cursor = index + 1;
  let inClass = false;
  while (cursor < source.length) {
    const ch = source[cursor];
    if (ch === "\\") {
      cursor += 2;
      continue;
    }
    if (ch === "\n") {
      break;
    }
    if (ch === "[") {
      inClass = true;
    } else if (ch === "]") {
      inClass = false;
    } else if (ch === "/" && !inClass) {
      break;
    }
    cursor += 1;
  }
  return cursor + 1;
};

/**
 * Read one string or template literal starting at its opening quote, recording
 * it, and return the index just past the closing quote. A `${...}` inside a
 * template is scanned as code — so literals nested in an interpolation are
 * collected too — and contributes a single placeholder to the literal's text.
 *
 * Held on a local object with scanRegion so mutual recursion does not hit the
 * Temporal Dead Zone and does not use module-scope `function` (JS-0067/0357).
 */
const scanners: {
  readLiteral: (
    source: string,
    openIndex: number,
    continuesExpression: boolean,
    literals: ScannedLiteral[],
  ) => number;
  scanRegion: (
    source: string,
    start: number,
    stopAtCloseBrace: boolean,
    literals: ScannedLiteral[],
  ) => number;
} = {
  readLiteral(source, openIndex, continuesExpression, literals) {
    const quote = source[openIndex];
    let index = openIndex + 1;
    let value = "";
    while (index < source.length && source[index] !== quote) {
      if (source[index] === "\\") {
        value += source[index + 1] ?? "";
        index += 2;
        continue;
      }
      if (quote === "`" && source[index] === "$" && source[index + 1] === "{") {
        index = scanners.scanRegion(source, index + 2, true, literals);
        value += INTERPOLATION;
        continue;
      }
      value += source[index];
      index += 1;
    }
    literals.push({ value, continuesExpression });
    return index + 1;
  },

  scanRegion(source, start, stopAtCloseBrace, literals) {
    let index = start;
    let depth = 0;
    let previousToken = "";
    while (index < source.length) {
      const ch = source[index];
      const next = source[index + 1] ?? "";
      if (ch === "/" && (next === "/" || next === "*")) {
        index = skipComment(source, index);
        continue;
      }
      if (ch === "/" && startsRegex(previousToken)) {
        index = skipRegex(source, index);
        previousToken = "/";
        continue;
      }
      if (ch === '"' || ch === "'" || ch === "`") {
        index = scanners.readLiteral(source, index, previousToken === "+", literals);
        previousToken = ch;
        continue;
      }
      if (stopAtCloseBrace) {
        if (ch === "{") {
          depth += 1;
        } else if (ch === "}") {
          if (depth === 0) {
            return index + 1;
          }
          depth -= 1;
        }
      }
      if (!isWhitespace(ch)) {
        previousToken = ch;
      }
      index += 1;
    }
    return index;
  },
};

/**
 * Purpose: Extract every string/template literal from TypeScript source,
 *   skipping comments and regex literals, and record whether each literal
 *   continues a `+` concatenation.
 * Standards: A hand-rolled character scan rather than a regex, because comments
 *   in this repository quote API paths ("// GET /revenue/months/{month}/...")
 *   and a regex over raw text cannot tell those from real call sites. Template
 *   interpolations are scanned as code, so a nested template such as
 *   `` `/exports${qs ? `?${qs}` : ""}` `` yields `/exports<placeholder>` rather
 *   than the truncated fragment a naive scan produces. The regex heuristic is
 *   the one place a mis-scan could hide a literal, which is why
 *   SCANNER_HEALTH_PREFIXES exists.
 * Blast Radius: Test-only.
 */
export const scanStringLiterals = (source: string): ScannedLiteral[] => {
  const literals: ScannedLiteral[] = [];
  scanners.scanRegion(source, 0, false, literals);
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

/**
 * The first path segment of a literal, e.g. "/revenue/months/x" -> "/revenue".
 * Split on INTERPOLATION as a string first so no control character appears in a
 * regex (DeepSource JS-0004); then terminate on / ? # only.
 */
const firstSegment = (literal: string): string => {
  const untilPlaceholder = literal.slice(1).split(INTERPOLATION, 1)[0] ?? "";
  return `/${untilPlaceholder.split(/[/?#]/u, 1)[0] ?? ""}`;
};

/**
 * Purpose: Derive the set of backend prefixes the application actually
 *   requests, by scanning frontend/src for leading-slash path literals.
 * Standards: Scans the whole of src/, not only src/lib/api/ — `/session/me`
 *   lives in contexts/SessionContext.tsx and `/tenants/me` in
 *   components/srcc/AppShell.tsx, so an api-directory-only scan would miss two
 *   of the twelve proxied routes and under-report coverage.
 * Blast Radius: Test-only.
 */
export const discoverRequestedPrefixes = (): string[] => {
  const found = new Set<string>();
  for (const file of sourceFiles(SRC_DIR)) {
    for (const literal of scanStringLiterals(readFileSync(file, "utf8"))) {
      if (!literal.value.startsWith("/") || literal.continuesExpression) {
        continue;
      }
      if (literal.value.length < 2 || !/^[a-z]/iu.test(literal.value[1])) {
        continue;
      }
      found.add(firstSegment(literal.value));
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

type ProxyReqHandler = (proxyReq: { setHeader: (header: string, value: string) => void }) => void;

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

/** Run a route's configure hook and collect the headers it injects on proxyReq. */
const injectedHeaders = (entry: ConfigurableProxyEntry, route: string): [string, string][] => {
  const collected: [string, string][] = [];
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
    setHeader: (header: string, value: string) => {
      collected.push([header, value]);
    },
  });
  return collected;
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
    // Build `${` via concatenation so the fixture neither uses an unnecessary
    // template literal (JS-R1004) nor a regular-string `${` token (JS-0038).
    const interpolation = "$" + "{";
    const source = [
      "const p =",
      "  `/revenue/channels/" + interpolation + "encodeURIComponent(id)}` +",
      "  `/months/" + interpolation + "encodeURIComponent(month)}/explain`;",
    ].join("\n");
    const scanned = scanStringLiterals(source);
    expect(scanned.map((literal) => literal.continuesExpression)).toEqual([false, true]);
    expect(REQUESTED_PREFIXES).not.toContain("/months");
  });

  it("ignores API paths that appear only in comments", () => {
    const source = '// GET /reports/raw-files is documented here\nconst x = 1;\n';
    expect(scanStringLiterals(source)).toEqual([]);
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
    expect(Object.keys(proxy)).toEqual(EXPECTED_ROUTES);
    for (const route of EXPECTED_ROUTES) {
      const entry = asConfigurableEntry(proxy[route], route);
      expect(entry.target).toBe(BACKEND_TARGET);
      expect(entry.changeOrigin).toBe(true);
      expect(injectedHeaders(entry, route)).toEqual(GATEWAY_HEADERS);
    }
  });
});
