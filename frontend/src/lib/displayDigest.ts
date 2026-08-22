import type { ChannelImportResult } from "@/lib/api/types";

const rightRotate = (value: number, amount: number): number =>
  (value >>> amount) | (value << (32 - amount));

const JSON_CHAR_ESCAPES: Readonly<Record<number, string>> = {
  0x08: "\\b",
  0x09: "\\t",
  0x0a: "\\n",
  0x0c: "\\f",
  0x0d: "\\r",
  0x22: '\\"',
  0x5c: "\\\\",
};

const escapeJsonCodePoint = (code: number): string | null => {
  const known = JSON_CHAR_ESCAPES[code];
  if (known !== undefined) {
    return known;
  }
  if (code < 0x20 || code >= 0x7f) {
    return `\\u${code.toString(16).padStart(4, "0")}`;
  }
  return null;
};

/**
 * Serialize a string to Python ``json.dumps(..., ensure_ascii=True)`` form.
 * Control characters use JSON short escapes; other non-ASCII uses ``\uXXXX``.
 */
const pythonJsonString = (value: string): string => {
  let out = '"';
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    const escaped = escapeJsonCodePoint(code);
    out += escaped ?? value[index] ?? "";
  }
  out += '"';
  return out;
};

const serializeNumber = (value: number): string => {
  if (!Number.isFinite(value)) {
    throw new TypeError("display digest canonical JSON rejects non-finite numbers");
  }
  return Number.isInteger(value) ? String(value) : JSON.stringify(value);
};

const SCALAR_SERIALIZERS: ReadonlyArray<(value: unknown) => string | null> = [
  (value) => (value === null ? "null" : null),
  (value) => (typeof value === "boolean" ? (value ? "true" : "false") : null),
  (value) => (typeof value === "number" ? serializeNumber(value) : null),
  (value) => (typeof value === "string" ? pythonJsonString(value) : null),
];

const canonicalizeScalar = (value: unknown): string | null => {
  for (const serialize of SCALAR_SERIALIZERS) {
    const result = serialize(value);
    if (result !== null) {
      return result;
    }
  }
  return null;
};

const pythonCanonicalJson = (value: unknown): string => {
  const scalar = canonicalizeScalar(value);
  if (scalar !== null) {
    return scalar;
  }
  if (Array.isArray(value)) {
    return `[${value.map((entry) => pythonCanonicalJson(entry)).join(",")}]`;
  }
  if (typeof value === "object") {
    const record = value as Record<string, unknown>;
    const keys = Object.keys(record).sort();
    return `{${keys.map((key) => `${pythonJsonString(key)}:${pythonCanonicalJson(record[key])}`).join(",")}}`;
  }
  throw new TypeError("display digest canonical JSON rejects unsupported values");
};

// ============================================================================
// Purpose: RFC 6234 SHA-256 over UTF-8 bytes for display_digest verification.
// Database/ORM: None (frontend) — pure digest primitive; no I/O.
// Standards: Sync implementation kept for unit-test fixtures and pinned
//   digests; production verification uses sha256HexAsync (Web Crypto) so
//   hashing does not block the main thread on large canonical payloads.
// Blast Radius: Import preview/apply binding — a wrong digest rejects a
//   plan the operator would otherwise trust (review #184, C1).
// Connections:
// - File: frontend/src/lib/displayDigest.ts -> computeDisplayDigest(Async).
// - File: backend/ums_smart_revenue/api/channels.py -> _display_digest recipe.
// - File: Docs/12_BACKEND_API_SPEC.md -> import plan contract.
// ============================================================================
const sha256Hex = (message: string): string => {
  const msg = new TextEncoder().encode(message);
  const roundConstants = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const state = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const bitLen = msg.length * 8;
  const withLen = new Uint8Array(((msg.length + 9 + 63) >> 6) << 6);
  withLen.set(msg);
  withLen[msg.length] = 0x80;
  const view = new DataView(withLen.buffer);
  view.setUint32(withLen.length - 4, bitLen, false);

  const schedule = new Uint32Array(64);
  for (let offset = 0; offset < withLen.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      schedule[index] = view.getUint32(offset + index * 4, false);
    }
    for (let index = 16; index < 64; index += 1) {
      const s0 =
        rightRotate(schedule[index - 15], 7) ^
        rightRotate(schedule[index - 15], 18) ^
        (schedule[index - 15] >>> 3);
      const s1 =
        rightRotate(schedule[index - 2], 17) ^
        rightRotate(schedule[index - 2], 19) ^
        (schedule[index - 2] >>> 10);
      schedule[index] = (schedule[index - 16] + s0 + schedule[index - 7] + s1) >>> 0;
    }
    let [word0, word1, word2, word3, word4, word5, word6, word7] = state;
    for (let index = 0; index < 64; index += 1) {
      const S1 = rightRotate(word4, 6) ^ rightRotate(word4, 11) ^ rightRotate(word4, 25);
      const ch = (word4 & word5) ^ (~word4 & word6);
      const temp1 = (word7 + S1 + ch + roundConstants[index] + schedule[index]) >>> 0;
      const S0 = rightRotate(word0, 2) ^ rightRotate(word0, 13) ^ rightRotate(word0, 22);
      const maj = (word0 & word1) ^ (word0 & word2) ^ (word1 & word2);
      const temp2 = (S0 + maj) >>> 0;
      word7 = word6;
      word6 = word5;
      word5 = word4;
      word4 = (word3 + temp1) >>> 0;
      word3 = word2;
      word2 = word1;
      word1 = word0;
      word0 = (temp1 + temp2) >>> 0;
    }
    state[0] = (state[0] + word0) >>> 0;
    state[1] = (state[1] + word1) >>> 0;
    state[2] = (state[2] + word2) >>> 0;
    state[3] = (state[3] + word3) >>> 0;
    state[4] = (state[4] + word4) >>> 0;
    state[5] = (state[5] + word5) >>> 0;
    state[6] = (state[6] + word6) >>> 0;
    state[7] = (state[7] + word7) >>> 0;
  }
  return Array.from(state, (word) => word.toString(16).padStart(8, "0")).join("");
};

