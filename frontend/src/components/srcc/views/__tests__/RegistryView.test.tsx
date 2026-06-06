import { render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RegistryView from "@/components/srcc/views/RegistryView";
import type { ChannelRegistryEntry } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const CHANNELS: ChannelRegistryEntry[] = [
  {
    youtube_channel_id: "UC-DRAMA-01",
    channel_name: "UMS Drama",
    primary_company_id: "united-studios",
    cms_status: "INSIDE_CMS",
    content_owner_id: "ams/content-owner-1",
    revenue_required: true,
    // OFFICIAL_CMS_REVENUE is the DB-constrained enum value (not YOUTUBE_REPORTING_API).
    revenue_source_status: "OFFICIAL_CMS_REVENUE",
    active: true,
  },
  {
    youtube_channel_id: "UC-SPORT-7",
    channel_name: "Sports Extra",
    primary_company_id: "tv-sector",
    cms_status: "OUTSIDE_CMS",
    content_owner_id: null,
    revenue_required: true,
    revenue_source_status: "OFFICIAL_MANUAL_IMPORT",
    active: true,
  },
  {
    youtube_channel_id: "UC-MUSIC-31",
    channel_name: "Music Stage",
    primary_company_id: null,
    // UNKNOWN is the DB-constrained enum value (not UNMAPPED).
    cms_status: "UNKNOWN",
    content_owner_id: null,
    revenue_required: true,
    revenue_source_status: "MISSING_REVENUE_SOURCE",
    active: true,
  },
];

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

function routeChannels(body: ChannelRegistryEntry[] | null = CHANNELS, status = 200) { // skipcq: JS-0067
  return (input: unknown) => {
    const url = urlOf(input);
    if (url === "/channels") {
      return Promise.resolve(jsonResponse(body, status));
    }
    return Promise.resolve(jsonResponse({}, 200));
  };
}

function renderRegistry(canManageRegistry = true, canViewFinance = true) { // skipcq: JS-0067
  return render(
    <TenantProvider initialSlug="ums">
      <RegistryView canManageRegistry={canManageRegistry} canViewFinance={canViewFinance} />
    </TenantProvider>,
  );
}

function channelCalls() { // skipcq: JS-0067
  return fetchMock().mock.calls.filter(
    (args: unknown[]) => urlOf(args[0]) === "/channels",
  );
}

describe("RegistryView wired to GET /channels", () => {
  it("renders live channel names and IDs from the API", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByText("UC-DRAMA-01")).toBeInTheDocument();
    // "Sports Extra" also appears in the mapping-form dropdown — use getAllByText.
    expect(screen.getAllByText("Sports Extra").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("UC-SPORT-7")).toBeInTheDocument();
    // "Music Stage" also appears in the mapping-form dropdown — use getAllByText.
    expect(screen.getAllByText("Music Stage").length).toBeGreaterThanOrEqual(1);
  });

  it("derives CMS badges from cms_status: INSIDE_CMS→green, OUTSIDE_CMS→amber, UNKNOWN→red (fallback)", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    // "Inside CMS" only appears as a badge (no summary tile with this exact text).
    expect(screen.getByText("Inside CMS")).toBeInTheDocument();
    // "Outside CMS" appears as both the summary tile label AND the CMS badge —
    // use getAllByText and assert at least one match.
    expect(screen.getAllByText("Outside CMS").length).toBeGreaterThanOrEqual(1);
    // UNKNOWN status falls through to the "Unmapped" fallback in cmsBadge.
    expect(screen.getByText("Unmapped")).toBeInTheDocument();
  });

  it("derives source labels from revenue_source_status", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    // OFFICIAL_CMS_REVENUE → "Official CMS revenue"
    expect(screen.getByText("Official CMS revenue")).toBeInTheDocument();
    // OFFICIAL_MANUAL_IMPORT → "Uploaded owner statement"
    expect(screen.getByText("Uploaded owner statement")).toBeInTheDocument();
    // MISSING_REVENUE_SOURCE → "Not linked"
    expect(screen.getByText("Not linked")).toBeInTheDocument();
  });

  it("derives state badges: MISSING_REVENUE_SOURCE+revenue_required→Export block, OUTSIDE_CMS+no content_owner→Evidence due, else→Approved", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    // INSIDE_CMS + has source → "Approved"
    expect(screen.getByText("Approved")).toBeInTheDocument();
    // OUTSIDE_CMS + no content_owner_id → "Evidence due"
    expect(screen.getByText("Evidence due")).toBeInTheDocument();
    // MISSING_REVENUE_SOURCE + revenue_required → "Export block"
    expect(screen.getByText("Export block")).toBeInTheDocument();
  });

  it("shows trace keys for registry managers and hides them for others", async () => {
    fetchMock().mockImplementation(routeChannels());
    const { rerender } = renderRegistry(true);

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    // Trace keys visible: channel:{youtube_channel_id}
    expect(screen.getByText("channel:UC-DRAMA-01")).toBeInTheDocument();
    // Unmapped channel shows "pending"
    expect(screen.getByText("pending")).toBeInTheDocument();

    // Re-render with canManageRegistry=false: trace keys withheld
    rerender(
      <TenantProvider initialSlug="ums">
        <RegistryView canManageRegistry={false} canViewFinance />
      </TenantProvider>,
    );
    expect(screen.queryByText("channel:UC-DRAMA-01")).not.toBeInTheDocument();
  });

  it("shows a loading state while fetching and does not render mock tile values", () => {
    fetchMock().mockImplementation(
      () => new Promise<Response>(() => { /* never resolves */ }),
    );
    renderRegistry();

    expect(screen.getByText(/Loading channels/i)).toBeInTheDocument();
    // Summary tiles must not show stale mock values while the fetch is in flight.
    expect(screen.queryByText("318")).not.toBeInTheDocument();
    expect(screen.queryByText("$42.8K")).not.toBeInTheDocument();
    // Mock note subtitles must also be cleared so they don't contradict live values.
    expect(screen.queryByText(/300\+ target registry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Allocation requires explicit source mapping/)).not.toBeInTheDocument();
  });

  it("shows an empty state when the API returns no channels", async () => {
    fetchMock().mockImplementation(routeChannels([]));
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText(/No channels in registry/i)).toBeInTheDocument(),
    );
  });

  it("shows a 403 error message and does not render mock tile values on error", async () => {
    fetchMock().mockImplementation(
      routeChannels(null, 403),
    );
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("No permission")).toBeInTheDocument(),
    );
    expect(screen.getByText(/cannot view the channel registry/i)).toBeInTheDocument();
    // Summary tiles must not show stale mock values when the fetch fails.
    expect(screen.queryByText("318")).not.toBeInTheDocument();
    expect(screen.queryByText("$42.8K")).not.toBeInTheDocument();
    // Mock note subtitles must also be cleared on error.
    expect(screen.queryByText(/300\+ target registry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Allocation requires explicit source mapping/)).not.toBeInTheDocument();
  });

  it("derives active channel count and outside-CMS count for summary tiles", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    // Active channels: total returned (3)
    expect(screen.getByText("3")).toBeInTheDocument();
    // Outside CMS: only channels with cms_status === OUTSIDE_CMS (Sports Extra = 1).
    // Music Stage has cms_status UNKNOWN, which is NOT counted as outside-CMS.
    expect(screen.getByText("1")).toBeInTheDocument();
    // Finance tiles (Unmapped revenue, Scoped changes) have no live source —
    // they should show "—", not the stale mock values.
    expect(screen.queryByText("$42.8K")).not.toBeInTheDocument();
    // Mock note subtitles must be cleared even when channels load successfully.
    expect(screen.queryByText(/300\+ target registry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Allocation requires explicit source mapping/)).not.toBeInTheDocument();
    expect(screen.queryByText("318")).not.toBeInTheDocument();
  });

  it("fires exactly one /channels fetch per mount (no duplicate on double-invoke)", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(channelCalls()).toHaveLength(1);
  });

  it("disables write-path buttons (Bulk Import, Request Mapping Change, Save Draft, Submit Approval) for all users in Phase 1", async () => {
    fetchMock().mockImplementation(routeChannels());
    // Test with canManageRegistry=true — Phase 1 write buttons must be disabled
    // for ALL users since the backend routes are not yet wired.
    renderRegistry(true);

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /bulk import/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /request mapping change/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /save draft/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /submit approval/i })).toBeDisabled();
  });
});
