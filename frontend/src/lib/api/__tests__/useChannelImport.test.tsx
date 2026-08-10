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

const DRY_RUN_RESULT: ChannelImportResult = {
  dry_run: true,
  content_owner_id: "COabc",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, UPDATE: 1 },
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
      revenue_source_status: null,
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
  counts: { ERROR: 1 },
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
    // Exactly the four wire fields — cms_status is omitted so the backend
    // default (INSIDE_CMS) applies.
    expect([...form.keys()].sort()).toEqual([
      "content_owner_id",
      "dry_run",
      "file",
      "reason",
    ]);
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
