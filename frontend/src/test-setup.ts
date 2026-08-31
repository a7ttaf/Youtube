import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// ============================================================================
// Purpose: Keep React Router's data-router Request construction compatible with
//          Vitest's jsdom AbortSignal and Node's native Request implementation.
// Database/ORM: None.
// Standards: Tests still exercise router cancellation. Compatible signals pass
//            through untouched; only jsdom's cross-realm signal is omitted from
//            Node's constructor, then exposed by the Request so abort state and
//            reason remain live. Production browser globals are untouched.
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

  globalThis.Request = class extends NativeRequest {
    readonly #crossRealmSignal: AbortSignal | null;

    constructor(input: RequestInfo | URL, init?: RequestInit) {
      const suppliedSignal = init?.signal ?? null;
      const isCrossRealmSignal =
        suppliedSignal !== null && !nativeRequestAcceptsSignal(suppliedSignal);

      // FIX: The former adapter deleted every RequestInit.signal, including
      // compatible signals, so router cancellation silently stopped working.
      // Node alone receives the incompatible signal-free init; callers still
      // observe the original live jsdom signal through the getter below.
      super(
        input,
        isCrossRealmSignal ? { ...init, signal: undefined } : init,
      );
      this.#crossRealmSignal = isCrossRealmSignal ? suppliedSignal : null;
    }

    override get signal(): AbortSignal {
      return this.#crossRealmSignal ?? super.signal;
    }
  } as typeof Request;
}

afterEach(() => {
  cleanup();
});
