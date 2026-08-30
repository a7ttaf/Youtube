import "@testing-library/jest-dom/vitest";
import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

// ============================================================================
// Purpose: Keep React Router's data-router Request construction compatible with
//          Vitest's jsdom AbortSignal and Node's native Request implementation.
// Database/ORM: None.
// Standards: Tests still exercise router transitions; the adapter removes only
//            the cross-realm signal rejected by Node's undici Request. Production
//            browser globals are untouched.
// Blast Radius: Test bootstrap only; no shipped runtime behavior.
// ============================================================================
const NativeRequest = globalThis.Request;
if (NativeRequest) {
  globalThis.Request = class extends NativeRequest {
    constructor(input: RequestInfo | URL, init?: RequestInit) {
      if (init === undefined) {
        super(input);
        return;
      }
      super(input, { ...init, signal: undefined });
    }
  } as typeof Request;
}

afterEach(() => {
  cleanup();
});
