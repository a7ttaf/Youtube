import type { ChannelImportResult } from "@/lib/api/types";

/**
 * Serialize a value to Python ``json.dumps(..., sort_keys=True, separators=(',', ':'),
 * ensure_ascii=True)`` form so the SPA can recompute ``display_digest`` from the
 * disclosed preview fields (review #184, C1).
 */
const pythonJsonString = (value: string): string => {
  let out = '"';
  for (let index = 0; index < value.length; index += 1) {
    const code = value.charCodeAt(index);
    if (code === 0x22) {
      out += '\\"';
    } else if (code === 0x5c) {
      out += "\\\\";
    } else if (code < 0x20 || code > 0x7f) {
      out += `\\u${code.toString(16).padStart(4, "0")}`;
    } else {
      out += value[index] ?? "";
    }
  }
  out += '"';
  return out;
};

const pythonCanonicalJson = (value: unknown): string => {
  if (value === null) {
    return "null";
  }
  if (typeof value === "boolean") {
    return value ? "true" : "false";
  }
  if (typeof value === "number") {
    if (!Number.isFinite(value)) {
      throw new TypeError("display digest canonical JSON rejects non-finite numbers");
    }
    return Number.isInteger(value) ? String(value) : JSON.stringify(value);
  }
  if (typeof value === "string") {
    return pythonJsonString(value);
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

// Minimal SHA-256 (RFC 6234) for sync digest verification in the browser bundle.
const sha256Hex = (message: string): string => {
  const msg = new TextEncoder().encode(message);
  const K = new Uint32Array([
    0x428a2f98, 0x71374491, 0xb5c0fbcf, 0xe9b5dba5, 0x3956c25b, 0x59f111f1, 0x923f82a4, 0xab1c5ed5,
    0xd807aa98, 0x12835b01, 0x243185be, 0x550c7dc3, 0x72be5d74, 0x80deb1fe, 0x9bdc06a7, 0xc19bf174,
    0xe49b69c1, 0xefbe4786, 0x0fc19dc6, 0x240ca1cc, 0x2de92c6f, 0x4a7484aa, 0x5cb0a9dc, 0x76f988da,
    0x983e5152, 0xa831c66d, 0xb00327c8, 0xbf597fc7, 0xc6e00bf3, 0xd5a79147, 0x06ca6351, 0x14292967,
    0x27b70a85, 0x2e1b2138, 0x4d2c6dfc, 0x53380d13, 0x650a7354, 0x766a0abb, 0x81c2c92e, 0x92722c85,
    0xa2bfe8a1, 0xa81a664b, 0xc24b8b70, 0xc76c51a3, 0xd192e819, 0xd6990624, 0xf40e3585, 0x106aa070,
    0x19a4c116, 0x1e376c08, 0x2748774c, 0x34b0bcb5, 0x391c0cb3, 0x4ed8aa4a, 0x5b9cca4f, 0x682e6ff3,
    0x748f82ee, 0x78a5636f, 0x84c87814, 0x8cc70208, 0x90befffa, 0xa4506ceb, 0xbef9a3f7, 0xc67178f2,
  ]);
  const H = new Uint32Array([
    0x6a09e667, 0xbb67ae85, 0x3c6ef372, 0xa54ff53a, 0x510e527f, 0x9b05688c, 0x1f83d9ab, 0x5be0cd19,
  ]);
  const bitLen = msg.length * 8;
  const withLen = new Uint8Array(((msg.length + 9 + 63) >> 6) << 6);
  withLen.set(msg);
  withLen[msg.length] = 0x80;
  const view = new DataView(withLen.buffer);
  view.setUint32(withLen.length - 4, bitLen, false);

  const W = new Uint32Array(64);
  for (let offset = 0; offset < withLen.length; offset += 64) {
    for (let index = 0; index < 16; index += 1) {
      W[index] = view.getUint32(offset + index * 4, false);
    }
    for (let index = 16; index < 64; index += 1) {
      const s0 = rightRotate(W[index - 15], 7) ^ rightRotate(W[index - 15], 18) ^ (W[index - 15] >>> 3);
      const s1 = rightRotate(W[index - 2], 17) ^ rightRotate(W[index - 2], 19) ^ (W[index - 2] >>> 10);
      W[index] = (W[index - 16] + s0 + W[index - 7] + s1) >>> 0;
    }
    let [a, b, c, d, e, f, g, h] = H;
    for (let index = 0; index < 64; index += 1) {
      const S1 = rightRotate(e, 6) ^ rightRotate(e, 11) ^ rightRotate(e, 25);
      const ch = (e & f) ^ (~e & g);
      const temp1 = (h + S1 + ch + K[index] + W[index]) >>> 0;
      const S0 = rightRotate(a, 2) ^ rightRotate(a, 13) ^ rightRotate(a, 22);
      const maj = (a & b) ^ (a & c) ^ (b & c);
      const temp2 = (S0 + maj) >>> 0;
      h = g;
      g = f;
      f = e;
      e = (d + temp1) >>> 0;
      d = c;
      c = b;
      b = a;
      a = (temp1 + temp2) >>> 0;
    }
    H[0] = (H[0] + a) >>> 0;
    H[1] = (H[1] + b) >>> 0;
    H[2] = (H[2] + c) >>> 0;
    H[3] = (H[3] + d) >>> 0;
    H[4] = (H[4] + e) >>> 0;
    H[5] = (H[5] + f) >>> 0;
    H[6] = (H[6] + g) >>> 0;
    H[7] = (H[7] + h) >>> 0;
  }
  return Array.from(H, (word) => word.toString(16).padStart(8, "0")).join("");
};

const rightRotate = (value: number, amount: number): number =>
  (value >>> amount) | (value << (32 - amount));

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

/** True when the body carries a digest matching its disclosed plan contents. */
export const displayDigestMatchesDisclosed = (plan: ChannelImportResult): boolean => {
  if (typeof plan.display_digest !== "string" || plan.display_digest === "") {
    return false;
  }
  return computeDisplayDigest(plan) === plan.display_digest;
};
