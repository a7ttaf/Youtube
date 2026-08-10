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
//   SCOPED to the tenant + principal. localStorage is origin-wide and outlives
//   sign-out, so an unscoped record followed a shared browser into the next
//   session: a different operator, or the same one on another tenant, would be
//   blocked by an import they cannot reconcile - and their acknowledgement
//   would clear the original operator's protection, after which returning to
//   the first tenant permits the duplicate this exists to stop (review #184,
//   codex P2). Every read, write and sweep is confined to one scope.
//   ADMISSION is atomic where the platform allows it. Reading `unsettled` and
//   then creating the key are two operations, so two tabs could both observe
//   false and both dispatch — for an all-UNCHANGED roster both POSTs return
//   200 and each appends a CHANNEL_IMPORTED (review #184, codex P1). admit()
//   does the check and the write inside one Web Lock held on the scope, which
//   IS cross-document, so at most one tab is admitted. Where navigator.locks
//   is unavailable it degrades to the same check-then-set without the lock and
//   says so here rather than pretending: that is narrower than the race, not
//   free of it.
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
 * Key PREFIX. Each pending apply gets `${prefix}.${scope}.${applyId}`; the
 * presence of such a key is the record, and its value is never read.
 */
export const UNSETTLED_IMPORT_STORAGE_KEY = "ums.unsettledChannelImport";

/**
 * The scope used when no principal is known - a standalone render, or a shell
 * that has not hydrated a session yet. Deliberately a real, NAMED scope rather
 * than an empty string: records made without an identity must not be visible
 * to an identified operator, and vice versa.
 */
export const UNSCOPED_IMPORT_SCOPE = "unscoped";

/**
 * One operator on one tenant. Encoded because the separator is a dot and a
 * slug or id containing one would otherwise blur two scopes together.
 */
export const importScopeFor = (
  tenantSlug: string | null | undefined,
  userId: string | null | undefined,
): string => {
  if (!tenantSlug || !userId) {
    return UNSCOPED_IMPORT_SCOPE;
  }
  return `${encodeURIComponent(tenantSlug)}~${encodeURIComponent(userId)}`;
};

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
const prefixFor = (scope: string): string => `${UNSETTLED_IMPORT_STORAGE_KEY}.${scope}.`;

const keyFor = (scope: string, applyId: string): string => `${prefixFor(scope)}${applyId}`;

const isApplyKey = (key: string): boolean => {
  return key.startsWith(`${UNSETTLED_IMPORT_STORAGE_KEY}.`);
};

const storedApplyKeys = (scope: string): string[] => {
  const store = globalThis.localStorage;
  const prefix = prefixFor(scope);
  const keys: string[] = [];
  for (let index = 0; index < store.length; index += 1) {
    const key = store.key(index);
    if (key !== null && key.startsWith(prefix)) {
      keys.push(key);
    }
  }
  return keys;
};

/**
 * The UNION of both sources, never one in preference to the other.
 *
 * Preferring a non-empty mirror was a fail-open: with apply A already stored
 * and a quota error swallowing B's write, B lives only in memory. Reading the
 * mirror alone reports {A}; A then settles, its removeItem succeeds, and the
 * guard drops while B may still be committing (review #184, codex P1). The
 * two sources are partial views of one set, so the set is what to read.
 */
const readIds = (scope: string): readonly string[] => {
  let stored: readonly string[] = [];
  try {
    stored = storedApplyKeys(scope);
  } catch {
    // Mirror unreadable; memoryIds alone is the guard for this document.
  }
  const inMemory = memoryIds.filter((id) => id.startsWith(prefixFor(scope)));
  return stored.length === 0 ? inMemory : [...new Set([...stored, ...inMemory])];
};

/** The snapshot useSyncExternalStore reads. A boolean, so it needs no cache. */
const readFlag = (scope: string): boolean => {
  return readIds(scope).length > 0;
};

// Held under the FULL key, so the in-memory set carries its scope exactly as
// the mirror does and cannot leak across operators either.
const rememberInMemory = (key: string, pending: boolean): void => {
  memoryIds = pending
    ? [...memoryIds.filter((id) => id !== key), key]
    : memoryIds.filter((id) => id !== key);
};

