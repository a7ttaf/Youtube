import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  UNSCOPED_IMPORT_SCOPE,
  UNSETTLED_IMPORT_STORAGE_KEY,
  importScopeFor,
  newApplyId,
  useUnsettledImport,
} from "@/contexts/UnsettledImportContext";

// The store is MODULE state mirrored into localStorage, so each test starts
// from a cleared mirror. These tests exist because the identity part of it is
// not observable from the flow's own tests: they render one document, and the
// case that broke was two.

beforeEach(() => {
  globalThis.localStorage.clear();
});

/** The apply ids the mirror currently holds for one scope. */
const storedIds = (scope: string = UNSCOPED_IMPORT_SCOPE): string[] => {
  const store = globalThis.localStorage;
  const prefix = `${UNSETTLED_IMPORT_STORAGE_KEY}.${scope}.`;
  return Array.from({ length: store.length }, (_unused, index) => store.key(index))
    .filter((key): key is string => key !== null && key.startsWith(prefix))
    .map((key) => key.slice(prefix.length))
    .sort();
};

describe("unsettled import store", () => {
  it("keeps the guard up when ONE of two pending applies settles", () => {
    // The finding: with a single boolean, whichever request settled first
    // cleared the other's protection, so a later lost response found the guard
    // already down and Import CSV live (review #184, codex P1).
    const { result } = renderHook(() => useUnsettledImport());
    expect(result.current.unsettled).toBe(false);

    act(() => result.current.trackApply("apply-tab-a"));
    act(() => result.current.trackApply("apply-tab-b"));
    expect(result.current.unsettled).toBe(true);

    act(() => result.current.settleApply("apply-tab-a"));
    expect(result.current.unsettled).toBe(true);
    expect(storedIds()).toEqual(["apply-tab-b"]);

    act(() => result.current.settleApply("apply-tab-b"));
    expect(result.current.unsettled).toBe(false);
    expect(storedIds()).toEqual([]);
  });

  it("settling an id it does not hold changes nothing", () => {
    // A stale handler from a document that already settled must not be able to
    // retire a LATER apply by arriving late.
    const { result } = renderHook(() => useUnsettledImport());
    act(() => result.current.trackApply("apply-live"));

    act(() => result.current.settleApply("apply-gone"));

    expect(result.current.unsettled).toBe(true);
    expect(storedIds()).toEqual(["apply-live"]);
  });

  it("acknowledging retires EVERY pending apply", () => {
    // The operator's claim is "I have checked the audit trail", which is about
    // all of them — not just the most recent.
    const { result } = renderHook(() => useUnsettledImport());
    act(() => result.current.trackApply("apply-one"));
    act(() => result.current.trackApply("apply-two"));

    act(() => result.current.acknowledgeAll());

    expect(result.current.unsettled).toBe(false);
    expect(storedIds()).toEqual([]);
  });

  it("does not lose a write when two tabs record an apply concurrently", () => {
    // The lost-update race a shared array had: both documents read the same
    // list before either saw the other's storage event, and the second write
    // dropped the first id. One key per apply has no shared cell to lose
    // (review #184, codex P1).
    const tabA = renderHook(() => useUnsettledImport());
    const tabB = renderHook(() => useUnsettledImport());

    // Interleaved deliberately: neither hook re-reads between the two writes.
    act(() => {
      tabA.result.current.trackApply("apply-tab-a");
      tabB.result.current.trackApply("apply-tab-b");
    });
    expect(storedIds()).toEqual(["apply-tab-a", "apply-tab-b"]);

    act(() => tabA.result.current.settleApply("apply-tab-a"));

    // Tab B's request is still unaccounted for, and both documents say so.
    expect(storedIds()).toEqual(["apply-tab-b"]);
    expect(tabA.result.current.unsettled).toBe(true);
    expect(tabB.result.current.unsettled).toBe(true);
  });

  it("does not lose a memory-only apply when a stored one settles", () => {
    // Quota (or any write failure) can land one apply in storage and the next
    // only in memory. Reading the mirror in PREFERENCE to memory reported just
    // the stored one; when that settled, its removeItem succeeded and the
    // guard dropped while the memory-only apply might still be committing
    // (review #184, codex P1). The two sources are one set.
    const { result } = renderHook(() => useUnsettledImport());
    act(() => result.current.trackApply("apply-stored"));
    expect(storedIds()).toEqual(["apply-stored"]);

    // Writes start failing; reads keep working. This is the quota shape.
    const setItemSpy = vi.spyOn(Storage.prototype, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    act(() => result.current.trackApply("apply-memory-only"));
    setItemSpy.mockRestore();

    // The stored one settles cleanly. The memory-only one must survive it.
    act(() => result.current.settleApply("apply-stored"));

    expect(storedIds()).toEqual([]);
    expect(result.current.unsettled).toBe(true);

    act(() => result.current.settleApply("apply-memory-only"));
    expect(result.current.unsettled).toBe(false);
  });

  it("ignores keys that are not per-apply records", () => {
    // The prefix must not swallow unrelated app storage, the bare prefix is
    // not a record, and another SCOPE's record belongs to another operator.
    globalThis.localStorage.setItem("ums.somethingElse", "1");
    globalThis.localStorage.setItem(UNSETTLED_IMPORT_STORAGE_KEY, "1");
    globalThis.localStorage.setItem(`${UNSETTLED_IMPORT_STORAGE_KEY}.other~user.a`, "1");

    const { result } = renderHook(() => useUnsettledImport());

    expect(result.current.unsettled).toBe(false);
  });

  it("keeps one operator's pending import invisible to another", () => {
    // localStorage is origin-wide and outlives sign-out. Unscoped, a pending
    // import followed a shared browser into the next session: the new operator
    // was blocked by an import they could not reconcile, and acknowledging it
    // cleared the original operator's protection (review #184, codex P2).
    const alice = importScopeFor("ums", "user-alice");
    const bob = importScopeFor("ums", "user-bob");
    const otherTenant = importScopeFor("other", "user-alice");
    expect(new Set([alice, bob, otherTenant]).size).toBe(3);

    const aliceHook = renderHook(() => useUnsettledImport(alice));
    act(() => aliceHook.result.current.trackApply("apply-alice"));

    // Neither the other principal nor the other tenant sees it.
    const bobHook = renderHook(() => useUnsettledImport(bob));
    const otherTenantHook = renderHook(() => useUnsettledImport(otherTenant));
    expect(aliceHook.result.current.unsettled).toBe(true);
    expect(bobHook.result.current.unsettled).toBe(false);
    expect(otherTenantHook.result.current.unsettled).toBe(false);

    // And Bob's blanket acknowledgement cannot retire Alice's protection.
    act(() => bobHook.result.current.acknowledgeAll());
    expect(storedIds(alice)).toEqual(["apply-alice"]);
    expect(aliceHook.result.current.unsettled).toBe(true);
  });

  it("still guards this document when storage refuses every write", () => {
    // Private mode / blocked cookies. The cost is cross-document persistence,
    // not the guard: an apply recorded only in memory still holds.
    const denied = () => {
      throw new DOMException("denied", "SecurityError");
    };
    vi.spyOn(Storage.prototype, "getItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "setItem").mockImplementation(denied);
    vi.spyOn(Storage.prototype, "removeItem").mockImplementation(denied);

    const { result } = renderHook(() => useUnsettledImport());
    act(() => result.current.trackApply("apply-memory-only"));
    expect(result.current.unsettled).toBe(true);

    act(() => result.current.settleApply("apply-memory-only"));
    expect(result.current.unsettled).toBe(false);

    vi.restoreAllMocks();
  });

  it("cannot let one tenant's scope be a PREFIX of another's", () => {
    // encodeURIComponent leaves `.` and `~` alone, so a slug shaped like
    // "ums~.child" produced keys beginning with the scope of a different
    // operator — who would then see, block on, and acknowledgeAll() away that
    // operator's pending imports, because the store matches with startsWith
    // (review #184, codex P2).
    const victim = importScopeFor("ums", "user-1");
    const attacker = importScopeFor("ums~.child", "user-1");

    expect(attacker.startsWith(victim)).toBe(false);
    expect(victim.startsWith(attacker)).toBe(false);
    // Neither half may contain the separator or the key delimiter.
    for (const scope of [victim, attacker, importScopeFor("a.b~c", "d~e.f")]) {
      expect(scope.split("~")).toHaveLength(2);
      expect(scope).not.toContain(".");
    }

    const victimHook = renderHook(() => useUnsettledImport(victim));
    const attackerHook = renderHook(() => useUnsettledImport(attacker));
    act(() => victimHook.result.current.trackApply("apply-victim"));

    expect(attackerHook.result.current.unsettled).toBe(false);
    act(() => attackerHook.result.current.acknowledgeAll());
    expect(victimHook.result.current.unsettled).toBe(true);
    expect(storedIds(victim)).toEqual(["apply-victim"]);
  });

  it("keeps distinct identities distinct after encoding", () => {
    // The encoding must be injective on the pair, or two different operators
    // would share one bucket — the same leak from the other direction.
    const scopes = [
      importScopeFor("a~b", "c"),
      importScopeFor("a", "b~c"),
      importScopeFor("a.b", "c"),
      importScopeFor("a", "b.c"),
      importScopeFor("A", "c"),
      importScopeFor("a", "C"),
    ];
    expect(new Set(scopes).size).toBe(scopes.length);
  });

  it("does its check and its record INSIDE one scope lock", async () => {
    // The check-then-act window codex identified is CROSS-DOCUMENT: two tabs
    // both read "nothing pending", both dispatch, and for an all-UNCHANGED
    // roster both POSTs return 200 and each appends a CHANNEL_IMPORTED
    // (review #184, codex P1). jsdom cannot host two documents, and within one
    // the claim is synchronous and so already indivisible — meaning no test
    // here can reproduce the interleave. What a test CAN pin is the mechanism
    // that closes it: the check and the write happen inside a Web Lock held on
    // this scope, which is the only cross-document exclusion a page has.
    const held: string[] = [];
    const request = vi.fn(async (name: string, callback: () => unknown) => {
      held.push(`enter:${name}`);
      const outcome = await callback();
      held.push(`exit:${name}`);
      return outcome;
    });
    vi.stubGlobal("navigator", { ...globalThis.navigator, locks: { request } });

    const { result } = renderHook(() => useUnsettledImport());
    expect(await result.current.admit("apply-one")).toBe(true);

    // One lock, named for the scope, entered and exited around the claim —
    // and the record exists only after it was taken.
    expect(request).toHaveBeenCalledTimes(1);
    expect(request.mock.calls[0][0]).toBe(
      `${UNSETTLED_IMPORT_STORAGE_KEY}.${UNSCOPED_IMPORT_SCOPE}`,
    );
    expect(held).toEqual([
      `enter:${UNSETTLED_IMPORT_STORAGE_KEY}.${UNSCOPED_IMPORT_SCOPE}`,
      `exit:${UNSETTLED_IMPORT_STORAGE_KEY}.${UNSCOPED_IMPORT_SCOPE}`,
    ]);
    expect(storedIds()).toEqual(["apply-one"]);

    // A second claim under the same lock is refused and writes NOTHING, so a
    // holder that loses the race cannot dispatch and cannot leave a record.
    expect(await result.current.admit("apply-two")).toBe(false);
    expect(storedIds()).toEqual(["apply-one"]);

    vi.unstubAllGlobals();
  });

  it("still admits when the browser has no Web Locks", async () => {
    // Degraded, and deliberately not disguised: without navigator.locks the
    // claim runs unlocked, which is narrower than the cross-document race but
    // not free of it. It must not fail CLOSED and lock the operator out.
    vi.stubGlobal("navigator", { userAgent: "test" });

    const { result } = renderHook(() => useUnsettledImport());

    expect(await result.current.admit("apply-nolocks")).toBe(true);
    expect(await result.current.admit("apply-second")).toBe(false);
    expect(storedIds()).toEqual(["apply-nolocks"]);

    vi.unstubAllGlobals();
  });

  it("admits again once the outstanding apply settles", async () => {
    // Admission must not be a one-shot latch: a settled apply frees the next.
    const { result } = renderHook(() => useUnsettledImport());
    expect(await result.current.admit("apply-first")).toBe(true);
    expect(await result.current.admit("apply-second")).toBe(false);

    act(() => result.current.settleApply("apply-first"));

    expect(await result.current.admit("apply-second")).toBe(true);
    expect(storedIds()).toEqual(["apply-second"]);
  });

  it("mints ids that do not collide", () => {
    const ids = new Set(Array.from({ length: 64 }, newApplyId));
    expect(ids.size).toBe(64);
  });
});
