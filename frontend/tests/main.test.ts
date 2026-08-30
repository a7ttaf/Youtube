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

  it("uses fixed categories and ignores caught, uncaught, and recoverable payloads", async () => {
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

    expect(consoleErrorSpy.mock.calls).toEqual([
      ["[ReactRoot] uncaught_render_failure"],
      ["[ReactRoot] recoverable_render_failure"],
    ]);
    expect(JSON.stringify(consoleErrorSpy.mock.calls)).not.toMatch(
      /root-message-secret|root-stack-secret|component-secret|recoverable-secret/u,
    );
  });
});
