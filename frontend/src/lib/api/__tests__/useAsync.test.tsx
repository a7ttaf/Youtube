import { act, renderHook, waitFor } from "@testing-library/react";
import { useCallback } from "react";
import { describe, expect, it } from "vitest";

import { ApiError } from "@/lib/api/client";
import { useAsync } from "@/lib/api/useAsync";

/** Resolve/reject a promise from outside, for ordering deferred fetches. */
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  let reject!: (reason: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
};

type Result = { id: string };

/**
 * Drive useAsync from a queue of deferred results behind a STABLE run closure
 * (useAsync requires a stable reference). Each fetch pops the next deferred so a
 * test can control mount vs. reload resolution order independently.
 */
const useQueuedAsync = (queue: Array<ReturnType<typeof deferred<Result>>>) => {
  const run = useCallback(() => {
    const next = queue.shift();
    if (!next) throw new Error("queue exhausted: no deferred for this fetch");
    return next.promise;
  }, [queue]);
  return useAsync(run);
};

describe("useAsync", () => {
  it("clears stale data on reload: data is null while the next fetch is in flight", async () => {
    const fetchA = deferred<Result>();
    const fetchB = deferred<Result>();
    const queue = [fetchA, fetchB];
    const { result } = renderHook(() => useQueuedAsync(queue));

    // Mount fetch A resolves -> data is A. Await the settled promise inside act
    // so React flushes the resulting state update before assertions run.
    await act(async () => {
      fetchA.resolve({ id: "A" });
      await fetchA.promise;
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.id).toBe("A");

    // reload() starts fetch B; while B is in flight, data must be cleared to
    // null (not the stale A) so views fall back to their loading state.
    act(() => {
      result.current.reload();
    });
    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();

    // B resolves -> data is B. Await the settled promise inside act so the state
    // update from the resolved fetch is flushed before assertions run.
    await act(async () => {
      fetchB.resolve({ id: "B" });
      await fetchB.promise;
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.id).toBe("B");
    expect(result.current.error).toBeNull();
  });

  it("starts with null data while the initial fetch is in flight", async () => {
    const fetchA = deferred<Result>();
    const queue = [fetchA];
    const { result } = renderHook(() => useQueuedAsync(queue));

    expect(result.current.loading).toBe(true);
    expect(result.current.data).toBeNull();

    // Await the settled promise inside act so the resolved-fetch state update is
    // flushed before assertions run.
    await act(async () => {
      fetchA.resolve({ id: "A" });
      await fetchA.promise;
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data?.id).toBe("A");
  });

  it("surfaces a typed ApiError and clears data on failure", async () => {
    const fetchA = deferred<Result>();
    const queue = [fetchA];
    const { result } = renderHook(() => useQueuedAsync(queue));

    // Reject the in-flight fetch, then await its settlement inside act (swallowing
    // the expected rejection here — the hook owns surfacing it) so the resulting
    // error-state update is flushed before assertions run.
    await act(async () => {
      fetchA.reject(
        new ApiError("forbidden", 403, { detail: "nope" }, "/test"),
      );
      await fetchA.promise.catch(() => undefined);
    });
    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });
});
