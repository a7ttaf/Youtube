import { useCallback, useMemo, useSyncExternalStore } from "react";

// ============================================================================
// Purpose: Remember that a bulk import's outcome was never established. The
//   import flow raises this BEFORE it dispatches the apply and clears it only
//   once a response settles the question; the registry view renders the
//   warning and keeps "Import CSV" shut while it stands.
// Database/ORM: None (frontend state only).
// Standards: Held in a MODULE-level store, not component state and not a
//   context value, because every narrower home has a hole. Component state
//   dies with the flow — an operator who leaves by the sidebar never runs its
//   exit handler. View state dies with the view — the notice's own advice is
//   to open the Audit trail, which unmounts Registry. A provider fixes those
//   two but still leaves consumers under NO provider each holding a private
//   copy, so the flow's raise never reaches the view's notice. One store, read
//   through useSyncExternalStore, has none of those seams (review #184).
//   Mirrored into localStorage so it also survives the DOCUMENT: a closed tab
//   or a reload discards the pending fetch while the backend goes on
//   committing. localStorage rather than sessionStorage precisely because
//   sessionStorage is per-tab and a second tab is one of the cases to cover;
//   a `storage` listener carries a raise or an acknowledgement between tabs.
//   Storage is a MIRROR. When a browser refuses it (private mode, blocked
//   cookies, quota), the in-memory flag governs, so the failure mode is a
//   guard that no longer crosses documents — never one that silently fails
//   OPEN on the control it protects.
//   Tracks pending applies BY IDENTITY, not as one boolean. Two tabs can each
//   hold a preview, and a single flag cannot tell their requests apart: the
//   first to settle would clear the other's protection, so a later lost
//   response would find the guard already down (review #184, codex P1). Each
//   apply carries its own id; settling removes only that id.
//   One KEY PER APPLY, never a shared array. An array needs read-modify-write,
//   and that is not atomic across documents: two tabs dispatching before
//   either sees the other's storage event both read the same list and the
//   second setItem drops the first id — after which one settle takes the guard
//   down while a genuinely unknown write is still outstanding. Per-key writes
//   touch only the entry they own, so there is no shared cell to lose a write
//   on and nothing to serialize (review #184, codex P1).
//   Cleared ONLY by an established outcome: a 2xx apply, a definite rejection,
//   or the operator stating they have checked the audit trail — which clears
//   ALL of them, because that is the claim the operator is actually making. A
//   registry reload must not clear anything, because a fresh GET cannot prove
//   the write landed.
//   Not a substitute for durable server-side idempotency and does not claim to
//   be — it is a client-side guard on one button. The authoritative record of
//   what committed is the CHANNEL_IMPORTED audit event, which is exactly what
//   the notice tells the operator to go and read.
// Blast Radius: Whether an operator can start a second audited bulk import
//   after one whose outcome is unknown. No requests, no authorization meaning.
// Connections:
//   - File: frontend/src/components/srcc/views/RegistryImportFlow.tsx ->
//       raises it before dispatching the apply, clears it on a settled result.
//   - File: frontend/src/components/srcc/views/RegistryView.tsx -> renders the
//       notice and gates the Import CSV control on it.
//   - File: Docs/12_BACKEND_API_SPEC.md -> records that a re-plan is end-state
//       evidence only, which is why an unknown outcome stays unknown.
// ============================================================================

/**
 * Key PREFIX. Each pending apply gets `${prefix}.${applyId}`; the presence of
 * such a key is the record, and its value is never read.
 */
export const UNSETTLED_IMPORT_STORAGE_KEY = "ums.unsettledChannelImport";

/**
 * The guard when storage is not usable, and ONLY then. It is written
 * exclusively on a storage failure and dropped when a clear lands in the
 * mirror, so a browser where storage works never accumulates a second,
 * divergent copy of the truth.
 */
let memoryIds: readonly string[] = [];

const listeners = new Set<() => void>();

const notify = (): void => {
  for (const listener of listeners) {
    listener();
  }
};

/**
 * Every storage touch below is wrapped: a browser can refuse localStorage
 * outright or throw on write, and neither may take down the shell — the cost
 * of a refusal is cross-document persistence, not the guard itself.
 *
 * The KEY is the record; its value is never read. That is what removes the
 * read-modify-write, and with it the lost-update race and any need to parse
 * (so there is no corrupt-value branch to get wrong either).
 */
const keyFor = (applyId: string): string => `${UNSETTLED_IMPORT_STORAGE_KEY}.${applyId}`;

const isApplyKey = (key: string): boolean => {
  return key.startsWith(`${UNSETTLED_IMPORT_STORAGE_KEY}.`);
};

