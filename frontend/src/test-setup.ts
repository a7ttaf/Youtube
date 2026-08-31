import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { transferableAbortController } from "node:util";
import { afterEach } from "vitest";

// ============================================================================
// Purpose: Keep React Router's data-router Request construction compatible with
//          Vitest's jsdom AbortSignal and Node's native Request implementation.
// Database/ORM: None.
// Standards: Tests still exercise router cancellation. Compatible signals pass
//            through untouched; a jsdom cross-realm signal is followed by a
//            native Node signal so Request internals, clone(), and fetch() all
//            observe the same abort. Production browser globals are untouched.
// Blast Radius: Test bootstrap only; no shipped runtime behavior.
// ============================================================================
const NativeRequest = globalThis.Request;
if (NativeRequest) {
  const nativeRequestAcceptsSignal = (signal: AbortSignal): boolean => {
    try {
      void new NativeRequest("http://ums.local/__request-signal-probe__", {
        signal,
      });
      return true;
    } catch (error: unknown) {
      // A valid fixed URL leaves signal-brand incompatibility as the expected
      // TypeError. Surface anything else instead of letting test setup hide it.
      if (error instanceof TypeError) return false;
      throw error;
    }
  };

  const followWithNativeSignal = (source: AbortSignal): AbortSignal => {
    const nativeController = transferableAbortController();
    const abortNativeSignal = () => nativeController.abort(source.reason);

    if (source.aborted) {
      abortNativeSignal();
    } else {
      source.addEventListener("abort", abortNativeSignal, { once: true });
    }
    return nativeController.signal;
  };

  globalThis.Request = class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      const suppliedSignal = init?.signal ?? null;
      const isCrossRealmSignal =
        suppliedSignal !== null && !nativeRequestAcceptsSignal(suppliedSignal);

      // FIX: The former adapter deleted every RequestInit.signal, including
      // compatible signals, and the getter-only follow-up left Request's native
      // slot unconnected, so clone() and fetch() still ignored cancellation.
      // Bridge only the rejected jsdom signal into a real Node AbortSignal.
      super(
        input,
        isCrossRealmSignal
          ? { ...init, signal: followWithNativeSignal(suppliedSignal) }
          : init,
      );
    }
  } as typeof Request;
}

afterEach(() => {
  cleanup();
});
