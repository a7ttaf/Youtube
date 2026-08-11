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

const CSV_TEXT = "youtube_channel_id,channel_name\nUCa,Alpha Channel\n";

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
      youtube_channel_id: "UCa",
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
      youtube_channel_id: "UCb",
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