/**
 * Record one apply as pending. Touches ONLY this apply's key, so a concurrent
 * tab doing the same thing cannot drop it and it cannot drop theirs.
 */
const addId = (scope: string, applyId: string): void => {
  try {
    globalThis.localStorage.setItem(keyFor(scope, applyId), "1");
  } catch {
    // Storage refused this write. The in-memory copy is the whole guard now.
    rememberInMemory(keyFor(scope, applyId), true);
  }
};

/** Retire one apply. Same per-key isolation, in the other direction. */
const removeId = (scope: string, applyId: string): void => {
  try {
    globalThis.localStorage.removeItem(keyFor(scope, applyId));
  } catch {
    // Ignored: rememberInMemory below is the whole guard when storage refuses.
  }
  // Drops ONLY this id. It used to clear the whole in-memory set, which meant
  // one apply settling took an unrelated memory-only apply down with it.
  rememberInMemory(keyFor(scope, applyId), false);
};

/**
 * Retire every apply this document can see. Removal-only, so a key another tab
 * adds while this runs simply survives — the guard stays UP, which is the safe
 * direction for a race on an operator's "I have checked" statement.
 */
const removeAllIds = (scope: string): void => {
  try {
    for (const key of storedApplyKeys(scope)) {
      globalThis.localStorage.removeItem(key);
    }
  } catch {
    // Ignored: the in-memory clear below still applies.
  }
  // THIS scope only. An operator's "I have checked" is a statement about their
  // own imports; it must not retire another principal's or another tenant's.
  memoryIds = memoryIds.filter((id) => !id.startsWith(prefixFor(scope)));
};

/**
 * Claim the right to dispatch an apply, atomically where possible.
 *
 * Returns true when this document is admitted and `applyId` is now recorded as
 * pending; false when another apply in the same scope is already outstanding,
 * in which case NOTHING was written and the caller must not dispatch.
 *
 * The Web Lock is held across the read AND the write, which is what makes this
 * different from calling `unsettled` and then `trackApply`: the lock is
 * cross-document, so a second tab blocks until the first has either recorded
 * its key or released. Web Locks is the only cross-document mutual-exclusion
 * primitive available to a page; storage events are notifications, not
 * exclusion, and cannot close this window.
 */
const admitApply = async (scope: string, applyId: string): Promise<boolean> => {
  const claim = (): boolean => {
    if (readFlag(scope)) {
      return false;
    }
    addId(scope, applyId);
    return true;
  };
  const locks = globalThis.navigator?.locks;
  if (locks === undefined) {
    // No cross-document exclusion available. Same check, same order, without
    // the guarantee — honestly narrower than the race rather than closed.
    return claim();
  }
  return locks.request(`${UNSETTLED_IMPORT_STORAGE_KEY}.${scope}`, claim);
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
   * fails — a tab closed mid-request never reaches a failure handler. Prefer
   * `admit` from a dispatch path; this is the unconditional form. */
  trackApply: (applyId: string) => void;
  /** Atomically claim the right to dispatch. False means another apply is
   * already outstanding in this scope and nothing was recorded. */
  admit: (applyId: string) => Promise<boolean>;
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
export const useUnsettledImport = (
  scope: string = UNSCOPED_IMPORT_SCOPE,
): UnsettledImportValue => {
  const snapshot = useCallback(() => readFlag(scope), [scope]);
  const unsettled = useSyncExternalStore(subscribe, snapshot, snapshot);
  const trackApply = useCallback(
    (applyId: string) => {
      addId(scope, applyId);
      notify();
    },
    [scope],
  );
  const admit = useCallback(
    async (applyId: string) => {
      const admitted = await admitApply(scope, applyId);
      notify();
      return admitted;
    },
    [scope],
  );
  const settleApply = useCallback(
    (applyId: string) => {
      removeId(scope, applyId);
      notify();
    },
    [scope],
  );
  const acknowledgeAll = useCallback(() => {
    removeAllIds(scope);
    notify();
  }, [scope]);
  return useMemo(
    () => ({ unsettled, trackApply, admit, settleApply, acknowledgeAll }),
    [unsettled, trackApply, admit, settleApply, acknowledgeAll],
  );
};
