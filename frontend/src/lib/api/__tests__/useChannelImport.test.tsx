import { renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelImportResult } from "@/lib/api/types";
import { useChannelImport } from "@/lib/api/useChannelImport";
import { TenantProvider } from "@/contexts/TenantContext";

const wrapper = ({ children }: { children: React.ReactNode }) => {
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
};

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

const requireFetchArgs = () => {
  const args = fetchMock().mock.calls.at(-1);
  if (!args) throw new Error("expected fetch to have been called");
  return args;
};

const requireFormDataBody = (init: unknown): FormData => {
  const body = (init as RequestInit).body;
  if (!(body instanceof FormData)) {
    throw new Error("expected the request body to be FormData");
  }
  return body;
};

const CSV_TEXT = "youtube_channel_id,channel_name\nUCaaaaaaaaaaaaaaaaaaaaaa,Alpha Channel\n";

const rosterFile = () => {
  return new File([CSV_TEXT], "roster.csv", { type: "text/csv" });
};

// The revenue_source_status literals this suite asserts on. Declared HERE and
// not imported from the module under test on purpose: a test that reuses the
// source's own constant cannot catch the source changing the value. Naming
// them once keeps the file internally consistent while still pinning the wire
// contract independently.
const REQUIRED_STATUS = "MISSING_REVENUE_SOURCE";
const OPTIONAL_STATUS = "PERFORMANCE_ONLY";
const OFFICIAL_STATUS = "OFFICIAL_CMS_REVENUE";

const DRY_RUN_RESULT: ChannelImportResult = {
  dry_run: true,
  content_owner_id: "COabc",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
  plan_fingerprint: "plan-abc",
  rows: [
    {
      row_number: 1,
      youtube_channel_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
      outcome: "CREATE",
      channel_name: "Alpha Channel",
      group_id: null,
      group_action: null,
      revenue_required: true,
      revenue_source_status: { from: null, to: REQUIRED_STATUS },
      changes: {},
      reason: null,
    },
    {
      row_number: 2,
      youtube_channel_id: "UCbbbbbbbbbbbbbbbbbbbbbb",
      outcome: "UPDATE",
      channel_name: "Beta Channel",
      group_id: "g1",
      group_action: "CREATE",
      revenue_required: false,
      revenue_source_status: null,
      changes: {
        channel_name: { from: "Old Beta", to: "Beta Channel" },
        revenue_required: { from: true, to: false },
      },
      reason: null,
    },
  ],
};

const APPLY_RESULT: ChannelImportResult = { ...DRY_RUN_RESULT, dry_run: false };

// The all-or-nothing apply rejection: a 422 whose `detail` is the full
// ChannelImportResult payload (channels.py:688-689), here with the ERROR row
// that blocked the apply.
const BLOCKED_APPLY_DETAIL: ChannelImportResult = {
  dry_run: false,
  content_owner_id: "COabc",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 0, ERROR: 1 },
  plan_fingerprint: "plan-blocked",
  rows: [
    {
      row_number: 1,
      youtube_channel_id: null,
      outcome: "ERROR",
      channel_name: null,
      group_id: null,
      group_action: null,
      revenue_required: null,
      revenue_source_status: null,
      changes: {},
      reason: "missing youtube_channel_id",
    },
  ],
};

