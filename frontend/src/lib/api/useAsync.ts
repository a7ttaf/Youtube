import { useCallback, useEffect, useRef, useState } from "react";

import { ApiError } from "@/lib/api/client";

// ============================================================================
// Purpose: Reusable, ApiError-aware async-state hook for the data layer. Runs a
//   fetch function, tracks {data, loading, error}, and discards the result of a
//   superseded call so a slow earlier request cannot overwrite a newer one
//   (e.g. rapid month/scope switching in the Command Center). This is the
//   pattern every wired screen reuses.
// Database/ORM: None (frontend only).
// Standards: error is the typed ApiError when the API boundary threw one, else
//   a generic Error; callers branch on `instanceof ApiError` + status (403).
//   data is cleared to null at the START of every fetch (dep change or reload)
//   so a slow request cannot keep showing the PRIOR month's finance numbers
//   under a NEW month/scope filter — views fall back to their loading state.
//   The effect also DEDUPES a StrictMode/concurrent double-invoke (setup ->
//   cleanup -> setup with identical deps on the same instance): it reuses the
//   in-flight promise for the same (run, nonce) instead of firing a second
//   request. This matters because several reads (the audit log + finance list
//   endpoints) WRITE an audit row per call, so a double-invoke would otherwise
//   record a duplicate audit event. A real dep change or reload() still fires.
// Blast Radius: None detected (read-only display state); the dedupe only collapses
//   identical back-to-back fires, so reload/dep-change refetch behavior is intact.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> the thrown ApiError this surfaces.
//   - File: frontend/src/lib/api/useNetRevenue.ts -> first consumer.
// ============================================================================

export type AsyncState<T> = {
  data: T | null;
  loading: boolean;
  error: ApiError | Error | null;
  reload: () => void;
};

/**
 * Run a stable fetch closure and track {data, loading, error, reload}, discarding
 * the result of a superseded call so a slow earlier request cannot overwrite a
 * newer one. data is reset to null at the start of each fetch (dep change or
 * reload) so stale finance values never show under a new filter. `reload()`
 * re-runs the same `run` reference.
 *
 * `enabled` (default true) gates the fetch: while false the effect fires NO
 * request and loading stays at its initial true (so callers render their loading
 * state instead of acting on a not-yet-decided scope). The Command Center uses
 * this to hold net-revenue/rankings reads until the authorized-scope verdict is
 * in, preventing an initial unauthorized global read for a scoped viewer.
 */
export function useAsync<T>( // skipcq: JS-0067
  // The fetch must be a stable reference (memoize the caller's closure with
  // useCallback) so the effect does not re-run on every render.
  run: () => Promise<T>,
  // When false, no fetch is issued; loading remains true until enabled flips.
  enabled = true,
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<ApiError | Error | null>(null);
  // Bumped by reload() to force a re-fetch with the same `run` reference.
  const [nonce, setNonce] = useState(0);
  // Holds the in-flight request for the current (run, nonce) so a StrictMode
  // double-invoke (or concurrent remount) reuses it instead of starting a second,
  // separately-audited request. Persists across the simulated unmount/remount
  // because refs survive on the same component instance.
  const inflightRef = useRef<{
    run: () => Promise<T>;
    nonce: number;
    promise: Promise<T>;
  } | null>(null);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(function () { // skipcq: JS-R1005
    // FIX (review #102): gate the fetch on `enabled`. While disabled the effect
    // issues NO request and leaves loading at its initial true so callers render
    // their loading state instead of acting on a not-yet-decided scope (e.g. the
    // Command Center holds net-revenue/rankings reads until the authorized-scope
    // fetch resolves, preventing an initial unauthorized global read). Returning
    // undefined (no cleanup) is valid for a gated useEffect branch.
    if (!enabled) return;
    let active = true;
    // FIX: clear data at fetch start so a slow request under a NEW month/scope
    // filter falls back to the loading state instead of briefly showing the
    // PRIOR month's finance numbers (stale-value-under-new-filter).
    setData(null);
    setLoading(true);
    setError(null);
    // Extracted so every code path returns the same cleanup type (avoids the
    // inconsistent-return lint rule on the outer function).
    const cleanup = () => { active = false; };
    // FIX: StrictMode (dev) runs setup -> cleanup -> setup on the SAME instance
    // with IDENTICAL deps; calling run() on both setups fires the request TWICE.
    // For a self-auditing read that records a DUPLICATE audit event. Reuse the
    // in-flight promise for the same (run, nonce); a real dep change or reload()
    // produces a new key and correctly starts a fresh request.
    const cached = inflightRef.current;
    let promise: Promise<T>;
    if (cached && cached.run === run && cached.nonce === nonce) {
      promise = cached.promise;
    } else {
      try {
        promise = run();
      } catch (caught: unknown) {
        // FIX: run() throwing synchronously (before returning a Promise) would
        // otherwise leave loading=true pinned forever — .finally never fires.
        setError(caught instanceof Error ? caught : new Error(String(caught)));
        setLoading(false);
        return cleanup;
      }
    }
    inflightRef.current = { run, nonce, promise };
    promise
      .then((result) => {
        if (!active) return;
        setData(result);
      })
      .catch((caught: unknown) => {
        if (!active) return;
        // Keep the typed ApiError so consumers can read .status (403 etc.);
        // wrap anything non-Error so the state shape stays consistent.
        setData(null);
        setError(
          caught instanceof Error ? caught : new Error(String(caught)),
        );
      })
      .finally(() => {
        if (!active) return;
        setLoading(false);
      });
    // Supersede: a newer run (dep change or reload) must win.
    return cleanup;
  }, [run, nonce, enabled]);

  return { data, loading, error, reload };
}