const storedApplyKeys = (): string[] => {
  const store = globalThis.localStorage;
  const keys: string[] = [];
  for (let index = 0; index < store.length; index += 1) {
    const key = store.key(index);
    if (key !== null && isApplyKey(key)) {
      keys.push(key);
    }
  }
  return keys;
};

const readIds = (): readonly string[] => {
  try {
    const stored = storedApplyKeys();
    // Either source holding an id WINS. A browser that reads storage happily
    // but throws on write (quota) would otherwise lose an apply recorded only
    // in memory — a fail-open on the one control this guards.
    return stored.length > 0 ? stored : memoryIds;
  } catch {
    return memoryIds;
  }
};

/** The snapshot useSyncExternalStore reads. A boolean, so it needs no cache. */
const readFlag = (): boolean => {
  return readIds().length > 0;
};

const rememberInMemory = (applyId: string, pending: boolean): void => {
  memoryIds = pending
    ? [...memoryIds.filter((id) => id !== applyId), applyId]
    : memoryIds.filter((id) => id !== applyId);
};

/**
 * Record one apply as pending. Touches ONLY this apply's key, so a concurrent
 * tab doing the same thing cannot drop it and it cannot drop theirs.
 */
const addId = (applyId: string): void => {
  try {
    globalThis.localStorage.setItem(keyFor(applyId), "1");
  } catch {
    // Storage refused this write. The in-memory copy is the whole guard now.
    rememberInMemory(applyId, true);
  }
};

/** Retire one apply. Same per-key isolation, in the other direction. */
const removeId = (applyId: string): void => {
  try {
    globalThis.localStorage.removeItem(keyFor(applyId));
  } catch {
    // Ignored: rememberInMemory below is the whole guard when storage refuses.
  }
  rememberInMemory(applyId, false);
};

/**
 * Retire every apply this document can see. Removal-only, so a key another tab
 * adds while this runs simply survives — the guard stays UP, which is the safe
 * direction for a race on an operator's "I have checked" statement.
 */
const removeAllIds = (): void => {
  try {
    for (const key of storedApplyKeys()) {
      globalThis.localStorage.removeItem(key);
    }
  } catch {
    // Ignored: the in-memory clear below still applies.
  }
  memoryIds = [];
};

const subscribe = (listener: () => void): (() => void) => {
  listeners.add(listener);
  // Another TAB's write. That event never fires for this document, so it
  // cannot loop against our own writes. A null key means the whole store was
  // cleared, so re-read rather than trusting newValue.
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || isApplyKey(event.key)) {
      listener();
    }
  };
  globalThis.addEventListener?.("storage", onStorage);
  return () => {
    listeners.delete(listener);
    globalThis.removeEventListener?.("storage", onStorage);
  };
};

export type UnsettledImportValue = {
  /** True while any apply of unknown outcome is unaccounted for. */
  unsettled: boolean;
  /** Record an apply as pending. Call BEFORE dispatching it, not after it
   * fails — a tab closed mid-request never reaches a failure handler. */
  trackApply: (applyId: string) => void;
  /** Retire ONE apply: its response established success or refusal. */
  settleApply: (applyId: string) => void;
  /** Retire every pending apply — the operator states they have checked the
   * audit trail, which is a claim about all of them, not the newest. */
  acknowledgeAll: () => void;
};

let fallbackCounter = 0;

/** Identity for one apply. Prefixed so a stray id is obvious in devtools. */
export const newApplyId = (): string => {
  const random = globalThis.crypto?.randomUUID?.();
  // performance.now() is monotonic within a document and the counter breaks
  // ties inside one millisecond, so the fallback cannot collide with itself.
  fallbackCounter += 1;
  return `apply-${random ?? `${Math.trunc(performance.now())}-${fallbackCounter}`}`;
};

/**
 * Read and control the store. Safe to call from any number of components —
 * they all observe the same module state, so a raise in the flow is visible to
 * the notice in the view without either being wired to the other.
 */
export const useUnsettledImport = (): UnsettledImportValue => {
  const unsettled = useSyncExternalStore(subscribe, readFlag, readFlag);
  const trackApply = useCallback((applyId: string) => {
    addId(applyId);
    notify();
  }, []);
  const settleApply = useCallback((applyId: string) => {
    removeId(applyId);
    notify();
  }, []);
  const acknowledgeAll = useCallback(() => {
    removeAllIds();
    notify();
  }, []);
  return useMemo(
    () => ({ unsettled, trackApply, settleApply, acknowledgeAll }),
    [unsettled, trackApply, settleApply, acknowledgeAll],
  );
};