describe("useChannelImport", () => {
  it("POSTs /channels/import as multipart FormData with the mapped fields", async () => {
    fetchMock().mockResolvedValue(jsonResponse(DRY_RUN_RESULT));
    const { result } = renderHook(() => useChannelImport(), { wrapper });
    const file = rosterFile();

    const response = await result.current({
      file,
      contentOwnerId: "COabc",
      dryRun: true,
      reason: "monthly roster import",
    });

    const [url, init] = requireFetchArgs();
    expect(urlOf(url)).toBe("/channels/import");
    expect((init as RequestInit).method).toBe("POST");
    const form = requireFormDataBody(init);
    // Exactly the five wire fields. cms_status is sent EXPLICITLY, matching
    // the route's own default: the response echoes it, and a value the client
    // never sent is one it cannot check the echo against (review #184).
    expect([...form.keys()].sort()).toEqual([
      "cms_status",
      "content_owner_id",
      "dry_run",
      "file",
      "reason",
    ]);
    expect(form.get("cms_status")).toBe("INSIDE_CMS");
    expect(form.get("content_owner_id")).toBe("COabc");
    expect(form.get("dry_run")).toBe("true");
    expect(form.get("reason")).toBe("monthly roster import");
    // The file part must be the caller's File object appended verbatim (same
    // identity — so its name and CSV bytes reach the wire untouched).
    const filePart = form.get("file");
    expect(filePart).toBeInstanceOf(File);
    expect(filePart).toBe(file);
    expect((filePart as File).name).toBe("roster.csv");
    // No JSON Content-Type: the FormData must pass through verbatim so fetch
    // sets the multipart boundary itself.
    const headers = new Headers((init as RequestInit).headers);
    expect(headers.get("Content-Type")).toBeNull();
    expect(response).toEqual(DRY_RUN_RESULT);
  });

  it("round-trips dryRun: false as the apply flag", async () => {
    fetchMock().mockResolvedValue(jsonResponse(APPLY_RESULT));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    const response = await result.current({
      file: rosterFile(),
      contentOwnerId: "COabc",
      dryRun: false,
      reason: "monthly roster import",
    });

    const form = requireFormDataBody(requireFetchArgs()[1]);
    expect(form.get("dry_run")).toBe("false");
    expect(response).toEqual(APPLY_RESULT);
  });

  it("propagates the 422 blocked-apply ApiError carrying the full plan payload", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: BLOCKED_APPLY_DETAIL }, 422),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({
      name: "ApiError",
      status: 422,
      body: { detail: BLOCKED_APPLY_DETAIL },
    });
  });
  it("rejects a 2xx whose body is not a usable plan", async () => {
    // client.post CASTS the body to its type parameter; it does not validate.
    // A legacy or malformed 200 missing plan_fingerprint would otherwise be
    // accepted as a preview, and the next Apply would send NO
    // expected_plan_fingerprint — silently downgrading the write to the
    // backend's unbound, file-wins path (review #184).
    const noFingerprint = { ...DRY_RUN_RESULT } as Record<string, unknown>;
    delete noFingerprint.plan_fingerprint;
    fetchMock().mockResolvedValue(jsonResponse(noFingerprint));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a 2xx carrying an empty JSON body", async () => {
    // The apply case: an empty object would advance the flow with no usable
    // result AFTER the write may have committed. Rejecting sends it down the
    // indeterminate path instead — this is not an ApiError, so it is not on
    // the flow's definite-rejection list.
    fetchMock().mockResolvedValue(jsonResponse({}));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a 2xx whose HEADER fields are malformed, not just its rows", async () => {
    // content_owner_id and cms_status are RENDERED by PreviewStep and
    // AppliedStep. A payload with perfectly good rows, counts and fingerprint
    // but content_owner_id: {} passed a rows-only check and then threw inside
    // React — and after an apply that throw lands where the write may already
    // have committed, escaping the indeterminate handling built for exactly
    // that case (review #184, codex P2).
    const malformedHeader = {
      ...DRY_RUN_RESULT,
      content_owner_id: {},
    } as Record<string, unknown>;
    fetchMock().mockResolvedValue(jsonResponse(malformedHeader));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a plan whose counts are not counts", async () => {
    // CountsStrip coerces during `value > 0`, so a string renders as a plan
    // total and a negative or NaN silently HIDES an outcome the operator
    // needed to see — on a payload that stays applicable (review #184).
    const whole = { CREATE: 0, UPDATE: 0, UNCHANGED: 0, ERROR: 0 };
    const badCounts = [
      { ...whole, CREATE: "1" },
      { ...whole, CREATE: -1 },
      { ...whole, CREATE: 1.5 },
      { ...whole, CREATE: Number.NaN },
      // A key the strip has no label for is a plan shape this UI cannot read.
      { ...whole, NOT_AN_OUTCOME: 1 },
      // Vacuously "valid" under a bare `every`, and a shape the planner cannot
      // emit: it seeds all four outcomes at 0 unconditionally.
      {},
      // Partial, for the same reason — CountsStrip would silently omit a total
      // the operator is entitled to, including on the Applied screen.
      { CREATE: 1 },
      { CREATE: 1, UPDATE: 1, UNCHANGED: 1 },
    ];

    for (const counts of badCounts) {
      fetchMock().mockResolvedValue(jsonResponse({ ...DRY_RUN_RESULT, counts }));
      const { result } = renderHook(() => useChannelImport(), { wrapper });

      await expect(
        result.current({
          file: rosterFile(),
          contentOwnerId: "COabc",
          dryRun: true,
          reason: "monthly roster import",
        }),
      ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
    }
  });

  it("rejects a row whose group EFFECT is undisclosed", async () => {
    // Field-by-field checks each pass on
    // {outcome: "UPDATE", group_id: "g1", group_action: null}: the id is a
    // string, the action is a legal null. Together they are a writable row
    // whose finance-scope effect — mint a new SECTOR group, or join an
    // existing one — was never disclosed, and Apply would stay enabled over
    // it (review #184).
    const undisclosed = {
      ...DRY_RUN_RESULT,
      rows: [{ ...DRY_RUN_RESULT.rows[1], group_id: "g1", group_action: null }],
    };
    fetchMock().mockResolvedValue(jsonResponse(undisclosed));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a group action on a row carrying no group", async () => {
    // The other direction: an action with no key claims a group write the
    // operator has no way to identify.
    const orphanAction = {
      ...DRY_RUN_RESULT,
      rows: [{ ...DRY_RUN_RESULT.rows[0], group_id: null, group_action: "CREATE" }],
    };
    fetchMock().mockResolvedValue(jsonResponse(orphanAction));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a writable row whose group key the parser could not have emitted", async () => {
    // `_text_fields` normalizes the cell before anything else sees it: a blank
    // or whitespace-only Group_ID becomes null, so a non-null key on a writable
    // row is always trimmed and non-blank. `" "` is a string, so it satisfies
    // isNullableString, and it pairs with a non-null group_action so the
    // biconditional passes too — the Group cell then renders a blank identifier
    // with Apply enabled, while the bound request writes the REAL CSV group the
    // retained fingerprint stands for (review #184, codex P2).
    const blankKey = {
      ...DRY_RUN_RESULT,
      rows: [{ ...DRY_RUN_RESULT.rows[1], group_id: "   ", group_action: "JOIN" }],
      counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
    };
    fetchMock().mockResolvedValue(jsonResponse(blankKey));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a group key the parser would have turned into an ERROR row", async () => {
    // The other two normalizations, which are ERROR rows upstream rather than
    // nulls: a NUL-bearing key, and one past MAX_GROUP_ID_CHARS (255).
    for (const groupId of ["mus\u0000ic", "g".repeat(256)]) {
      fetchMock().mockResolvedValue(
        jsonResponse({
          ...DRY_RUN_RESULT,
          rows: [{ ...DRY_RUN_RESULT.rows[1], group_id: groupId, group_action: "JOIN" }],
          counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
        }),
      );
      const { result } = renderHook(() => useChannelImport(), { wrapper });

      await expect(
        result.current({
          file: rosterFile(),
          contentOwnerId: "COabc",
          dryRun: true,
          reason: "monthly roster import",
        }),
      ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
    }
  });

  it("still accepts a group key carrying INTERIOR spaces", async () => {
    // The parser strips only the ENDS, so "Music EMEA" is a key the backend can
    // emit. Pinned because the obvious over-tightening — no spaces at all —
    // would refuse legitimate plans, and nothing else in the suite would catch
    // it.
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        rows: [{ ...DRY_RUN_RESULT.rows[1], group_id: "Music EMEA", group_action: "JOIN" }],
        counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ rows: [{ group_id: "Music EMEA" }] });
  });

  it("rejects a writable name the parser would have normalized away", async () => {
    // _parse_text_fields strips every name and ERRORs a NUL-bearing row, so
    // " Alpha " and "Alpha\u0000" are values the backend cannot emit on a
    // writable row. A non-blank TRIM is not enough: a padded name renders on
    // the preview while the bound apply writes the normalized real one — the
    // same substitution as a blank cell, just quieter (review #184, codex P2).
    for (const channelName of [" Alpha Channel ", "Alpha\u0000Channel", "Alpha "]) {
      fetchMock().mockResolvedValue(
        jsonResponse({
          ...DRY_RUN_RESULT,
          counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
          rows: [{ ...DRY_RUN_RESULT.rows[0], channel_name: channelName }],
        }),
      );
      const { result } = renderHook(() => useChannelImport(), { wrapper });

      await expect(
        result.current({
          file: rosterFile(),
          contentOwnerId: "COabc",
          dryRun: true,
          reason: "monthly roster import",
        }),
      ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
    }
  });

  it("still accepts a name carrying INTERIOR spaces and punctuation", async () => {
    // The parser strips only the ENDS, so these are names it emits every day.
    // Pinned because the obvious over-tightening — no spaces, or an alphabet —
    // would refuse most of a real catalogue.
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
        rows: [{ ...DRY_RUN_RESULT.rows[0], channel_name: "قناة الدراما - UMS" }],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ rows: [{ channel_name: "قناة الدراما - UMS" }] });
  });

  it("rejects two rows claiming DIFFERENT effects for one group key", async () => {
    // Every per-row predicate is row-local, so this passes all of them: two
    // writable rows share cms-tv, one promising a NEW SECTOR group and the
    // other a join of an existing one. The backend derives the action from
    // that key's existence for the request owner — one fact per key — and the
    // apply batches the key into a single group operation, so a plan showing
    // two effects is showing one the import will not perform, while the
    // retained fingerprint authorises the one real effect (review #184).
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 0, UPDATE: 2, UNCHANGED: 0, ERROR: 0 },
        rows: [
          { ...DRY_RUN_RESULT.rows[1], row_number: 1, group_id: "cms-tv", group_action: "CREATE" },
          {
            ...DRY_RUN_RESULT.rows[1],
            row_number: 2,
            youtube_channel_id: "UCcccccccccccccccccccccc",
            group_id: "cms-tv",
            group_action: "JOIN",
          },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("accepts many rows AGREEING on one group key", async () => {
    // The anti-vacuity half: a roster listing several channels under one new
    // key is the ordinary case, and all of them are legitimately CREATE.
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 0, UPDATE: 2, UNCHANGED: 0, ERROR: 0 },
        rows: [
          { ...DRY_RUN_RESULT.rows[1], row_number: 1, group_id: "cms-tv", group_action: "CREATE" },
          {
            ...DRY_RUN_RESULT.rows[1],
            row_number: 2,
            youtube_channel_id: "UCcccccccccccccccccccccc",
            group_id: "cms-tv",
            group_action: "CREATE",
          },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { UPDATE: 2 } });
  });

  it("rejects an ERROR row claiming an identity the planner never sets", async () => {
    // All three ERROR constructions leave channel_name and revenue_required at
    // their None defaults, and the id they DO keep already cleared
    // CHANNEL_ID_PATTERN in the parser. Anything else prints a channel and a
    // revenue flag beside the remediation text and sends the operator to fix
    // the wrong CSV line (review #184, codex P2).
    const errorRow = { ...BLOCKED_APPLY_DETAIL.rows[0] };
    for (const claim of [
      { youtube_channel_id: "bogus" },
      { channel_name: "Alpha Channel" },
      { revenue_required: true },
    ]) {
      fetchMock().mockResolvedValue(
        jsonResponse({ ...BLOCKED_APPLY_DETAIL, dry_run: true, rows: [{ ...errorRow, ...claim }] }),
      );
      const { result } = renderHook(() => useChannelImport(), { wrapper });

      await expect(
        result.current({
          file: rosterFile(),
          contentOwnerId: "COabc",
          dryRun: true,
          reason: "monthly roster import",
        }),
      ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
    }
  });

  it("still accepts the ERROR row shapes the planner DOES emit", async () => {
    // Both of them: a parse failure with no channel to name, and a planning
    // refusal that keeps the id of a row whose identity parsed cleanly.
    for (const identity of [null, "UCaaaaaaaaaaaaaaaaaaaaaa"]) {
      fetchMock().mockResolvedValue(
        jsonResponse({
          ...BLOCKED_APPLY_DETAIL,
          dry_run: true,
          rows: [{ ...BLOCKED_APPLY_DETAIL.rows[0], youtube_channel_id: identity }],
        }),
      );
      const { result } = renderHook(() => useChannelImport(), { wrapper });

      await expect(
        result.current({
          file: rosterFile(),
          contentOwnerId: "COabc",
          dryRun: true,
          reason: "monthly roster import",
        }),
      ).resolves.toMatchObject({ counts: { ERROR: 1 } });
    }
  });

  it("rejects repeated copies of one channel that disagree", async () => {
    // The parser rejects copies disagreeing on the inventory fields, so every
    // surviving copy agrees; and only the FIRST copy owns the inventory
    // decision, later ones planning as UNCHANGED so the apply attaches their
    // group without a second outcome. Both halves, and the ordering rule.
    const first = { ...DRY_RUN_RESULT.rows[1], row_number: 1, group_id: "g1" };
    const membership = {
      ...first,
      row_number: 2,
      outcome: "UNCHANGED",
      changes: {},
      group_id: "g2",
      group_action: "CREATE",
    };
    for (const [label, second] of [
      ["a second NAME", { ...membership, channel_name: "Beta Channel Renamed" }],
      ["a second revenue FLAG", { ...membership, revenue_required: true }],
      ["a second inventory decision", { ...first, row_number: 2, group_id: "g2" }],
    ] as const) {
      fetchMock().mockResolvedValue(
        jsonResponse({
          ...DRY_RUN_RESULT,
          counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 1, ERROR: 0 },
          rows: [first, second],
        }),
      );
      const { result } = renderHook(() => useChannelImport(), { wrapper });

      await expect(
        result.current({
          file: rosterFile(),
          contentOwnerId: "COabc",
          dryRun: true,
          reason: "monthly roster import",
        }),
        `expected ${label} to be refused`,
      ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
    }
  });

  it("accepts one channel repeated across GROUPS, the way a roster does", async () => {
    // The shape this must not break: many-to-many membership, one association
    // per row, the first copy owning the inventory outcome and the rest
    // UNCHANGED. Refusing this would refuse every grouped roster.
    const first = { ...DRY_RUN_RESULT.rows[1], row_number: 1, group_id: "g1" };
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 1, ERROR: 0 },
        rows: [
          first,
          {
            ...first,
            row_number: 2,
            outcome: "UNCHANGED",
            changes: {},
            group_id: "g2",
            group_action: "CREATE",
          },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { UNCHANGED: 1 } });
  });

  it("rejects rows arriving out of row_number order", async () => {
    // plan_channel_import sorts its entries, so a descending payload is one no
    // response can carry — and it puts the operator's rows on screen in an
    // order their file does not have, landing a remediation reason beside the
    // wrong line while every number is individually plausible (review #184).
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        rows: [
          { ...DRY_RUN_RESULT.rows[1], row_number: 2 },
          { ...DRY_RUN_RESULT.rows[0], row_number: 1 },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("still accepts row numbers with GAPS, which a blank CSV line produces", async () => {
    // `enumerate(reader, start=1)` numbers every line of the data section and
    // blank lines are skipped AFTER consuming their index, so 1, 2, 4 is what a
    // roster with a blank line between records emits. The number names the
    // operator's line, which is the point of showing it — requiring 1..N would
    // refuse those rosters outright.
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        rows: [
          { ...DRY_RUN_RESULT.rows[0], row_number: 1 },
          { ...DRY_RUN_RESULT.rows[1], row_number: 4 },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ rows: [{ row_number: 1 }, { row_number: 4 }] });
  });

  it("rejects a cms_status the column cannot hold", async () => {
    // Both sides are values of a column constrained to three literals, so
    // "any string" was never the rule. A GARBAGE prior state renders as the
    // reviewed pre-state while the bound apply writes from the real one
    // (review #184, codex P2).
    for (const change of [
      { from: "GARBAGE", to: "INSIDE_CMS" },
      { from: "OUTSIDE_CMS", to: "ALSO_GARBAGE" },
    ]) {
      fetchMock().mockResolvedValue(
        jsonResponse({
          ...DRY_RUN_RESULT,
          counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
          rows: [
            {
              ...DRY_RUN_RESULT.rows[1],
              revenue_source_status: null,
              changes: { cms_status: change },
            },
          ],
        }),
      );
      const { result } = renderHook(() => useChannelImport(), { wrapper });

      await expect(
        result.current({
          file: rosterFile(),
          contentOwnerId: "COabc",
          dryRun: true,
          reason: "monthly roster import",
        }),
      ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
    }
  });

  it("accepts the cms_status transition an import actually performs", async () => {
    // OUTSIDE_CMS -> INSIDE_CMS is the ordinary case: a channel the CMS now
    // carries. Pinned so the literal set cannot be narrowed to the one value
    // this client sends.
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
        rows: [
          {
            ...DRY_RUN_RESULT.rows[1],
            revenue_source_status: null,
            changes: { cms_status: { from: "OUTSIDE_CMS", to: "INSIDE_CMS" } },
          },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { UPDATE: 1 } });
  });

  it("accepts a group key the BACKEND accepts, counted in code points", async () => {
    // The backend caps len(group_id) at 255 code points; JS .length counts
    // UTF-16 units, so 200 non-BMP characters are 200 to Python and 400 here.
    // The plain .length check refused a roster the backend had already planned,
    // which does not merely warn -- it fails the whole preview and takes the
    // import UI down (review #184, qodo).
    const astralKey = "\u{1F600}".repeat(200);
    expect(astralKey.length).toBe(400);
    expect([...astralKey].length).toBe(200);
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
        rows: [{ ...DRY_RUN_RESULT.rows[1], group_id: astralKey, group_action: "JOIN" }],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ rows: [{ group_id: astralKey }] });
  });

  it("still rejects a group key past the cap in CODE POINTS", async () => {
    // The other half: switching units must not remove the cap. 256 astral
    // characters are 256 code points, which the backend ERRORs.
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
        rows: [
          {
            ...DRY_RUN_RESULT.rows[1],
            group_id: "\u{1F600}".repeat(256),
            group_action: "JOIN",
          },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a PRE-DISCLOSURE payload that omits the fields entirely", async () => {
    // The rejections above pin `null`; this one pins ABSENT, which is the
    // shape a backend WITHOUT this PR emits — the fields do not exist there.
    // It is the evidence behind the handoff's rollback ordering rule: revert
    // the backend under a deployed stepper and every preview dies here, so the
    // frontend must come down FIRST.
    //
    // Measured, not assumed: FOUR independent gates reject this payload — the
    // two PLAN_ROW_FIELDS entries, hasConsistentGroupEffect, and both source
    // predicates — so loosening any ONE of them leaves it rejected. It fails
    // only against a client that requires none of the disclosure, which is
    // precisely the pre-PR client this pins the incompatibility with.
    const preDisclosure = {
      ...DRY_RUN_RESULT,
      rows: DRY_RUN_RESULT.rows.map((row) => {
        // Deleted, not set to undefined: `{a: undefined}` still answers
        // Object.hasOwn, and the shape under test is a key that never existed.
        const stripped: Record<string, unknown> = { ...row };
        delete stripped.group_action;
        delete stripped.revenue_source_status;
        return stripped;
      }),
    };
    fetchMock().mockResolvedValue(jsonResponse(preDisclosure));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a 2xx describing a DIFFERENT content owner", async () => {
    // The fingerprint cannot police this alone: the digest is computed
    // server-side over the request's actual owner, and the client cannot
    // recompute it (it also takes the server-resolved tenant), so a body that
    // keeps a valid fingerprint while changing the owner is self-consistent
    // from here. Preview would render the altered target while Apply still
    // sends the captured owner (review #184, codex P2).
    fetchMock().mockResolvedValue(
      jsonResponse({ ...DRY_RUN_RESULT, content_owner_id: "COsomeone-else" }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a 2xx describing a different CMS status", async () => {
    // The other half of the target, and the reason cms_status is now sent
    // explicitly rather than left to the route default: an echo can only be
    // checked against a value the request named.
    fetchMock().mockResolvedValue(
      jsonResponse({ ...DRY_RUN_RESULT, cms_status: "OUTSIDE_CMS" }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("accepts the echo of a PADDED owner, which the route strips", async () => {
    // _validated_import_form strips the owner once at the boundary and plans
    // against the normalized value, so " COabc " legitimately comes back as
    // "COabc". Comparing raw would reject every response to a padded request.
    fetchMock().mockResolvedValue(jsonResponse(DRY_RUN_RESULT));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "  COabc  ",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ content_owner_id: "COabc" });
  });

  it("rejects a writable row missing the values it would write", async () => {
    // Each of these three fields is independently nullable because an ERROR
    // row carries none of them, so an outcome-blind check passes a CREATE with
    // all three null: Preview renders dashes where the channel, its name and
    // its revenue flag belong, Apply stays enabled, and the backend goes on to
    // write the real CSV values the operator never saw (review #184, codex P2).
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
        rows: [
          {
            ...DRY_RUN_RESULT.rows[0],
            youtube_channel_id: null,
            channel_name: null,
            revenue_required: null,
          },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("still accepts an ERROR row that carries none of them", async () => {
    // The exemption is the whole reason those fields are nullable: a row that
    // failed to parse has no channel to name. Requiring them everywhere would
    // reject the payload the backend actually emits for a bad roster.
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 0, ERROR: 1 },
        rows: [
          {
            row_number: 1,
            youtube_channel_id: null,
            outcome: "ERROR",
            channel_name: null,
            group_id: null,
            group_action: null,
            revenue_required: null,
            revenue_source_status: null,
            changes: {},
            reason: "channel_name is empty",
          },
        ],
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { ERROR: 1 } });
  });

  const rejectsPlan = async (plan: unknown) => {
    fetchMock().mockResolvedValue(jsonResponse(plan));
    const { result } = renderHook(() => useChannelImport(), { wrapper });
    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  };

  const onlyRow = (row: Record<string, unknown>) => ({
    ...DRY_RUN_RESULT,
    counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 1, ERROR: 0 },
    rows: [{ ...DRY_RUN_RESULT.rows[1], outcome: "UNCHANGED", changes: {}, ...row }],
  });

  /** A single UPDATE row with counts that AGREE with it, so countsMatchRows
   * cannot be what rejects the payload — the outcome/diff rule has to be. */
  const onlyUpdateRow = (row: Record<string, unknown>) => ({
    ...DRY_RUN_RESULT,
    counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
    rows: [{ ...DRY_RUN_RESULT.rows[1], ...row }],
  });

  it("rejects a CREATE that discloses no source status", async () => {
    // The planner stamps (None, _created_revenue_source_status(...)) on EVERY
    // create, so null is unemittable there — and it is exactly the case where
    // silence hides the finance classification a new channel is born with
    // (review #184, codex P2).
    await rejectsPlan({
      ...DRY_RUN_RESULT,
      counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      rows: [{ ...DRY_RUN_RESULT.rows[0], revenue_source_status: null }],
    });
  });

  it("rejects a source status moving somewhere the planner cannot derive", async () => {
    // `to` is always the DERIVED value, and derive_revenue_source_status
    // returns only these two when the flag flips (otherwise the pair is null
    // entirely). OFFICIAL_CMS_REVENUE can be a `from`, never a `to`.
    await rejectsPlan(
      onlyRow({
        revenue_source_status: { from: OPTIONAL_STATUS, to: OFFICIAL_STATUS },
      }),
    );
  });

  it("still accepts an UNCHANGED row that discloses nothing", async () => {
    // The complement: _planned_revenue_source_status returns None whenever the
    // write leaves the classification alone, which is the common case. The
    // CREATE rule must not be over-applied to it.
    fetchMock().mockResolvedValue(jsonResponse(onlyRow({ revenue_source_status: null })));
    const { result } = renderHook(() => useChannelImport(), { wrapper });
    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { UNCHANGED: 1 } });
  });

  it("rejects an UNCHANGED row that carries a diff", async () => {
    // outcome = UPDATE if changes else UNCHANGED, exactly — so a row labelled
    // UNCHANGED while carrying a real diff is unemittable, and it would show
    // the operator "no change" over fields the apply will actually write
    // (review #184, codex P2).
    await rejectsPlan(
      onlyRow({
        changes: { channel_name: { from: "Old Beta", to: "Beta Channel" } },
      }),
    );
  });

  it("rejects an UPDATE carrying no diff at all", async () => {
    await rejectsPlan(onlyUpdateRow({ changes: {} }));
  });

  it("rejects a diff naming a field the import never compares", async () => {
    // _inventory_changes compares exactly four fields, so a diff mentioning
    // anything else describes a write this route cannot perform.
    await rejectsPlan(
      onlyUpdateRow({ changes: { active: { from: true, to: false } } }),
    );
  });

  it("rejects a successful plan carrying no rows", async () => {
    // parse_channel_import_csv rejects a header-only or blank-only roster
    // outright ("CSV contains no data rows"), which is a format error with a
    // string detail — never a plan. Four zero counts satisfy countsMatchRows,
    // so without this the preview reads "empty roster" while Apply stays live.
    await rejectsPlan({
      ...DRY_RUN_RESULT,
      counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      rows: [],
    });
  });

  it("rejects duplicate row numbers", async () => {
    // They are the preview's React keys and the record the operator must go
    // and fix; the parser emits one per input row via enumerate(start=1).
    await rejectsPlan({
      ...DRY_RUN_RESULT,
      rows: [DRY_RUN_RESULT.rows[0], { ...DRY_RUN_RESULT.rows[1], row_number: 1 }],
    });
  });

  it("rejects a row number that is not a positive integer", async () => {
    await rejectsPlan(onlyRow({ row_number: 0 }));
    await rejectsPlan(onlyRow({ row_number: 1.5 }));
  });

  it("rejects an ERROR row that explains nothing", async () => {
    // `reason` is the only thing telling the operator WHY Apply is blocked and
    // which CSV record to fix. Every backend error entry carries one, so null
    // or blank is unemittable — and it renders as a dash in the Note column
    // (review #184, codex P2).
    const errorRow = {
      row_number: 1,
      youtube_channel_id: null,
      outcome: "ERROR",
      channel_name: null,
      group_id: null,
      group_action: null,
      revenue_required: null,
      revenue_source_status: null,
      changes: {},
      reason: null,
    };
    const counts = { CREATE: 0, UPDATE: 0, UNCHANGED: 0, ERROR: 1 };
    await rejectsPlan({ ...DRY_RUN_RESULT, counts, rows: [errorRow] });
    await rejectsPlan({ ...DRY_RUN_RESULT, counts, rows: [{ ...errorRow, reason: "" }] });
  });

  it("rejects a WRITABLE row carrying a reason", async () => {
    // The inverse impossible shape: no writable entry passes `reason` at all,
    // so it defaults to None there. A reason beside a CREATE would present a
    // diagnosis for a row that is not failing.
    await rejectsPlan(onlyRow({ reason: "looks suspicious" }));
  });

  it("rejects a source status contradicting the row's revenue flag", async () => {
    // Both derivations agree: MISSING_REVENUE_SOURCE when revenue is required,
    // PERFORMANCE_ONLY when it is not. Membership alone let the opposite pair
    // through, and RevenueCell renders the two together — the operator would
    // approve a finance classification that is the inverse of what the backend
    // persists (review #184, codex P2).
    await rejectsPlan(
      onlyRow({
        revenue_required: true,
        revenue_source_status: { from: OPTIONAL_STATUS, to: OPTIONAL_STATUS },
      }),
    );
    await rejectsPlan(
      onlyRow({
        revenue_required: false,
        revenue_source_status: { from: OPTIONAL_STATUS, to: REQUIRED_STATUS },
      }),
    );
  });

  it("accepts the source status that MATCHES the revenue flag", async () => {
    // The complement, so the rule cannot be over-applied to a legitimate flip.
    // An UPDATE carrying the flag diff that PRODUCES the transition — the only
    // shape the planner emits one in.
    fetchMock().mockResolvedValue(
      jsonResponse(
        onlyUpdateRow({
          revenue_required: true,
          revenue_source_status: { from: OPTIONAL_STATUS, to: REQUIRED_STATUS },
          changes: { revenue_required: { from: false, to: true } },
        }),
      ),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });
    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { UPDATE: 1 } });
  });

  it("rejects an APPLY answered 2xx over a plan holding ERROR rows", async () => {
    // import_channels raises 422 before applying whenever plan.has_errors, so
    // a 2xx carrying them is unemittable. Accepting it settled the durable
    // pending-write record and showed "Import applied" for a write the backend
    // refuses to perform (review #184, codex P2).
    const errored = {
      ...DRY_RUN_RESULT,
      dry_run: false,
      counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 0, ERROR: 1 },
      rows: [
        {
          row_number: 1,
          youtube_channel_id: null,
          outcome: "ERROR",
          channel_name: null,
          group_id: null,
          group_action: null,
          revenue_required: null,
          revenue_source_status: null,
          changes: {},
          reason: "channel_name is empty",
        },
      ],
    };
    fetchMock().mockResolvedValue(jsonResponse(errored));
    const { result } = renderHook(() => useChannelImport(), { wrapper });
    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });

    // But a DRY RUN over the same plan is exactly what the preview exists for.
    fetchMock().mockResolvedValue(jsonResponse({ ...errored, dry_run: true }));
    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { ERROR: 1 } });
  });

  it("rejects a diff whose sides are EQUAL", async () => {
    // _inventory_changes keeps only pairs where pair[0] != pair[1], so an
    // unchanged pair is unemittable — and it presents "no change" for a field
    // the apply may well write (review #184, codex P2).
    await rejectsPlan(
      onlyUpdateRow({ changes: { channel_name: { from: "Beta Channel", to: "Beta Channel" } } }),
    );
  });

  it("rejects a diff whose `to` contradicts the row", async () => {
    // The reported case: a diff saying revenue_required stays false, beside a
    // row saying it is true. Apply writes the row's value; the operator read
    // the diff.
    await rejectsPlan(
      onlyUpdateRow({
        revenue_required: true,
        revenue_source_status: { from: OPTIONAL_STATUS, to: REQUIRED_STATUS },
        changes: { revenue_required: { from: false, to: false } },
      }),
    );
    await rejectsPlan(
      onlyUpdateRow({
        channel_name: "Beta Channel",
        changes: { channel_name: { from: "Old Beta", to: "Someone Else" } },
      }),
    );
  });

  it("rejects a diff whose `to` contradicts the request TARGET", async () => {
    // cms_status and content_owner_id take their `to` from the request, not
    // the row, so they are checked against the plan's echoed target.
    await rejectsPlan(
      onlyUpdateRow({ changes: { cms_status: { from: "OUTSIDE_CMS", to: "OUTSIDE_CMS_2" } } }),
    );
    await rejectsPlan(
      onlyUpdateRow({ changes: { content_owner_id: { from: null, to: "COsomeone-else" } } }),
    );
  });

  it("accepts diffs that agree with the row and the target", async () => {
    // The complement, one entry per field, so the rule cannot be over-applied
    // to the payload the backend actually emits.
    fetchMock().mockResolvedValue(
      jsonResponse(
        onlyUpdateRow({
          channel_name: "Beta Channel",
          revenue_required: true,
          revenue_source_status: { from: OPTIONAL_STATUS, to: REQUIRED_STATUS },
          changes: {
            channel_name: { from: "Old Beta", to: "Beta Channel" },
            revenue_required: { from: false, to: true },
            cms_status: { from: "OUTSIDE_CMS", to: "INSIDE_CMS" },
            content_owner_id: { from: null, to: "COabc" },
          },
        }),
      ),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });
    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { UPDATE: 1 } });
  });

  it("rejects a source transition FROM a status the column cannot hold", async () => {
    // `from` is the row's CURRENT status, which the
    // ck_youtube_channels_revenue_source_status CHECK constrains to five
    // literals. Accepting any string let a row present
    // "GARBAGE -> MISSING_REVENUE_SOURCE" while Apply stayed enabled and the
    // retained fingerprint authorised the real mutation (review #184, codex).
    await rejectsPlan(
      onlyRow({
        revenue_required: true,
        revenue_source_status: { from: "GARBAGE", to: REQUIRED_STATUS },
      }),
    );
  });

  it("rejects a source transition that goes nowhere", async () => {
    // _planned_revenue_source_status returns None when planned == current, so
    // a pair with equal sides is unemittable — and it would announce a
    // reclassification that is not happening.
    await rejectsPlan(
      onlyRow({
        revenue_required: true,
        revenue_source_status: {
          from: REQUIRED_STATUS,
          to: REQUIRED_STATUS,
        },
      }),
    );
  });

  it("rejects a non-CREATE transition with no prior status", async () => {
    // Only a CREATE has no `from`: it is born with its classification. An
    // UPDATE or UNCHANGED row always has one.
    await rejectsPlan(
      onlyRow({
        revenue_required: true,
        revenue_source_status: { from: null, to: REQUIRED_STATUS },
      }),
    );
  });

  it("accepts a transition from any of the five declared statuses", async () => {
    // Including the two a plan never derives INTO — an OFFICIAL_* status is a
    // legitimate `from`, which is why only `to` is narrowed.
    fetchMock().mockResolvedValue(
      jsonResponse(
        onlyUpdateRow({
          revenue_required: false,
          revenue_source_status: { from: OFFICIAL_STATUS, to: OPTIONAL_STATUS },
          changes: { revenue_required: { from: true, to: false } },
        }),
      ),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });
    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ counts: { UPDATE: 1 } });
  });

  it("rejects a source transition with no revenue-flag diff behind it", async () => {
    // _planned_revenue_source_status only emits a transition when the flag
    // FLIPS: derive returns the current status untouched otherwise, which
    // becomes None. So a transition beside a diff that changes only the name
    // is unemittable — and it asks the operator to approve a finance-source
    // reclassification the backend will not perform (review #184, codex P2).
    await rejectsPlan(
      onlyUpdateRow({
        revenue_required: true,
        revenue_source_status: { from: OPTIONAL_STATUS, to: REQUIRED_STATUS },
        changes: { channel_name: { from: "Old Beta", to: "Beta Channel" } },
      }),
    );
  });

  it("rejects a writable row whose channel id is not a channel id", async () => {
    // CHANNEL_ID_PATTERN is ^UC[A-Za-z0-9_-]{22}$ and a row failing it becomes
    // an ERROR, so any other shape on a writable row is unemittable — and it
    // would put a misidentified channel on the preview while the bound apply
    // wrote the real one (review #184, codex P2).
    await rejectsPlan(onlyRow({ youtube_channel_id: "UC-too-short" }));
    await rejectsPlan(onlyRow({ youtube_channel_id: " " }));
  });

  it("rejects a writable row whose name is only whitespace", async () => {
    // The parser strips the name and rejects an empty result, so a blank name
    // never reaches a writable row — and it renders as an empty cell, exactly
    // as unreviewable as an absent one.
    await rejectsPlan(onlyRow({ channel_name: "   " }));
  });

  it("rejects an ERROR row claiming a group effect", async () => {
    // An ERROR row performs no writes, so the planner leaves both group fields
    // null — it never computes group_action past the block check. Accepting a
    // non-null pair told the operator a rejected row would create or join a
    // group, on the same screen that says error rows write nothing.
    await rejectsPlan({
      ...DRY_RUN_RESULT,
      counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 0, ERROR: 1 },
      rows: [
        {
          row_number: 1,
          youtube_channel_id: null,
          outcome: "ERROR",
          channel_name: null,
          group_id: "g1",
          group_action: "CREATE",
          revenue_required: null,
          revenue_source_status: null,
          changes: {},
          reason: "channel_name is empty",
        },
      ],
    });
  });

  it("rejects a 2xx describing a plan other than the one bound", async () => {
    // A stale, misrouted or legacy-server response can be structurally perfect
    // and describe a DIFFERENT plan. The route returns the digest it compared
    // against on success, so an inequality is never legitimate — and accepting
    // it lets the flow clear the unsettled record and present an unrelated
    // payload as the approved one (review #184, codex P2).
    fetchMock().mockResolvedValue(
      jsonResponse({ ...APPLY_RESULT, plan_fingerprint: "someone-elses-plan" }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
        expectedPlanFingerprint: "plan-abc",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("accepts a 2xx echoing the bound fingerprint", async () => {
    // The complement: the check must not reject the legitimate case, or every
    // apply would land in the indeterminate path.
    fetchMock().mockResolvedValue(jsonResponse(APPLY_RESULT));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
        expectedPlanFingerprint: APPLY_RESULT.plan_fingerprint,
      }),
    ).resolves.toMatchObject({ plan_fingerprint: APPLY_RESULT.plan_fingerprint });
  });

  it("does not police the fingerprint on an UNBOUND request", async () => {
    // An API client that never previewed sends no expectation, so there is
    // nothing to compare and nothing to refuse.
    fetchMock().mockResolvedValue(
      jsonResponse({ ...APPLY_RESULT, plan_fingerprint: "whatever-the-server-says" }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({ plan_fingerprint: "whatever-the-server-says" });
  });

  it("rejects an apply answered with a PREVIEW payload", async () => {
    // A structural check only proves `dry_run` is a boolean. A malformed or
    // legacy apply response carrying `dry_run: true` passed it, and the flow
    // then advanced to Applied and told the operator the import committed —
    // on a body that identifies itself as a preview (review #184).
    fetchMock().mockResolvedValue(jsonResponse(DRY_RUN_RESULT));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: false,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects a preview answered with an APPLY payload", async () => {
    // The mirror image, and the more alarming direction: a dry run that comes
    // back marked as a committed write means something wrote when the
    // operator asked for a read.
    fetchMock().mockResolvedValue(jsonResponse(APPLY_RESULT));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("rejects counts that disagree with the rows they claim to tally", async () => {
    // All four keys, all valid integers — and a lie. The backend derives each
    // count BY tallying the rows, so a payload where they disagree is one it
    // cannot emit, and it would put contradictory totals on the preview and
    // carry them onto the Applied screen as the approved plan (review #184).
    fetchMock().mockResolvedValue(
      jsonResponse({
        ...DRY_RUN_RESULT,
        counts: { CREATE: 99, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });

  it("accepts a zero count, which is a real tally", async () => {
    // The complement: zero is legitimate (the preview hides zero rows rather
    // than rejecting the plan), so the check must not confuse "empty" with
    // "malformed".
    fetchMock().mockResolvedValue(
      // The tally must MATCH the rows now, so this varies the rows too: zero is
      // legitimate for an outcome nothing planned, which is the point of the
      // test — the check must not confuse "empty" with "malformed".
      jsonResponse({
        ...DRY_RUN_RESULT,
        rows: [
          { ...DRY_RUN_RESULT.rows[1], outcome: "UNCHANGED", changes: {} },
          { ...DRY_RUN_RESULT.rows[1], row_number: 3, outcome: "UNCHANGED", changes: {} },
        ],
        counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 2, ERROR: 0 },
      }),
    );
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).resolves.toMatchObject({
      counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 2, ERROR: 0 },
    });
  });

  it("rejects a 2xx missing dry_run, which selects the flow's next step", async () => {
    const noDryRun = { ...DRY_RUN_RESULT } as Record<string, unknown>;
    delete noDryRun.dry_run;
    fetchMock().mockResolvedValue(jsonResponse(noDryRun));
    const { result } = renderHook(() => useChannelImport(), { wrapper });

    await expect(
      result.current({
        file: rosterFile(),
        contentOwnerId: "COabc",
        dryRun: true,
        reason: "monthly roster import",
      }),
    ).rejects.toMatchObject({ name: "ChannelImportShapeError" });
  });
});
