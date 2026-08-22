import { describe, expect, it } from "vitest";

import { computeDisplayDigest, displayDigestMatchesDisclosed } from "@/lib/displayDigest";
import type { ChannelImportResult } from "@/lib/api/types";

describe("displayDigest", () => {
  it("matches the backend canonical recipe including non-ASCII channel names", () => {
    const plan: Pick<ChannelImportResult, "content_owner_id" | "cms_status" | "counts" | "rows"> = {
      content_owner_id: "COabc",
      cms_status: "INSIDE_CMS",
      counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 0 },
      rows: [
        {
          row_number: 1,
          youtube_channel_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
          outcome: "CREATE",
          channel_name: "Alpha Nëws ünd Sport",
          group_id: null,
          group_action: null,
          revenue_required: true,
          revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
          changes: {},
          reason: null,
        },
      ],
    };
    const digest = computeDisplayDigest(plan);
    expect(displayDigestMatchesDisclosed({ ...plan, display_digest: digest, dry_run: true, plan_fingerprint: "fp" })).toBe(true);
    expect(displayDigestMatchesDisclosed({ ...plan, display_digest: "deadbeef", dry_run: true, plan_fingerprint: "fp" })).toBe(false);
  });
});
