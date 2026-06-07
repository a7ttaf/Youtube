import { describe, expect, it } from "vitest";

import { buildConnectorRunsUrl } from "@/lib/api/useConnectorRuns";

describe("buildConnectorRunsUrl", () => {
  it("returns the bare path with no params", () => {
    expect(buildConnectorRunsUrl()).toBe("/connectors/runs");
  });

  it("appends connector_key, account_id, and limit when provided", () => {
    const url = buildConnectorRunsUrl("youtube_reporting", "acct-1", undefined, undefined, 25);
    expect(url).toContain("connector_key=youtube_reporting");
    expect(url).toContain("account_id=acct-1");
    expect(url).toContain("limit=25");
    expect(url).not.toContain("cursor_started_at");
    expect(url).not.toContain("cursor_id");
  });

  it("appends the cursor pair only when BOTH halves are present (both-or-neither)", () => {
    const both = buildConnectorRunsUrl(undefined, undefined, "2026-03-21T02:00:00Z", "run-1");
    expect(both).toContain("cursor_started_at=2026-03-21T02%3A00%3A00Z");
    expect(both).toContain("cursor_id=run-1");
  });

  it("drops a half cursor (started_at only)", () => {
    const url = buildConnectorRunsUrl(undefined, undefined, "2026-03-21T02:00:00Z");
    expect(url).not.toContain("cursor_started_at");
    expect(url).not.toContain("cursor_id");
    expect(url).toBe("/connectors/runs");
  });

  it("drops a half cursor (id only)", () => {
    const url = buildConnectorRunsUrl(undefined, undefined, undefined, "run-1");
    expect(url).not.toContain("cursor_started_at");
    expect(url).not.toContain("cursor_id");
    expect(url).toBe("/connectors/runs");
  });

  it("combines filters and a full cursor", () => {
    const url = buildConnectorRunsUrl(
      "adsense",
      "pub-9",
      "2026-02-01T00:00:00Z",
      "run-9",
      50,
    );
    expect(url).toContain("connector_key=adsense");
    expect(url).toContain("account_id=pub-9");
    expect(url).toContain("limit=50");
    expect(url).toContain("cursor_started_at=");
    expect(url).toContain("cursor_id=run-9");
  });
});
