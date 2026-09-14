import { describe, expect, it } from "vitest";

describe("Request compatibility setup", () => {
  it("preserves cancellation for the jsdom AbortSignal accepted by the adapter", () => {
    const controller = new AbortController();
    const request = new Request("http://ums.local/router-transition", {
      signal: controller.signal,
    });

    // Request follows the caller's signal with its own native signal; it does
    // not merely return the cross-realm object from an overridden getter.
    expect(request.signal).not.toBe(controller.signal);
    expect(request.signal.aborted).toBe(false);

    controller.abort("superseded navigation");

    expect(request.signal.aborted).toBe(true);
    expect(request.signal.reason).toBe("superseded navigation");
  });

  it("propagates a jsdom abort through clone() and native fetch()", async () => {
    const controller = new AbortController();
    const request = new Request("data:text/plain,should-not-load", {
      signal: controller.signal,
    });
    const clone = request.clone();

    controller.abort();

    expect(request.signal.aborted).toBe(true);
    expect(clone.signal.aborted).toBe(true);
    expect(clone.signal.reason).toMatchObject({ name: "AbortError" });
    await expect(fetch(clone)).rejects.toMatchObject({ name: "AbortError" });
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
