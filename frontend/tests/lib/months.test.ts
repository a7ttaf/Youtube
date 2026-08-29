import { describe, expect, it } from "vitest";

import { DEFAULT_MONTH, MONTH_OPTIONS, WRITE_DEFAULT_MONTH } from "@/components/srcc/shared";
import {
  MONTH_WINDOW_SIZE,
  currentMonthKey,
  lastCompleteMonthKey,
  monthKey,
  monthKeyOfDateInput,
  monthKeyLabel,
  rollingMonthWindow,
} from "@/lib/months";

const MONTH_KEY_SHAPE = /^\d{4}-(0[1-9]|1[0-2])$/;

describe("rollingMonthWindow", () => {
  it("rolls back across a YEAR boundary instead of underflowing the month", () => {
    // now = 15 Jan 2027 (local). The three months before January belong to the
    // PREVIOUS year, which naive `monthIndex - back` formatting cannot express.
    expect(rollingMonthWindow(4, new Date(2027, 0, 15))).toEqual([
      "2027-01",
      "2026-12",
      "2026-11",
      "2026-10",
    ]);
  });

  it("rolls back across MULTIPLE year boundaries", () => {
    expect(rollingMonthWindow(14, new Date(2027, 1, 3))).toEqual([
      "2027-02",
      "2027-01",
      "2026-12",
      "2026-11",
      "2026-10",
      "2026-09",
      "2026-08",
      "2026-07",
      "2026-06",
      "2026-05",
      "2026-04",
      "2026-03",
      "2026-02",
      "2026-01",
    ]);
  });

  it("stays inside one year when the window does not cross January", () => {
    expect(rollingMonthWindow(4, new Date(2026, 7, 27))).toEqual([
      "2026-08",
      "2026-07",
      "2026-06",
      "2026-05",
    ]);
  });

  it("defaults to a four-month window and to the current clock", () => {
    expect(MONTH_WINDOW_SIZE).toBe(4);
    const now = new Date(2026, 10, 30);
    expect(rollingMonthWindow(undefined, now)).toHaveLength(4);
    expect(rollingMonthWindow(MONTH_WINDOW_SIZE, now)[0]).toBe(currentMonthKey(now));
    // No `now` argument at all: the window still has the default shape and its
    // head is the month the clock is in right now.
    expect(rollingMonthWindow()).toHaveLength(MONTH_WINDOW_SIZE);
  });

  it("yields an empty window for a non-positive size rather than throwing", () => {
    expect(rollingMonthWindow(0, new Date(2027, 0, 15))).toEqual([]);
    expect(rollingMonthWindow(-3, new Date(2027, 0, 15))).toEqual([]);
  });

  it("always emits zero-padded YYYY-MM keys, newest first", () => {
    const window = rollingMonthWindow(12, new Date(2026, 4, 9));
    for (const key of window) {
      expect(key).toMatch(MONTH_KEY_SHAPE);
    }
    // Lexical order equals chronological order for zero-padded YYYY-MM, so a
    // strictly descending sort proves both the padding and the newest-first
    // ordering in one assertion.
    expect(window).toEqual([...window].sort().reverse());
  });
});

describe("currentMonthKey", () => {
  it("reads LOCAL date components, not UTC ones", () => {
    // These two instants are the first and last local instants of Jan 2027. In
    // any timezone with a non-zero UTC offset, exactly one of them falls in a
    // different UTC month, so a getUTC*-based implementation fails one branch
    // whichever side of UTC the runner sits on.
    expect(currentMonthKey(new Date(2027, 0, 1, 0, 0, 0, 0))).toBe("2027-01");
    expect(currentMonthKey(new Date(2027, 0, 31, 23, 59, 59, 999))).toBe("2027-01");
  });

  it("zero-pads single-digit months", () => {
    expect(currentMonthKey(new Date(2026, 0, 5))).toBe("2026-01");
    expect(currentMonthKey(new Date(2026, 8, 5))).toBe("2026-09");
    expect(currentMonthKey(new Date(2026, 9, 5))).toBe("2026-10");
    expect(currentMonthKey(new Date(2026, 11, 5))).toBe("2026-12");
  });
});

