import { describe, expect, it } from "vitest";

import { confidenceDisplay } from "@/lib/confidence";

describe("confidenceDisplay", () => {
  it("prefers a provided API human label over the prefix map", () => {
    // Code prefix would map to "Reconciled"/green, but the API label wins.
    expect(confidenceDisplay("B_RECONCILED", "HIGH")).toEqual({
      label: "HIGH",
      tone: "green",
    });
    expect(confidenceDisplay("A_VERIFIED", "MEDIUM")).toEqual({
      label: "MEDIUM",
      tone: "amber",
    });
    expect(confidenceDisplay("A_VERIFIED", "LOW")).toEqual({
      label: "LOW",
      tone: "red",
    });
  });

  it("ignores a blank API label and falls back to the prefix map", () => {
    expect(confidenceDisplay("A_VERIFIED", "  ")).toEqual({
      label: "Verified",
      tone: "green",
    });
    expect(confidenceDisplay("A_VERIFIED", null)).toEqual({
      label: "Verified",
      tone: "green",
    });
  });

  it("maps each code prefix to its human label and tone", () => {
    expect(confidenceDisplay("A_VERIFIED")).toEqual({ label: "Verified", tone: "green" });
    expect(confidenceDisplay("B_RECONCILED")).toEqual({ label: "Reconciled", tone: "green" });
    expect(confidenceDisplay("C_ESTIMATED")).toEqual({ label: "Estimated", tone: "amber" });
    expect(confidenceDisplay("D_MODELLED")).toEqual({ label: "Estimated", tone: "amber" });
    expect(confidenceDisplay("E_MISSING")).toEqual({ label: "Missing", tone: "red" });
  });

  it("returns the raw code with a neutral tone for an unknown prefix", () => {
    expect(confidenceDisplay("Z_WEIRD")).toEqual({ label: "Z_WEIRD", tone: "blue" });
    expect(confidenceDisplay("")).toEqual({ label: "—", tone: "blue" });
  });
});
