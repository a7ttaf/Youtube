import { describe, expect, it } from "vitest";

describe("Request compatibility setup", () => {
  it("preserves cancellation for the jsdom AbortSignal accepted by the adapter", () => {
    const controller = new AbortController();
    const request = new Request("http://ums.local/router-transition", {
      signal: controller.signal,
    });

    expect(request.signal).toBe(controller.signal);
    expect(request.signal.aborted).toBe(false);

    controller.abort("superseded navigation");

    expect(request.signal.aborted).toBe(true);
    expect(request.signal.reason).toBe("superseded navigation");
  });

  it("passes a native-compatible AbortSignal through with its aborted state", () => {
    const nativeSignal = new Request(
      "http://ums.local/native-signal-source",
    ).signal;
    const NativeAbortSignal = nativeSignal.constructor as typeof AbortSignal;
    const abortedSignal = NativeAbortSignal.abort("already cancelled");

    const request = new Request("http://ums.local/native-signal-target", {
      signal: abortedSignal,
    });

    expect(request.signal.aborted).toBe(true);
    expect(request.signal.reason).toBe("already cancelled");
  });
});
