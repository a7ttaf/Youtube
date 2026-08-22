import type { ChannelImportResult } from "@/lib/api/types";

import {
  computeDisplayDigestFromFields,
  type DisplayDigestPlanFields,
} from "@/lib/displayDigestCore";

const yieldToMainThread = async (): Promise<void> => {
  if (typeof scheduler !== "undefined" && "yield" in scheduler) {
    await (scheduler as Scheduler & { yield: () => Promise<void> }).yield();
    return;
  }
  await new Promise<void>((resolve) => {
    setTimeout(resolve, 0);
  });
};

type DigestWorkerResponse =
  | { id: number; digest: string }
  | { id: number; error: string };

let digestWorker: Worker | null | undefined;
let nextWorkerRequestId = 0;
const workerWaiters = new Map<
  number,
  { resolve: (digest: string) => void; reject: (error: Error) => void }
>();

const ensureDigestWorkerListener = (worker: Worker): void => {
  worker.onmessage = (event: MessageEvent<DigestWorkerResponse>) => {
    const waiter = workerWaiters.get(event.data.id);
    if (!waiter) {
      return;
    }
    workerWaiters.delete(event.data.id);
    if ("error" in event.data) {
      waiter.reject(new Error(event.data.error));
      return;
    }
    waiter.resolve(event.data.digest);
  };
};

const getDigestWorker = (): Worker | null => {
  if (digestWorker !== undefined) {
    return digestWorker;
  }
  if (typeof Worker === "undefined") {
    digestWorker = null;
    return digestWorker;
  }
  try {
    digestWorker = new Worker(new URL("./displayDigest.worker.ts", import.meta.url), {
      type: "module",
    });
    ensureDigestWorkerListener(digestWorker);
  } catch {
    digestWorker = null;
  }
  return digestWorker;
};

const computeDisplayDigestInWorker = async (plan: DisplayDigestPlanFields): Promise<string> => {
  const worker = getDigestWorker();
  if (!worker) {
    await yieldToMainThread();
    return computeDisplayDigestFromFields(plan);
  }
  const id = nextWorkerRequestId + 1;
  nextWorkerRequestId = id;
  return new Promise<string>((resolve, reject) => {
    workerWaiters.set(id, { resolve, reject });
    worker.postMessage({ id, plan });
  });
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
export const computeDisplayDigest = (plan: DisplayDigestPlanFields): string =>
  computeDisplayDigestFromFields(plan);

/** Off-main-thread variant: canonicalization + hashing run in a dedicated worker. */
export const computeDisplayDigestAsync = async (
  plan: DisplayDigestPlanFields,
): Promise<string> => computeDisplayDigestInWorker(plan);

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

// ============================================================================
// Purpose: Production fail-closed verify path before trusted import UI state.
// Database/ORM: None (frontend) — async boolean gate over canonical JSON +
//   SHA-256 executed off the main thread when Workers are available.
// Standards: Fail closed — empty/missing digest returns false; worker or
//   digest failures return false; plain HTTP falls back to sync SHA-256 after
//   yielding so non-secure contexts remain supported.
// Blast Radius: Import preview/apply binding — blocks malformed 2xx payloads
//   and unverified 409/422 refresh plans from replacing operator-approved state.
// Connections:
// - File: frontend/src/lib/displayDigest.worker.ts -> off-thread canonicalize+hash.
// - File: frontend/src/lib/api/useChannelImport.ts -> assertUsableResult.
// - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> applyRaceDetail.
// - File: Docs/12_BACKEND_API_SPEC.md -> import plan contract.
// ============================================================================
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
