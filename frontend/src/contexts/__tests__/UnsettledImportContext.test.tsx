import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  UNSETTLED_IMPORT_STORAGE_KEY,
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

/** The per-apply keys the mirror currently holds, suffix only. */
const storedIds = (): string[] => {
  const store = globalThis.localStorage;
  const prefix = `${UNSETTLED_IMPORT_STORAGE_KEY}.`;
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

  it("ignores keys that are not per-apply records", () => {
    // The prefix must not swallow unrelated app storage, and the bare prefix
    // itself is not a record.
    globalThis.localStorage.setItem("ums.somethingElse", "1");
    globalThis.localStorage.setItem(UNSETTLED_IMPORT_STORAGE_KEY, "1");

    const { result } = renderHook(() => useUnsettledImport());

    expect(result.current.unsettled).toBe(false);
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

  it("mints ids that do not collide", () => {
    const ids = new Set(Array.from({ length: 64 }, newApplyId));
    expect(ids.size).toBe(64);
  });
});
