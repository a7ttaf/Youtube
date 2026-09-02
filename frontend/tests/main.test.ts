import type { ErrorInfo } from "react";
import { afterAll, afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

// Keep bootstrap execution isolated so the test can inspect the exact options
// handed to React 19's createRoot without mounting the complete application.
const { createRootMock, renderMock } = vi.hoisted(() => {
  const renderMock = vi.fn();
  return {
    createRootMock: vi.fn((_element: unknown, _options: unknown) => ({
      render: renderMock,
    })),
    renderMock,
  };
});

vi.mock("react-dom/client", () => ({ createRoot: createRootMock }));

type RootErrorHandler = (error: unknown, info: ErrorInfo) => void;

type RootOptions = {
  onCaughtError: RootErrorHandler;
  onUncaughtError: RootErrorHandler;
  onRecoverableError: RootErrorHandler;
};

let rootOptions: RootOptions;
let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

// ============================================================================
// Purpose: Verify React bootstrap replaces every default raw error reporter.
// Database/ORM: None (frontend bootstrap test only).
// Standards: Replay hostile production-shaped errors in both development and
//   production modes; assertions reject the secret message and component stack
//   from every console payload while retaining only safe telemetry fields.
// Blast Radius: React root error containment and client-side privacy contract.
// Connections:
//   - File: frontend/src/main.tsx -> createRoot callback wiring.
//   - File: frontend/src/components/srcc/ErrorBoundary.tsx -> shared safe
//     category and correlation-id payload.
// ============================================================================

beforeAll(async () => {
  document.body.innerHTML = '<div id="root"></div>';
  await import("@/main");

  const options = createRootMock.mock.calls[0]?.[1];
  if (options === undefined) {
    throw new Error("createRoot options were not captured");
  }
  rootOptions = options as RootOptions;
});

beforeEach(() => {
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
  vi.unstubAllEnvs();
});

afterAll(() => {
  document.body.innerHTML = "";
});

describe("React root error callbacks", () => {
  it("passes sanitized callbacks to createRoot and still renders the app", () => {
    expect(createRootMock).toHaveBeenCalledWith(
      document.getElementById("root"),
      expect.objectContaining({
        onCaughtError: expect.any(Function),
        onUncaughtError: expect.any(Function),
        onRecoverableError: expect.any(Function),
      }),
    );
    expect(renderMock).toHaveBeenCalledTimes(1);
  });

  it.each([
    ["development", true],
    ["production", false],
  ])(
    "never forwards raw error data to console in %s mode",
    (_mode, isDevelopment) => {
      vi.stubEnv("DEV", isDevelopment);
      const secret = "secret finance token 42-amount-999";
      const error = new TypeError(secret);
      error.name = "SecretFinanceError";
      const info: ErrorInfo = { componentStack: `\n    at ${secret}` };

      // A caught subtree is reported by ErrorBoundary.componentDidCatch; this
      // callback must suppress React's default raw console.error path.
      rootOptions.onCaughtError(error, info);
      expect(consoleErrorSpy).not.toHaveBeenCalled();

      rootOptions.onUncaughtError(error, info);
      rootOptions.onRecoverableError(error, info);

      expect(consoleErrorSpy).toHaveBeenCalledTimes(2);
      for (const call of consoleErrorSpy.mock.calls) {
        expect(JSON.stringify(call)).not.toContain(secret);
        expect(call[0]).toMatch(/^\[ReactRoot\] (uncaught|recoverable) render failure$/u);
        expect(call[1]).toEqual({
          category: "Error",
          correlationId: expect.stringMatching(/^(?:view-error-)?[0-9a-f-]+$/iu),
        });
      }
    },
  );

  it("records and degrades — never rethrows — when the console sink itself fails", async () => {
    // Rule: a caught exception on the reporting path must be observably
    // handled, but the sink must never become a second uncaught application
    // error. So a throwing console.error is RECORDED in the in-memory trail
    // and degraded to console.warn with a count-only payload — no raw error
    // data ever reaches the fallback channel either.
    const { recordedRootReportFailures } = (await import("@/main")) as {
      recordedRootReportFailures: () => readonly unknown[];
    };
    const failuresBefore = recordedRootReportFailures().length;
    const consoleWarnSpy = vi.spyOn(console, "warn").mockImplementation(() => undefined);
    const throwingSink = vi.spyOn(console, "error").mockImplementation(() => {
      throw new Error("console sink is broken");
    });
    const secret = "secret finance token 42-amount-999";
    const error = new TypeError(secret);

    expect(() => rootOptions.onUncaughtError(error, {} as ErrorInfo)).not.toThrow();
    expect(() => rootOptions.onRecoverableError(error, {} as ErrorInfo)).not.toThrow();

    // Each failed emit was recorded exactly once.
    expect(recordedRootReportFailures().length).toBe(failuresBefore + 2);
    // The degraded notice went to the DIFFERENT channel, count-only.
    expect(consoleWarnSpy).toHaveBeenCalledTimes(2);
    for (const call of consoleWarnSpy.mock.calls) {
      expect(JSON.stringify(call)).not.toContain(secret);
      expect(JSON.stringify(call)).not.toContain("console sink is broken");
      expect(call[0]).toBe("[ReactRoot] root error report could not be emitted");
      expect(call[1]).toEqual({
        recordedFailures: expect.any(Number),
      });
    }
    // And the primary sink was never re-entered after its first failure.
    expect(throwingSink).toHaveBeenCalledTimes(2);

    // The accessor hands out a DEFENSIVE COPY, not the live backing array.
    // Freezing (or mutating) a snapshot therefore cannot reach the internal
    // trail, so a later recording push still cannot throw from the root
    // callback — pinning the runtime guarantee the `readonly` type can't.
    const snapshot = Object.freeze(recordedRootReportFailures());
    expect(recordedRootReportFailures()).not.toBe(snapshot);
    expect(() => rootOptions.onUncaughtError(error, {} as ErrorInfo)).not.toThrow();
    expect(recordedRootReportFailures().length).toBe(failuresBefore + 3);
  });
});
