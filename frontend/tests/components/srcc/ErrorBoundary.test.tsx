import { fireEvent, render, screen, within } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ErrorBoundary, {
  correlationIdOf,
} from "@/components/srcc/ErrorBoundary";

let consoleErrorSpy: ReturnType<typeof vi.spyOn>;

beforeEach(() => {
  consoleErrorSpy = vi.spyOn(console, "error").mockImplementation(() => undefined);
});

afterEach(() => {
  vi.restoreAllMocks();
});

const renderBoundary = (child: ReactNode) =>
  render(<ErrorBoundary>{child}</ErrorBoundary>, {
    onCaughtError: () => undefined,
  });

const explodingComponent = (thrown: unknown): (() => ReactNode) =>
  function Exploding(): ReactNode {
    throw thrown;
  };

const boundaryReports = (): unknown[][] =>
  consoleErrorSpy.mock.calls.filter((call) =>
    String(call[0]).includes("[ErrorBoundary] view render failed"),
  );

const SAFE_REFERENCE_PATTERN =
  /^Reference: (?:[0-9a-f-]{36}|view-error-[0-9a-z-]+)$/iu;

describe("ErrorBoundary", () => {
  it("passes a healthy subtree through", () => {
    renderBoundary(<p>healthy content</p>);

    expect(screen.getByText("healthy content")).toBeInTheDocument();
    expect(screen.queryByTestId("view-error-fallback")).not.toBeInTheDocument();
    expect(consoleErrorSpy).not.toHaveBeenCalled();
  });

  it("reports only an allowlisted category and opaque correlation ID", () => {
    const sensitive = new TypeError("payment-token=message-secret");
    sensitive.stack = "stack-secret at /private/customer-ledger.ts:91";
    const Exploding = explodingComponent(sensitive);
    const onReport = vi.fn();

    render(
      <ErrorBoundary onReport={onReport}>
        <Exploding />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    );

    const fallback = screen.getByTestId("view-error-fallback");
    expect(fallback).toHaveAttribute("role", "alert");
    expect(fallback).toHaveFocus();
    expect(within(fallback).getByText("This section could not be displayed"))
      .toBeInTheDocument();
    expect(within(fallback).getByText("TypeError")).toBeInTheDocument();
    expect(fallback).toHaveTextContent("A write may already have committed");
    expect(screen.getByTestId("view-error-correlation-id").textContent)
      .toMatch(SAFE_REFERENCE_PATTERN);
    expect(fallback.textContent).not.toMatch(
      /payment-token=message-secret|stack-secret|customer-ledger/u,
    );
    expect(onReport).toHaveBeenCalledTimes(1);
    expect(onReport).toHaveBeenCalledWith({
      category: "TypeError",
      correlationId: expect.any(String),
    });
    expect(boundaryReports()).toHaveLength(0);
  });

  it("logs only the safe report when no approved sink is supplied", () => {
    const sensitive = new RangeError("private revenue amount 123.45");
    const Exploding = explodingComponent(sensitive);
    renderBoundary(<Exploding />);

    const reports = boundaryReports();
    expect(reports).toHaveLength(1);
    expect(reports[0]).toHaveLength(2);
    expect(reports[0]?.[1]).toEqual({
      category: "RangeError",
      correlationId: expect.any(String),
    });
    expect(JSON.stringify(reports)).not.toContain(sensitive.message);
  });

  it("does not inspect or expose a hostile non-Error payload", () => {
    const Exploding = explodingComponent({
      name: "ObjectSecretError",
      message: "bank-account=object-secret",
      stack: "object-stack-secret",
      data: { accessToken: "object-token-secret" },
    });
    renderBoundary(<Exploding />);

    const fallback = screen.getByTestId("view-error-fallback");
    expect(within(fallback).getByText("Error")).toBeInTheDocument();
    expect(fallback.textContent).not.toMatch(
      /ObjectSecretError|object-secret|object-stack-secret|object-token-secret/u,
    );
    expect(JSON.stringify(boundaryReports())).not.toMatch(/object-secret/u);
  });

  it("collapses custom and hostile Error.name values to Error", () => {
    const custom = new Error("custom-name-message-secret");
    custom.name = "RevenueRow-42";
    const CustomExploding = explodingComponent(custom);
    const { unmount } = renderBoundary(<CustomExploding />);

    expect(within(screen.getByTestId("view-error-fallback")).getByText("Error"))
      .toBeInTheDocument();
    expect(document.body.textContent).not.toContain("RevenueRow-42");
    unmount();

    const hostile = new Error("getter-message-secret");
    Object.defineProperty(hostile, "name", {
      configurable: true,
      get: () => {
        throw new Error("name-getter-secret");
      },
    });
    const HostileExploding = explodingComponent(hostile);
    renderBoundary(<HostileExploding />);

    expect(within(screen.getByTestId("view-error-fallback")).getByText("Error"))
      .toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/getter-message-secret|name-getter-secret/u);
  });

  it("uses fresh injected crypto values when UUID generation is unavailable", () => {
    const values = [
      [0x11111111, 0x22222222, 0x33333333, 0x44444444],
      [0xaaaaaaaa, 0xbbbbbbbb, 0xcccccccc, 0xdddddddd],
    ];
    const getRandomValues = vi.fn((buffer: Uint32Array): Uint32Array => {
      const next = values.shift();
      if (!next) throw new Error("test entropy exhausted");
      buffer.set(next);
      return buffer;
    });

    const first = correlationIdOf({ getRandomValues });
    const second = correlationIdOf({ getRandomValues });

    expect(getRandomValues).toHaveBeenCalledTimes(2);
    expect(first).toBe("view-error-11111111222222223333333344444444");
    expect(second).toBe("view-error-aaaaaaaabbbbbbbbccccccccdddddddd");
    expect(first).not.toBe(second);
  });

  it("keeps the correlation fallback nonthrowing when entropy sources are hostile", () => {
    const hostileSource = {
      randomUUID: (): string => {
        throw new Error("uuid secret");
      },
      getRandomValues: (): Uint32Array => {
        throw new Error("random secret");
      },
    };
    vi.spyOn(Math, "random").mockImplementation(() => {
      throw new Error("math secret");
    });

    expect(() => correlationIdOf(hostileSource)).not.toThrow();
    expect(correlationIdOf(hostileSource)).toMatch(/^view-error-noentropy-[0-9a-z]+$/iu);
  });

  it("reloads for reconciliation without retrying the child in place", () => {
    const committedWrite = vi.fn();
    let writeRecorded = false;
    const WriteThenThrow = (): ReactNode => {
      if (!writeRecorded) {
        writeRecorded = true;
        committedWrite();
      }
      throw new TypeError("render failed after committed write");
    };
    const onReload = vi.fn();

    render(
      <ErrorBoundary onReload={onReload}>
        <WriteThenThrow />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    );

    fireEvent.click(screen.getByRole("button", { name: "Reload and reconcile" }));

    expect(onReload).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("view-error-fallback")).toBeInTheDocument();
    expect(committedWrite).toHaveBeenCalledTimes(1);
  });

  it("withholds reconciliation until the unabortable-write latch settles", () => {
    const Exploding = explodingComponent(new Error("write-result-secret"));
    const onReload = vi.fn();
    const { rerender } = render(
      <ErrorBoundary recoveryDisabled onReload={onReload}>
        <Exploding />
      </ErrorBoundary>,
      { onCaughtError: () => undefined },
    );

    const button = screen.getByRole("button", { name: "Reload and reconcile" });
    expect(button).toBeDisabled();
    expect(button).toHaveAttribute("title", expect.stringMatching(/active write/iu));
    fireEvent.click(button);
    expect(onReload).not.toHaveBeenCalled();

    rerender(
      <ErrorBoundary recoveryDisabled={false} onReload={onReload}>
        <Exploding />
      </ErrorBoundary>,
    );
    expect(button).toBeEnabled();
    fireEvent.click(button);

    expect(onReload).toHaveBeenCalledTimes(1);
    expect(screen.getByTestId("view-error-fallback")).toBeInTheDocument();
  });
});