const sha256HexAsync = async (message: string): Promise<string> => {
  const msg = new TextEncoder().encode(message);
  const hashBuffer = await crypto.subtle.digest("SHA-256", msg);
  return Array.from(new Uint8Array(hashBuffer), (byte) =>
    byte.toString(16).padStart(2, "0"),
  ).join("");
};

// ============================================================================
// Purpose: Recompute the server display_digest from exactly the disclosed plan
//   fields so the SPA can verify a 2xx body before trusting preview state.
// Database/ORM: None (frontend) — pure canonical JSON + SHA-256 over the four
//   disclosed import-plan fields mirrored in Docs/12.
// Standards: Python json.dumps(sort_keys=True, separators=(',', ':'),
//   ensure_ascii=True) byte match; sync SHA-256 in-browser.
// Blast Radius: Import preview/apply binding — rejects substituted rows while
//   echoing an unverified digest token (review #184, C1).
// Connections:
// - File: backend/ums_smart_revenue/api/channels.py -> _display_digest recipe.
// - File: frontend/src/lib/api/useChannelImport.ts -> isChannelImportResult.
// - File: Docs/12_BACKEND_API_SPEC.md -> import plan contract.
// ============================================================================
/** Recompute the server ``display_digest`` from exactly the disclosed plan fields. */
export const computeDisplayDigest = (plan: Pick<
  ChannelImportResult,
  "content_owner_id" | "cms_status" | "counts" | "rows"
>): string => {
  const canonical = pythonCanonicalJson({
    content_owner_id: plan.content_owner_id,
    cms_status: plan.cms_status,
    counts: plan.counts,
    rows: plan.rows,
  });
  return sha256Hex(canonical);
};

/** Async variant using Web Crypto so hashing stays off the synchronous hot path. */
export const computeDisplayDigestAsync = async (
  plan: Pick<ChannelImportResult, "content_owner_id" | "cms_status" | "counts" | "rows">,
): Promise<string> => {
  const canonical = pythonCanonicalJson({
    content_owner_id: plan.content_owner_id,
    cms_status: plan.cms_status,
    counts: plan.counts,
    rows: plan.rows,
  });
  return await sha256HexAsync(canonical);
};

// ============================================================================
// Purpose: Verify a plan's display_digest matches a client-side recomputation
//   from its disclosed fields before the import flow trusts preview/apply state.
// Database/ORM: None (frontend) — boolean gate over canonical JSON + SHA-256.
// Standards: Fail closed — empty/missing digest returns false; recomputation
//   exceptions return false instead of throwing through isChannelImportResult.
// Blast Radius: Import preview/apply binding — blocks malformed 2xx payloads
//   that echo an unverified digest while substituting disclosed rows (review
//   #184, C1).
// Connections:
// - File: frontend/src/lib/displayDigest.ts -> computeDisplayDigest recipe.
// - File: frontend/src/lib/api/useChannelImport.ts -> isChannelImportResult.
// - File: Docs/12_BACKEND_API_SPEC.md -> import plan contract.
// ============================================================================
/** True when the body carries a digest matching its disclosed plan contents. */
export const displayDigestMatchesDisclosed = (plan: ChannelImportResult): boolean => {
  if (typeof plan.display_digest !== "string" || plan.display_digest === "") {
    return false;
  }
  try {
    return computeDisplayDigest(plan) === plan.display_digest;
  } catch {
    return false;
  }
};

/** Async verify path for response validation before trusted UI state updates. */
export const displayDigestMatchesDisclosedAsync = async (
  plan: ChannelImportResult,
): Promise<boolean> => {
  if (typeof plan.display_digest !== "string" || plan.display_digest === "") {
    return false;
  }
  try {
    return (await computeDisplayDigestAsync(plan)) === plan.display_digest;
  } catch {
    return false;
  }
};
