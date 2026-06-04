import { useCallback, useEffect, useState } from "react";

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
// Blast Radius: None detected (read-only display state).
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

export function useAsync<T>(
  // The fetch must be a stable reference (memoize the caller's closure with
  // useCallback) so the effect does not re-run on every render.
  run: () => Promise<T>,
): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [loading, setLoading] = useState<boolean>(true);
  const [error, setError] = useState<ApiError | Error | null>(null);
  // Bumped by reload() to force a re-fetch with the same `run` reference.
  const [nonce, setNonce] = useState(0);

  const reload = useCallback(() => setNonce((value) => value + 1), []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    setError(null);
    run()
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
    return () => {
      // Supersede: a newer run (dep change or reload) must win.
      active = false;
    };
  }, [run, nonce]);

  return { data, loading, error, reload };
}
