import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type { ChannelIssuesResponse } from "@/lib/api/types";
import { useChannelIssues } from "@/lib/api/useChannelIssues";
import { TenantProvider } from "@/contexts/TenantContext";

function wrapper({ children }: { children: React.ReactNode }) { // skipcq: JS-0067
  return <TenantProvider initialSlug="ums">{children}</TenantProvider>;
}

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const ISSUES: ChannelIssuesResponse = {
  items: [
    {
      youtube_channel_id: "UC-ISSUE-01",
      channel_name: "No Company",
      primary_company_id: null,
      issue_type: "MISSING_COMPANY",
      severity: "high",
      message: "Channel has no primary company mapping.",
      recommended_action: "Assign the channel to the correct company.",
    },
    {
      youtube_channel_id: "UC-ISSUE-02",
      channel_name: "Outside CMS",
      primary_company_id: "united-studios",
      issue_type: "OUTSIDE_CMS_REVENUE_REQUIRED",
      severity: "medium",
      message: "Revenue-required channel is outside CMS.",
      recommended_action: "Link the channel to CMS or maintain official manual import.",
    },
  ],
  summary: {
    total_issue_count: 2,
    channel_count: 2,
    issue_type_counts: {
      MISSING_COMPANY: 1,
      OUTSIDE_CMS_REVENUE_REQUIRED: 1,
    },
  },
};

function jsonResponse(body: unknown, status = 200) { // skipcq: JS-0067
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchMock() { // skipcq: JS-0067
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

function urlOf(input: unknown): string { // skipcq: JS-0067
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

function requireFetchArgs() { // skipcq: JS-0067
  const args = fetchMock().mock.calls.at(-1);
  if (!args) throw new Error("expected fetch to have been called");
  return args;
}

describe("useChannelIssues", () => {
  it("auto-fetches GET /channels/issues on mount and returns the {items, summary} shape", async () => {
    fetchMock().mockResolvedValue(jsonResponse(ISSUES));
    const { result } = renderHook(() => useChannelIssues(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(urlOf(requireFetchArgs()[0])).toBe("/channels/issues");
    expect(result.current.data?.items).toHaveLength(2);
    expect(result.current.data?.items[0]?.issue_type).toBe("MISSING_COMPANY");
    expect(result.current.data?.summary.total_issue_count).toBe(2);
    expect(result.current.data?.summary.issue_type_counts.MISSING_COMPANY).toBe(1);
    expect(result.current.error).toBeNull();
    expect(typeof result.current.reload).toBe("function");
  });

  it("captures a typed ApiError (403) and clears data (never masks authz)", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: analytics.view" }, 403),
    );
    const { result } = renderHook(() => useChannelIssues(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(result.current.data).toBeNull();
    expect(result.current.error).toMatchObject({ name: "ApiError", status: 403 });
  });

  it("reload() re-runs the fetch", async () => {
    fetchMock().mockResolvedValue(jsonResponse(ISSUES));
    const { result } = renderHook(() => useChannelIssues(), { wrapper });

    await waitFor(() => expect(result.current.loading).toBe(false));
    expect(fetchMock()).toHaveBeenCalledTimes(1);

    act(() => result.current.reload());
    await waitFor(() => expect(fetchMock()).toHaveBeenCalledTimes(2));
  });
});
