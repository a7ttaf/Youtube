import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { createRootMock, renderMock } = vi.hoisted(() => ({
  createRootMock: vi.fn(),
  renderMock: vi.fn(),
}));

vi.mock("react-dom/client", () => ({ createRoot: createRootMock }));

describe("React root diagnostic privacy", () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="root"></div>';
    createRootMock.mockReturnValue({ render: renderMock });
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.resetModules();
    createRootMock.mockReset();
    renderMock.mockReset();
  });

  it("reports only safe categories and opaque IDs for uncaught/recoverable errors", async () => {
    const consoleErrorSpy = vi
      .spyOn(console, "error")
      .mockImplementation(() => undefined);
    await import("@/main");

    const options = createRootMock.mock.calls[0]?.[1] as {
      onCaughtError: (...args: unknown[]) => void;
      onUncaughtError: (...args: unknown[]) => void;
      onRecoverableError: (...args: unknown[]) => void;
    };
    const secret = new Error("root-message-secret");
    secret.stack = "root-stack-secret";
    options.onCaughtError(secret, { componentStack: "caught-component-secret" });
    options.onUncaughtError(secret, { componentStack: "uncaught-component-secret" });
    options.onRecoverableError(secret, { componentStack: "recoverable-secret" });

    expect(consoleErrorSpy).toHaveBeenCalledTimes(2);
    expect(consoleErrorSpy.mock.calls[0]?.[0]).toBe(
      "[ReactRoot] uncaught render failure",
    );
    expect(consoleErrorSpy.mock.calls[1]?.[0]).toBe(
      "[ReactRoot] recoverable render failure",
    );
    for (const call of consoleErrorSpy.mock.calls) {
      expect(call).toHaveLength(2);
      expect(call[1]).toEqual({
        category: "Error",
        correlationId: expect.any(String),
      });
      expect((call[1] as { correlationId: string }).correlationId).toMatch(
        /^(?:[0-9a-f-]{36}|view-error-[0-9a-z-]+)$/iu,
      );
    }
    expect(JSON.stringify(consoleErrorSpy.mock.calls)).not.toMatch(
      /root-message-secret|root-stack-secret|component-secret|recoverable-secret/u,
    );
  });
});
