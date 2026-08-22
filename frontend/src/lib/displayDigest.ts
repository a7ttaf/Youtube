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

const rejectAllWorkerWaiters = (error: Error): void => {
  for (const waiter of workerWaiters.values()) {
    waiter.reject(error);
  }
  workerWaiters.clear();
};

const disableDigestWorker = (): void => {
  if (digestWorker) {
    digestWorker.onmessage = null;
    digestWorker.onerror = null;
    digestWorker.onmessageerror = null;
    digestWorker.terminate();
  }
  digestWorker = null;
};

const handleDigestWorkerFailure = (message: string): void => {
  rejectAllWorkerWaiters(new Error(message));
  disableDigestWorker();
};

// ============================================================================
// Purpose: Register Worker message/error listeners for digest request correlation.
// Database/ORM: None (frontend) — wires onmessage / onerror / onmessageerror only.
// Standards: Correlate responses by request id; on worker/messageerror reject all
//   pending waiters, terminate the worker, and clear the cache so the next call
//   falls back to sync computeDisplayDigestFromFields (via getDigestWorker null
//   path or recreate). Unknown response ids are ignored (no throw).
// Blast Radius: Import preview/apply binding — a hung or crashed worker without
//   these handlers would strand displayDigestMatchesDisclosedAsync and leak
//   workerWaiters (review #184, C1; Qodo worker-hang finding).
// Connections:
//   - File: frontend/src/lib/displayDigest.worker.ts -> posts {id,digest|error}.
//   - File: frontend/src/lib/displayDigest.ts -> computeDisplayDigestInWorker
//     and handleDigestWorkerFailure / disableDigestWorker.
//   - File: frontend/src/lib/api/useChannelImport.ts -> assertUsableResult.
// ============================================================================
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
  worker.onerror = () => {
    handleDigestWorkerFailure("display digest worker failed");
  };
  worker.onmessageerror = () => {
    handleDigestWorkerFailure("display digest worker message error");
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

// ============================================================================
// Purpose: Worker orchestration for off-main-thread display_digest compute.
// Database/ORM: None (frontend) — spawns worker, correlates requests, handles failures.
// Standards: Lazy worker init; on worker/postMessage failure rejects waiters and
//   falls back to sync computeDisplayDigestFromFields on the main thread.
// Blast Radius: Import preview/apply binding — worker path must match sync recipe
//   (review #184, C1).
// Connections: worker entry, shared recipe, and the async verify callers below.
//   - File: frontend/src/lib/displayDigest.worker.ts -> onmessage handler.
//   - File: frontend/src/lib/displayDigestCore.ts -> computeDisplayDigestFromFields.
//   - File: frontend/src/lib/api/useChannelImport.ts -> assertUsableResult verify.
// ============================================================================
const computeDisplayDigestInWorker = async (plan: DisplayDigestPlanFields): Promise<string> => {
  const worker = getDigestWorker();
  if (!worker) {
    await yieldToMainThread();
    return computeDisplayDigestFromFields(plan);
  }
  const id = nextWorkerRequestId + 1;
  nextWorkerRequestId = id;
  try {
    return await new Promise<string>((resolve, reject) => {
      workerWaiters.set(id, { resolve, reject });
      // FIX: postMessage can throw synchronously (e.g. unusable data channel);
      // drop this request's waiter before that rejection unwinds so the map
      // never accumulates stale entries (PR #195 review, worker waiter leak).
      try {
        worker.postMessage({ id, plan });
      } catch (error: unknown) {
        workerWaiters.delete(id);
        throw error;
      }
    });
  } catch {
    await yieldToMainThread();
    return computeDisplayDigestFromFields(plan);
  }
};

/**
 * Test-only introspection: the number of unresolved worker waiters. The
 * waiter map is closure-private by design; tests import a fresh module copy
 * (vi.resetModules) and read this after a failed postMessage to prove no
 * stale waiter entry leaks (PR #195 review). Undefined outside tests.
 */
export const __workerWaiterCount = (): number => workerWaiters.size;

// ============================================================================
// Purpose: Recompute the server display_digest from exactly the disclosed plan
//   fields so the SPA can verify a 2xx body before trusting preview state.
// Database/ORM: None (frontend) — pure canonical JSON + SHA-256 over the four
//   disclosed import-plan fields mirrored in Docs/12.
// Standards: Python json.dumps(sort_keys=True, separators=(',', ':'),
//   ensure_ascii=True) byte match; sync SHA-256 in-browser.
// Blast Radius: Import preview/apply binding — rejects substituted rows while
//   echoing an unverified digest token (review #184, C1).
// Connections: backend recipe and the verify gates that consume the digest.
//   - File: backend/ums_smart_revenue/api/channels.py -> _display_digest recipe.
//   - File: frontend/src/lib/displayDigest.ts -> displayDigestMatchesDisclosed
//     and displayDigestMatchesDisclosedAsync recompute with this exact recipe.
//   - File: Docs/12_BACKEND_API_SPEC.md -> import plan contract.
// ============================================================================
/** Recompute the server ``display_digest`` from exactly the disclosed plan fields. */
export const computeDisplayDigest = (plan: DisplayDigestPlanFields): string =>
  computeDisplayDigestFromFields(plan);

// ============================================================================
// Purpose: Off-main-thread display_digest compute with sync fallback on failure.
// Database/ORM: None (frontend) — worker orchestration over displayDigestCore.
// Standards: Dedicated worker when available; on worker/postMessage failure yields
//   to main thread and falls back to sync computeDisplayDigestFromFields.
// Blast Radius: Import preview/apply binding — digest drift rejects trusted plans
//   (review #184, C1).
// Connections: worker entry, shared recipe, and the production verify callers.
//   - File: frontend/src/lib/displayDigest.worker.ts -> Worker entry point.
//   - File: frontend/src/lib/displayDigestCore.ts -> Canonical JSON + SHA-256.
//   - File: frontend/src/lib/displayDigest.ts -> displayDigestMatchesDisclosedAsync.
//   - File: frontend/src/lib/api/useChannelImport.ts -> assertUsableResult verify.
// ============================================================================
/** Off-main-thread variant: canonicalization + hashing run in a dedicated worker. */
export const computeDisplayDigestAsync = async (
  plan: DisplayDigestPlanFields,
): Promise<string> => {
  return await computeDisplayDigestInWorker(plan);
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
// Connections: test-only sync twin of the async production verify gate.
//   - File: frontend/src/lib/displayDigestCore.ts -> computeDisplayDigestFromFields
//     supplies the shared recipe this gate recomputes with.
//   - File: frontend/src/lib/displayDigest.ts -> displayDigestMatchesDisclosedAsync
//     is the production gate; production callers use that one, not this sync twin.
//   - File: frontend/tests/lib/displayDigest.test.ts -> exercises this gate.
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
// Standards: Fail closed on empty/missing digest or a failed digest
//   recomputation. Worker/postMessage failure does NOT fail the gate by
//   itself — computeDisplayDigestInWorker yields and falls back to sync
//   computeDisplayDigestFromFields; only a failed fallback (or a digest that
//   does not match the disclosed plan) returns false. Plain HTTP / no-Worker
//   contexts use the same sync path after yielding.
// Blast Radius: Import preview/apply binding — blocks malformed 2xx payloads
//   and unverified 409/422 refresh plans from replacing operator-approved state.
// Connections: off-thread compute plus every production verify call site.
//   - File: frontend/src/lib/displayDigest.worker.ts -> off-thread canonicalize+hash.
//   - File: frontend/src/lib/displayDigest.ts -> computeDisplayDigestInWorker
//     owns the worker-failure → sync-fallback recovery path.
//   - File: frontend/src/lib/api/useChannelImport.ts -> assertUsableResult.
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx -> applyRaceDetail.
//   - File: Docs/12_BACKEND_API_SPEC.md -> import plan contract.
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
