// ============================================================================
// Purpose: Own the one audited set of API route roots shared by the browser
//   client and Vite's trusted development proxy.
// Database/ORM: None.
// Standards: Reject absolute/relative paths whose first exact segment is not
//   allowlisted; callers cannot silently add a request the proxy does not cover.
// Blast Radius: Frontend API traffic and development trusted-header proxying.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> validates every resolved request.
//   - File: frontend/devProxy.ts -> builds the proxy from the same roots.
// ============================================================================

export const TENANT_SCOPED_ROUTES = [
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
] as const;

/** Return the request target from an API URL without accepting other schemes. */
const apiRequestTarget = (rawPath: string): string => {
  if (/^https?:\/\//iu.test(rawPath)) {
    const parsed = new URL(rawPath);
    return `${parsed.pathname}${parsed.search}`;
  }
  if (/^[a-z][a-z0-9+.-]*:/iu.test(rawPath) || rawPath.startsWith("//")) {
    throw new Error("API request URL must be relative or use HTTP(S)");
  }
  // FIX: Preserve resolveUrl's documented relative-path contract while still
  // validating the exact normalized request target shared with the proxy.
  return rawPath.startsWith("/") ? rawPath : `/${rawPath}`;
};

// Browser fragments are navigation metadata and are never part of the HTTP
// request target. Strip literal fragments consistently for relative and
// absolute inputs; encoded `%23` remains path data and is rejected below.
const pathPart = (requestUrl: string): string =>
  requestUrl.split(/[?#]/u, 1)[0] ?? "";

/** Return whether a value contains C0 or DEL control characters. */
const hasControlChar = (value: string): boolean => {
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code < 0x20 || code === 0x7f) {
      return true;
    }
  }
  return false;
};

/** Return whether a request target carries a dangerous percent encoding. */
const hasUnsafeEncoding = (value: string): boolean =>
  /%(?:0[0-9a-f]|1[0-9a-f]|23|2f|5c|7f)/iu.test(value);

const UNSAFE_TARGET_CHECKS: ReadonlyArray<(value: string) => boolean> = [
  (value) => !value.startsWith("/"),
  (value) => value.startsWith("//"),
  (value) => value.includes("\\"),
  hasControlChar,
  (value) => value.slice(1).includes("//"),
  hasUnsafeEncoding,
];

/** Return whether a request target carries traversal, control, or split segments. */
const hasUnsafeSegments = (value: string): boolean =>
  UNSAFE_TARGET_CHECKS.some((check) => check(value)) ||
  value.split("/").some((segment) => segment === "." || segment === "..");

/** Decode one candidate layer, returning null when the encoding is malformed. */
const decodeLayer = (candidate: string): string | null => {
  try {
    return decodeURIComponent(candidate);
  } catch {
    return null;
  }
};

/** Return whether the candidate's first path segment is exactly this route. */
const firstSegmentMatches = (candidate: string, route: string): boolean =>
  `/${candidate.slice(1).split("/", 1)[0] ?? ""}` === route;

/** Advance one decode layer: false rejects, true is stable, string continues. */
const nextRouteCandidate = (
  candidate: string,
  route: string,
): string | boolean => {
  if (hasUnsafeSegments(candidate) || !firstSegmentMatches(candidate, route)) {
    return false;
  }
  const decoded = decodeLayer(candidate);
  if (decoded === null) {
    return false;
  }
  return decoded === candidate || decoded;
};

/** Match one exact route root through every supported decode layer. */
export const isSafeRouteUrl = (requestUrl: string, route: string): boolean => {
  let candidate = pathPart(requestUrl);
  for (let decodeDepth = 0; decodeDepth < 5; decodeDepth += 1) {
    const next = nextRouteCandidate(candidate, route);
    if (next === false) {
      return false;
    }
    if (next === true) {
      return true;
    }
    candidate = next;
  }
  return false;
};

/** Fail closed unless one exact shared route root owns this request path. */
export const assertTrustedApiRoute = (rawPath: string): void => {
  const requestTarget = apiRequestTarget(rawPath);
  if (!TENANT_SCOPED_ROUTES.some((route) => isSafeRouteUrl(requestTarget, route))) {
    throw new Error("API request path is outside the audited route roots");
  }
};
