import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  UNSCOPED_IMPORT_SCOPE,
  adoptPendingApplies,
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

  it("refuses admission when the record cannot be persisted", async () => {
    // Fail CLOSED: a memory-only record is invisible to another tab and gone
    // after a reload, so admitting on one hands out a claim nobody else can
    // honour — for a write that appends an audit event. Private-browsing and
    // "block site data" both produce exactly this throw.
    const setItem = vi
      .spyOn(globalThis.Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("quota", "QuotaExceededError");
      });
    try {
      const { result } = renderHook(() => useUnsettledImport());

      await act(async () => {
        await expect(result.current.admit("apply-nostore")).resolves.toBe("not-durable");
      });
    } finally {
      setItem.mockRestore();
    }
  });

  it("leaves NOTHING pending after a refused admission", async () => {
    // The follow-on finding: addId keeps the id in memory before reporting the
    // failure, so a refusal that did not dispatch still marked the document
    // unsettled. The next attempt was then refused as "other-apply-pending" —
    // blaming a tab that does not exist — and leaving the flow warned that an
    // import may still be committing for a request that never left (review
    // #184, codex P2).
    const setItem = vi
      .spyOn(globalThis.Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("quota", "QuotaExceededError");
      });
    const { result } = renderHook(() => useUnsettledImport());
    try {
      await act(async () => {
        await result.current.admit("apply-refused");
      });

      expect(result.current.unsettled).toBe(false);
    } finally {
      setItem.mockRestore();
    }

    // And the refusal is not sticky: once storage works, the NEXT admission is
    // decided on its own merits rather than on the corpse of the refused one.
    await act(async () => {
      await expect(result.current.admit("apply-later")).resolves.toBe("admitted");
    });
    expect(result.current.unsettled).toBe(true);
    expect(storedIds()).toEqual(["apply-later"]);
  });

  it("still retains a tracked apply when storage refuses", () => {
    // The complement, so the retirement above cannot be over-applied:
    // trackApply is called AFTER a request is dispatched, and there the write
    // really is in flight. Losing that record would take the guard down over a
    // live write — the opposite failure.
    const setItem = vi
      .spyOn(globalThis.Storage.prototype, "setItem")
      .mockImplementation(() => {
        throw new DOMException("quota", "QuotaExceededError");
      });
    try {
      const { result } = renderHook(() => useUnsettledImport());

      act(() => result.current.trackApply("apply-dispatched"));

      expect(result.current.unsettled).toBe(true);
    } finally {
      setItem.mockRestore();
    }
  });

  it("carries a pending apply across a tenant RESOLUTION", () => {
    // A session whose body has no tenant starts on the missing-tenant scope
    // and moves to the real one when /tenants/me answers. Admission is
    // withheld until the scope settles, but a tenant that resolves AFTER a
    // failed bootstrap can still move it under an outstanding apply — and the
    // record would then sit where neither this tab's guard nor another tab's
    // looks for it (review #184, codex P2).
    const before = importScopeFor(null, "user-1");
    const after = importScopeFor("tenant-1", "user-1");
    const beforeHook = renderHook(() => useUnsettledImport(before));
    act(() => beforeHook.result.current.trackApply("apply-mid-bootstrap"));

    act(() => adoptPendingApplies(before, after));

    expect(storedIds(after)).toEqual(["apply-mid-bootstrap"]);
    expect(storedIds(before)).toEqual([]);
    expect(renderHook(() => useUnsettledImport(after)).result.current.unsettled).toBe(true);
  });

  it("refuses admission under a scope whose adoption has not been drained", async () => {
    // The migration lives in an effect, and an effect runs after paint. An
    // admission decided in that gap checks the NEW — empty — scope and is
    // granted while the old one still holds an outstanding apply, dropping the
    // duplicate guard exactly during tenant resolution (review #184, qodo).
    // admit() therefore drains the adoption itself before claiming.
    //
    // HONEST LIMIT: React flushes the effect during rerender here, so this
    // asserts the end-to-end guarantee rather than isolating the pre-effect
    // window — it would also pass if only the effect adopted. What pins the
    // mechanism is the syncScope() call at the top of admit; this test fails
    // if adoption stops happening at all on a scope change.
    const before = importScopeFor(null, "user-1");
    const after = importScopeFor("tenant-1", "user-1");
    const { result, rerender } = renderHook(({ scope }) => useUnsettledImport(scope), {
      initialProps: { scope: before },
    });
    act(() => result.current.trackApply("apply-outstanding"));

    rerender({ scope: after });

    await act(async () => {
      await expect(result.current.admit("apply-second")).resolves.toBe(
        "other-apply-pending",
      );
    });
    expect(storedIds(after)).toEqual(["apply-outstanding"]);
  });

  it("carries NOTHING across a change of principal", () => {
    // The isolation this scoping exists for. Every transition except a
    // same-user tenant resolution is a different operator, and inheriting
    // their pending import is the cross-operator leak — an acknowledgement
    // would then retire protection that was never theirs.
    const alice = importScopeFor(null, "user-alice");
    const bobResolved = importScopeFor("tenant-1", "user-bob");
    const aliceHook = renderHook(() => useUnsettledImport(alice));
    act(() => aliceHook.result.current.trackApply("apply-alice"));

    act(() => adoptPendingApplies(alice, bobResolved));

    expect(storedIds(bobResolved)).toEqual([]);
    expect(storedIds(alice)).toEqual(["apply-alice"]);
  });

  it("carries nothing BACKWARDS, from a known tenant to none", () => {
    // A tenant going back to unresolved is a regression, not a resolution:
    // sign-out and re-bootstrap look like this, and the next session must not
    // inherit the previous one's pending import.
    const resolved = importScopeFor("tenant-1", "user-1");
    const unresolved = importScopeFor(null, "user-1");
    const hook = renderHook(() => useUnsettledImport(resolved));
    act(() => hook.result.current.trackApply("apply-resolved"));

    act(() => adoptPendingApplies(resolved, unresolved));

    expect(storedIds(unresolved)).toEqual([]);
    expect(storedIds(resolved)).toEqual(["apply-resolved"]);
  });

  it("adopts idempotently, because two components share the scope", () => {
    // RegistryView and RegistryImportFlow both read this store, so the effect
    // runs twice for one scope change. The second pass must find nothing left
    // to move rather than duplicating or dropping.
    const before = importScopeFor(null, "user-1");
    const after = importScopeFor("tenant-1", "user-1");
    const hook = renderHook(() => useUnsettledImport(before));
    act(() => hook.result.current.trackApply("apply-once"));

    act(() => adoptPendingApplies(before, after));
    act(() => adoptPendingApplies(before, after));

    expect(storedIds(after)).toEqual(["apply-once"]);
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

    act(() => result.current.acknowledge(result.current.snapshotPendingIds()));

    expect(result.current.unsettled).toBe(false);
    expect(storedIds()).toEqual([]);
  });

  it("acknowledges only the applies the warning represented", () => {
    // The operator's claim is "I checked the audit trail for the imports this
    // warning told me about". An apply admitted in ANOTHER tab after that
    // warning rendered was never represented by it and may still be
    // committing, so sweeping every key in the scope drops the duplicate-write
    // guard over a live request (review #184, codex P2).
    const { result } = renderHook(() => useUnsettledImport());
    act(() => result.current.trackApply("apply-warned"));

    // What the warning showed, captured when it went up.
    const warned = result.current.snapshotPendingIds();

    // Another tab admits while the warning stands.
    act(() => result.current.trackApply("apply-admitted-later"));

    act(() => result.current.acknowledge(warned));

    expect(storedIds()).toEqual(["apply-admitted-later"]);
    expect(result.current.unsettled).toBe(true);
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
    act(() => bobHook.result.current.acknowledge(bobHook.result.current.snapshotPendingIds()));
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
    // operator — who would then see, block on, and acknowledge away that
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
    act(() => attackerHook.result.current.acknowledge(attackerHook.result.current.snapshotPendingIds()));
    expect(victimHook.result.current.unsettled).toBe(true);
    expect(storedIds(victim)).toEqual(["apply-victim"]);
  });

  it("keeps two known operators apart when the TENANT is missing", () => {
    // SessionMe.tenant is nullable and hasActiveSession still admits such a
    // session, so bootstrap and degraded tenant contexts really reach this.
    // Collapsing to one "unknown" bucket whenever either half was missing put
    // two different operators — both with a known userId — in the same bucket,
    // where one blocked the other and the second's acknowledgement retired
    // the first's protection (review #184, codex P2).
    const alice = importScopeFor(null, "user-alice");
    const bob = importScopeFor(undefined, "user-bob");
    expect(alice).not.toBe(bob);
    expect(alice).not.toBe(UNSCOPED_IMPORT_SCOPE);

    const aliceHook = renderHook(() => useUnsettledImport(alice));
    const bobHook = renderHook(() => useUnsettledImport(bob));
    act(() => aliceHook.result.current.trackApply("apply-alice"));

    expect(bobHook.result.current.unsettled).toBe(false);
    act(() => bobHook.result.current.acknowledge(bobHook.result.current.snapshotPendingIds()));
    expect(aliceHook.result.current.unsettled).toBe(true);
  });

  it("keeps a known tenant apart from a wholly unknown session", () => {
    // The other half of the same rule, and the fully-unknown case still has
    // its own named bucket rather than sharing with anyone identified.
    const tenantOnly = importScopeFor("tenant-1", null);
    const nothing = importScopeFor(null, null);

    expect(tenantOnly).not.toBe(nothing);
    expect(nothing).toBe(UNSCOPED_IMPORT_SCOPE);
    // A known component can never be read as a missing one: the presence flag
    // is a fixed leading character, so no real id can impersonate the sentinel.
    expect(importScopeFor("0", "user-1")).not.toBe(importScopeFor(null, "user-1"));
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
    expect(await result.current.admit("apply-one")).toBe("admitted");

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
    expect(await result.current.admit("apply-two")).toBe("other-apply-pending");
    expect(storedIds()).toEqual(["apply-one"]);

    vi.unstubAllGlobals();
  });

  it("still admits when the browser has no Web Locks", async () => {
    // Degraded, and deliberately not disguised: without navigator.locks the
    // claim runs unlocked, which is narrower than the cross-document race but
    // not free of it. It must not fail CLOSED and lock the operator out.
    vi.stubGlobal("navigator", { userAgent: "test" });

    const { result } = renderHook(() => useUnsettledImport());

    expect(await result.current.admit("apply-nolocks")).toBe("admitted");
    expect(await result.current.admit("apply-second")).toBe("other-apply-pending");
    expect(storedIds()).toEqual(["apply-nolocks"]);

    vi.unstubAllGlobals();
  });

  it("admits again once the outstanding apply settles", async () => {
    // Admission must not be a one-shot latch: a settled apply frees the next.
    const { result } = renderHook(() => useUnsettledImport());
    expect(await result.current.admit("apply-first")).toBe("admitted");
    expect(await result.current.admit("apply-second")).toBe("other-apply-pending");

    act(() => result.current.settleApply("apply-first"));

    expect(await result.current.admit("apply-second")).toBe("admitted");
    expect(storedIds()).toEqual(["apply-second"]);
  });

  it("mints ids that do not collide", () => {
    const ids = new Set(Array.from({ length: 64 }, newApplyId));
    expect(ids.size).toBe(64);
  });

  it("draws the document salt from getRandomValues at MODULE INIT", async () => {
    // The salt is an IIFE at module scope, so stubbing `crypto` inside a test
    // that imported the module earlier changes nothing — a test written that
    // way asserts the id FORMAT and would pass even if the salt initialization
    // regressed entirely (review #184, qodo). Reset the module registry and
    // re-import under the stub so the real initialization path runs.
    vi.resetModules();
    let calls = 0;
    const stubbed = {
      getRandomValues: (array: Uint32Array) => {
        calls += 1;
        array.fill(0xabcdef);
        return array;
      },
    };
    vi.stubGlobal("crypto", stubbed);
    try {
      const fresh = await import("@/contexts/UnsettledImportContext");
      const id = fresh.newApplyId();

      // The module asked for entropy at init, exactly once...
      expect(calls).toBe(1);
      // ...and what it was given is IN the id, so the salt is really sourced
      // from getRandomValues rather than reconstructed from the clock.
      expect(id).toContain((0xabcdef).toString(36));
      // No randomUUID on the stub, so this is the fallback shape.
      expect(id).toMatch(/^apply-[0-9a-z]+-\d+-\d+$/u);
    } finally {
      vi.unstubAllGlobals();
      vi.resetModules();
    }
  });

  it("initialises the salt without crypto at all, rather than throwing", async () => {
    // Last resort, and it must survive MODULE LOAD: a throw here would break
    // the import of this context entirely, not merely degrade an id.
    vi.resetModules();
    const original = Object.getOwnPropertyDescriptor(globalThis, "crypto");
    Object.defineProperty(globalThis, "crypto", { configurable: true, value: undefined });
    try {
      const fresh = await import("@/contexts/UnsettledImportContext");
      const ids = Array.from({ length: 32 }, fresh.newApplyId);

      expect(new Set(ids).size).toBe(32);
      expect(ids[0]).toMatch(/^apply-[0-9a-z]+-\d+-\d+$/u);
    } finally {
      // ALWAYS, and unconditionally: a thrown assertion above must not leak a
      // crypto-less global into every test that runs after this one.
      if (original === undefined) {
        Reflect.deleteProperty(globalThis, "crypto");
      } else {
        Object.defineProperty(globalThis, "crypto", original);
      }
      vi.resetModules();
    }
  });
});