describe("lastCompleteMonthKey", () => {
  it("steps back across the YEAR boundary: January yields the previous December", () => {
    // now = 9 Jan 2027 (local). The last COMPLETE month is December 2026 — the
    // case a naive `month - 1` on the same year would render as "2027-00".
    expect(lastCompleteMonthKey(new Date(2027, 0, 9))).toBe("2026-12");
    // The boundary holds at both local edges of January, so no UTC offset can
    // shift the answer into the current month.
    expect(lastCompleteMonthKey(new Date(2027, 0, 1, 0, 0, 0, 0))).toBe("2026-12");
    expect(lastCompleteMonthKey(new Date(2027, 0, 31, 23, 59, 59, 999))).toBe("2026-12");
  });

  it("is the month BEFORE the current one, mid-year and zero-padded", () => {
    expect(lastCompleteMonthKey(new Date(2026, 7, 27))).toBe("2026-07");
    expect(lastCompleteMonthKey(new Date(2026, 9, 1))).toBe("2026-09");
    // Never the in-progress month: that is the whole point of the write default.
    const now = new Date(2026, 4, 15);
    expect(lastCompleteMonthKey(now)).not.toBe(currentMonthKey(now));
  });

  it("is always a SELECTABLE option — MONTH_OPTIONS[1] by construction", () => {
    // The write default must be offered by the same <select> the read views
    // list, or the seeded value would not match any <option>.
    expect(MONTH_OPTIONS).toContain(lastCompleteMonthKey());
    expect(lastCompleteMonthKey()).toBe(MONTH_OPTIONS[1]);
    // Same relationship on a pinned clock, independent of when the module loaded.
    const now = new Date(2026, 0, 4);
    expect(lastCompleteMonthKey(now)).toBe(
      rollingMonthWindow(MONTH_WINDOW_SIZE, now)[1],
    );
  });
});

describe("monthKey", () => {
  it("normalises an out-of-range zero-based month index", () => {
    expect(monthKey(2027, -1)).toBe("2026-12");
    expect(monthKey(2027, -13)).toBe("2025-12");
    expect(monthKey(2026, 12)).toBe("2027-01");
  });
});

describe("monthKeyOfDateInput", () => {
  it("takes the month straight from a date-input value, no Date parsing", () => {
    expect(monthKeyOfDateInput("2026-03-21")).toBe("2026-03");
    expect(monthKeyOfDateInput("2026-12-31")).toBe("2026-12");
    expect(monthKeyOfDateInput("  2026-07-04  ")).toBe("2026-07");
  });

  it("returns an empty string for anything that is not a complete YYYY-MM-DD value", () => {
    expect(monthKeyOfDateInput("")).toBe("");
    expect(monthKeyOfDateInput("2026-03")).toBe("");
    expect(monthKeyOfDateInput("2026-03-21T00:00:00Z")).toBe("");
    expect(monthKeyOfDateInput("not-a-date")).toBe("");
  });
});

describe("monthKeyLabel", () => {
  it("renders a month key as its short human label", () => {
    expect(monthKeyLabel("2026-03")).toBe("Mar 2026");
    expect(monthKeyLabel("2025-12")).toBe("Dec 2025");
    expect(monthKeyLabel("2027-01")).toBe("Jan 2027");
  });

  it("echoes an unrecognised key instead of rendering Invalid Date", () => {
    expect(monthKeyLabel("")).toBe("");
    expect(monthKeyLabel("2026-3")).toBe("2026-3");
    expect(monthKeyLabel("not-a-month")).toBe("not-a-month");
  });
});

describe("shared month window exports", () => {
  it("keeps the shapes consumers depend on", () => {
    expect(Array.isArray(MONTH_OPTIONS)).toBe(true);
    expect(MONTH_OPTIONS).toHaveLength(MONTH_WINDOW_SIZE);
    for (const option of MONTH_OPTIONS) {
      expect(option).toMatch(MONTH_KEY_SHAPE);
    }
  });

  it("makes DEFAULT_MONTH the first option and the CURRENT month", () => {
    expect(DEFAULT_MONTH).toBe(MONTH_OPTIONS[0]);
    // Not a frozen literal: the published window is the one the clock implies.
    expect(MONTH_OPTIONS).toEqual(rollingMonthWindow(MONTH_WINDOW_SIZE, new Date()));
  });

  it("derives WRITE_DEFAULT_MONTH from the frozen window, never a second clock read", () => {
    // The write default (connector report_month / AdSense payment month) must
    // come from the SAME module-load snapshot as the option list: derived FROM
    // MONTH_OPTIONS, it can never be a month the frozen <select> cannot
    // display (PR #211 review — a live clock read at view mount let a
    // two-month-old tab seed an invisible month).
    expect(WRITE_DEFAULT_MONTH).toBe(MONTH_OPTIONS[1]);
    expect(MONTH_OPTIONS).toContain(WRITE_DEFAULT_MONTH);
    expect(WRITE_DEFAULT_MONTH).not.toBe(DEFAULT_MONTH);
  });
});
