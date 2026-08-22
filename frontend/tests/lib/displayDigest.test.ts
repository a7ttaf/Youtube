import { describe, expect, it, vi } from "vitest";

import { computeDisplayDigest, computeDisplayDigestAsync, displayDigestMatchesDisclosedAsync } from "@/lib/displayDigest";
import type { ChannelImportResult } from "@/lib/api/types";

describe("displayDigest", () => {
  it("matches the backend canonical recipe including non-ASCII channel names", async () => {
    const plan: Pick<ChannelImportResult, "content_owner_id" | "cms_status" | "counts" | "rows"> = {
      content_owner_id: "COabc",
      cms_status: "INSIDE_CMS",
      counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      rows: [
        {
          row_number: 1,
          youtube_channel_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
          outcome: "CREATE",
          channel_name: "Alpha Nëws ünd Sport",
          group_id: null,
          group_action: null,
          revenue_required: true,
          revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
          changes: {},
          reason: null,
        },
      ],
    };
    const digest = computeDisplayDigest(plan);
    expect(await computeDisplayDigestAsync(plan)).toBe(digest);
    expect(digest).toBe("b84ea3d6d06c0fca721a49d4c994765aed33ef494ab493248542df51c06f15d1");
    expect(await displayDigestMatchesDisclosedAsync({ ...plan, display_digest: digest, dry_run: true, plan_fingerprint: "fp" })).toBe(true);
    expect(await displayDigestMatchesDisclosedAsync({ ...plan, display_digest: "deadbeef", dry_run: true, plan_fingerprint: "fp" })).toBe(false);
  });

  it("escapes DEL (U+007F) the way Python ensure_ascii does", async () => {
    const plan: Pick<ChannelImportResult, "content_owner_id" | "cms_status" | "counts" | "rows"> = {
      content_owner_id: "COabc",
      cms_status: "INSIDE_CMS",
      counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      rows: [
        {
          row_number: 1,
          youtube_channel_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
          outcome: "CREATE",
          channel_name: "Channel\x7fName",
          group_id: null,
          group_action: null,
          revenue_required: true,
          revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
          changes: {},
          reason: null,
        },
      ],
    };
    const digest = computeDisplayDigest(plan);
    expect(digest).toBe("2ccc2e316b542138b558bd277104910ec17123216f74443f78fd638c0a6452ce");
    expect(await displayDigestMatchesDisclosedAsync({ ...plan, display_digest: digest, dry_run: true, plan_fingerprint: "fp" })).toBe(true);
  });

  it("returns false instead of throwing when canonical JSON cannot be built", async () => {
    const plan = {
      content_owner_id: "COabc",
      cms_status: "INSIDE_CMS",
      counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      rows: [{ channel_name: Symbol("bad") }],
      display_digest: "abc123",
      dry_run: true,
      plan_fingerprint: "fp",
    } as unknown as ChannelImportResult;
    expect(await displayDigestMatchesDisclosedAsync(plan)).toBe(false);
  });

  it("verifies digests when Web Crypto subtle is unavailable (plain HTTP)", async () => {
    const plan: Pick<ChannelImportResult, "content_owner_id" | "cms_status" | "counts" | "rows"> = {
      content_owner_id: "COabc",
      cms_status: "INSIDE_CMS",
      counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      rows: [
        {
          row_number: 1,
          youtube_channel_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
          outcome: "CREATE",
          channel_name: "Alpha News",
          group_id: null,
          group_action: null,
          revenue_required: true,
          revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
          changes: {},
          reason: null,
        },
      ],
    };
    const digest = computeDisplayDigest(plan);
    const subtle = globalThis.crypto.subtle;
    Object.defineProperty(globalThis.crypto, "subtle", { configurable: true, value: undefined });
    try {
      expect(
        await displayDigestMatchesDisclosedAsync({
          ...plan,
          display_digest: digest,
          dry_run: true,
          plan_fingerprint: "fp",
        }),
      ).toBe(true);
    } finally {
      Object.defineProperty(globalThis.crypto, "subtle", { configurable: true, value: subtle });
    }
  });

  it("falls back to the main thread and leaks no waiter when postMessage throws", async () => {
    // Regression (PR #195 review): a synchronously throwing postMessage must
    // delete its own waiter entry before the fallback unwinds, so the map
    // never accumulates stale entries for the rest of the session. The map is
    // closure-private with no externally observable symptom, so this test pins
    // the observable contract: both calls still return the sync-recipe digest
    // and a broken worker never poisons later calls.
    //
    // The module memoizes `digestWorker` on first use, and earlier tests in
    // this file already called computeDisplayDigestAsync — so replacing
    // globalThis.Worker alone would leave the cached worker in place and the
    // stub never constructed. Reset the module registry FIRST (clears the
    // memoized digestWorker), THEN install the stub, THEN import a fresh
    // copy — and assert the stub's postMessage WAS called (postMessageCalls
    // > 0), which is what makes this test effective rather than a silent no-op.
    const previousWorker = (globalThis as { Worker?: unknown }).Worker;
    let constructedCount = 0;
    let postMessageCalls = 0;
    const brokenWorkerFactory = function WorkerStub(
      this: {
        onmessage: ((event: MessageEvent) => void) | null;
        onerror: ((event: ErrorEvent) => void) | null;
        onmessageerror: ((event: MessageEvent) => void) | null;
        terminated: boolean;
      },
    ): void {
      this.onmessage = null;
      this.onerror = null;
      this.onmessageerror = null;
      this.terminated = false;
      constructedCount += 1;
    };
    brokenWorkerFactory.prototype.postMessage = function postMessage(
      this: { terminated: boolean },
    ): void {
      void this.terminated;
      postMessageCalls += 1;
      throw new Error("detached or broken data channel");
    };
    brokenWorkerFactory.prototype.terminate = function terminate(
      this: { terminated: boolean },
    ): void {
      this.terminated = true;
    };
    try {
      // Order matters: clear the cached module (and its memoized Worker)
      // before installing the stub, then import so construction uses the stub.
      vi.resetModules();
      (globalThis as { Worker?: unknown }).Worker = brokenWorkerFactory as unknown;
      const freshModule = await import("@/lib/displayDigest");
      const plan: Pick<
        ChannelImportResult,
        "content_owner_id" | "cms_status" | "counts" | "rows"
      > = {
        content_owner_id: "COabc",
        cms_status: "INSIDE_CMS",
        counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
        rows: [],
      };
      const expected = computeDisplayDigest(plan);
      const first = await freshModule.computeDisplayDigestAsync(plan);
      expect(first).toBe(expected);
      const second = await freshModule.computeDisplayDigestAsync(plan);
      expect(second).toBe(expected);
      // Effectiveness proof: the stub was constructed and its throwing
      // postMessage was actually called on both requests — the fallback path
      // under test is genuinely exercised, not bypassed.
      expect(constructedCount).toBe(1);
      expect(postMessageCalls).toBe(2);
      // Leak proof: both throwing requests removed their own waiter entries,
      // so the fresh module's waiter map is empty. Without the fix, the map
      // holds exactly the two stale entries (observable via the count probe).
      expect(freshModule.__workerWaiterCount()).toBe(0);
    } finally {
      (globalThis as { Worker?: unknown }).Worker = previousWorker;
      vi.resetModules();
    }
  });
});
