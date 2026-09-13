import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RegistryView, {
  warnedIdsForAcknowledgement,
} from "@/components/srcc/views/RegistryView";
import type { ChannelRegistryEntry, OrgUnit } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;


describe("acknowledgement id selection", () => {
  // The capture effect fills the ref AFTER the render that raises the notice.
  // Testing Library flushes effects before dispatching an event, so the
  // empty-capture branch is unreachable from a rendered click — a component
  // test of it passes with the branch removed, which is why the rule is pinned
  // here directly (review #184, qodo).

  it("retires the CAPTURED ids when the warning was captured", () => {
    // The exclusion that matters: an apply admitted after the warning went up
    // was never represented by it and must survive the acknowledgement.
    expect(warnedIdsForAcknowledgement(["apply-warned"], ["apply-warned", "apply-later"])).toEqual([
      "apply-warned",
    ]);
  });

  it("falls back to the LIVE set when nothing was captured", () => {
    // Otherwise the click retires nothing and the notice — plus the Apply
    // refusal behind it — cannot be cleared at all.
    expect(warnedIdsForAcknowledgement([], ["apply-live"])).toEqual(["apply-live"]);
  });

  it("retires nothing when neither has anything", () => {
    expect(warnedIdsForAcknowledgement([], [])).toEqual([]);
  });
});

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

// Org-unit ids deliberately reuse the channels' primary_company_id values so
// the name join resolves; sector resolves through the company's parent_id.
const ORG_UNITS: OrgUnit[] = [
  { id: "sector-tv", parent_id: null, type: "SECTOR", name: "TV", active: true },
  {
    id: "united-studios", parent_id: "sector-tv", type: "COMPANY",
    name: "United Studios", active: true,
  },
  {
    id: "tv-sector", parent_id: "sector-tv", type: "COMPANY",
    name: "Catalog Media", active: true,
  },
];

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

type RouteOverrides = {
  channels?: () => Promise<Response> | Response;
  orgUnits?: () => Promise<Response> | Response;
  mapping?: (init: RequestInit | undefined) => Promise<Response> | Response;
  propose?: (init: RequestInit | undefined) => Promise<Response> | Response;
};

/** Resolve GET /channels: the override when provided, else the seeded CHANNELS. */
const channelsResponse = (overrides: RouteOverrides) =>
  (overrides.channels ? overrides.channels() : jsonResponse(CHANNELS));

/** Resolve GET /org-units: the override when provided, else the seeded ORG_UNITS. */
const orgUnitsResponse = (overrides: RouteOverrides) =>
  (overrides.orgUnits ? overrides.orgUnits() : jsonResponse(ORG_UNITS));

/** Resolve the mapping PATCH: the override when provided, else an audited success. */
const mappingResponse = (overrides: RouteOverrides, init?: RequestInit) =>
  (overrides.mapping
    ? overrides.mapping(init)
    : jsonResponse({ audit_event: { event_type: "CHANNEL_UPDATED" } }));

/** Resolve the link-proposal POST: the override when provided, else a 201 proposal. */
const proposeResponse = (overrides: RouteOverrides, init?: RequestInit) =>
  (overrides.propose
    ? overrides.propose(init)
    : jsonResponse(
        { link: {}, audit_event: { event_type: "CHANNEL_ACCOUNT_LINK_PROPOSED" } },
        201,
      ));

/** True for a PATCH /channels/{id}/mapping request. */
const isMappingPatch = (url: string, init?: RequestInit) =>
  /^\/channels\/[^/]+\/mapping$/.test(url) && init?.method === "PATCH";

/** True for a POST /revenue/channel-account-links request. */
const isProposePost = (url: string, init?: RequestInit) =>
  url === "/revenue/channel-account-links" && init?.method === "POST";

/** Resolve the registry's GET reads (channels, org-units, owner picker), or null. */
const readRouteResponse = (
  url: string,
  overrides: RouteOverrides,
): Response | Promise<Response> | null => {
  if (url === "/channels") {
    return channelsResponse(overrides);
  }
  if (url === "/org-units") {
    return orgUnitsResponse(overrides);
  }
  // The import stepper's owner picker (PR-B): no credentials seeded, so the
  // picker renders its empty state with the Connectors pointer.
  if (url === "/connectors/content-owners?connector_key=youtube-analytics") {
    return jsonResponse({ items: [] });
  }
  return null;
};

/** Route the registry's endpoints; unrouted URLs fail loudly (404 + []). */
const routeRegistry = (overrides: RouteOverrides = {}) => {
  return (input: unknown, init?: RequestInit) => {
    const url = urlOf(input);
    const read = readRouteResponse(url, overrides);
    if (read !== null) {
      return Promise.resolve(read);
    }
    if (isMappingPatch(url, init)) {
      return Promise.resolve(mappingResponse(overrides, init));
    }
    if (isProposePost(url, init)) {
      return Promise.resolve(proposeResponse(overrides, init));
    }
    return Promise.resolve(jsonResponse([], 404));
  };
};

/** Backwards-compatible channels-only router used by the Phase 1 read tests. */
const routeChannels = (body: ChannelRegistryEntry[] | null = CHANNELS, status = 200) => {
  return routeRegistry({ channels: () => jsonResponse(body, status) });
};

const renderRegistry = (
  canManageRegistry = true,
  canViewFinance = true,
  onOpenTrace?: (channelId: string) => void,
  canImportChannels = false,
) => {
  return render(
    <TenantProvider initialSlug="ums">
      <RegistryView
        canManageRegistry={canManageRegistry}
        canImportChannels={canImportChannels}
        canViewFinance={canViewFinance}
        onOpenTrace={onOpenTrace}
      />
    </TenantProvider>,
  );
};

/** Render the registry with a routed fetch, returning the render result. */
const renderRegistryWith = (route: ReturnType<typeof routeRegistry>) => {
  fetchMock().mockImplementation(route);
  return renderRegistry();
};

/** The summary-tile elements the registry header rendered, in DOM order. */
const summaryTilesIn = (container: HTMLElement): Element[] =>
  Array.from(
    container.querySelectorAll('[aria-label="Registry summary"] article'),
  );

const callsTo = (predicate: (url: string, init?: RequestInit) => boolean) => {
  return fetchMock().mock.calls.filter((args: unknown[]) =>
    predicate(urlOf(args[0]), args[1] as RequestInit | undefined),
  );
};

const channelCalls = () => {
  return callsTo((url) => url === "/channels");
};

const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((r) => {
    resolve = r;
  });
  return { promise, resolve };
};

describe("RegistryView wired to GET /channels", () => {
  it("renders live channel names and IDs from the API", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByText("UC-DRAMA-01")).toBeInTheDocument();
    expect(screen.getAllByText("Sports Extra").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("UC-SPORT-7")).toBeInTheDocument();
    expect(screen.getAllByText("Music Stage").length).toBeGreaterThanOrEqual(1);
  });

  it("derives CMS badges from cms_status: INSIDE_CMS→green, OUTSIDE_CMS→amber, UNKNOWN→red (fallback)", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByText("Inside CMS")).toBeInTheDocument();
    expect(screen.getAllByText("Outside CMS").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("Unmapped")).toBeInTheDocument();
  });

  it("derives source labels from revenue_source_status", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByText("Official CMS revenue")).toBeInTheDocument();
    expect(screen.getByText("Uploaded owner statement")).toBeInTheDocument();
    expect(screen.getByText("Not linked")).toBeInTheDocument();
  });

  it("derives state badges: MISSING_REVENUE_SOURCE+revenue_required→Export block, OUTSIDE_CMS+no content_owner→Evidence due, else→Approved", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByText("Approved")).toBeInTheDocument();
    expect(screen.getByText("Evidence due")).toBeInTheDocument();
    expect(screen.getByText("Export block")).toBeInTheDocument();
  });

  it("shows trace keys for registry managers and hides them for others", async () => {
    fetchMock().mockImplementation(routeChannels());
    const { rerender } = renderRegistry(true);

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByText("channel:UC-DRAMA-01")).toBeInTheDocument();
    expect(screen.getByText("pending")).toBeInTheDocument();

    rerender(
      <TenantProvider initialSlug="ums">
        <RegistryView
          canManageRegistry={false}
          canImportChannels={false}
          canViewFinance
        />
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
    expect(screen.queryByText("318")).not.toBeInTheDocument();
    expect(screen.queryByText("$42.8K")).not.toBeInTheDocument();
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
    expect(screen.queryByText("318")).not.toBeInTheDocument();
    expect(screen.queryByText("$42.8K")).not.toBeInTheDocument();
    expect(screen.queryByText(/300\+ target registry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Allocation requires explicit source mapping/)).not.toBeInTheDocument();
  });

  it("derives active channel count and outside-CMS count for summary tiles", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.getByText("3")).toBeInTheDocument();
    expect(screen.getByText("1")).toBeInTheDocument();
    expect(screen.queryByText("$42.8K")).not.toBeInTheDocument();
    expect(screen.queryByText(/300\+ target registry/)).not.toBeInTheDocument();
    expect(screen.queryByText(/Allocation requires explicit source mapping/)).not.toBeInTheDocument();
    expect(screen.queryByText("318")).not.toBeInTheDocument();
  });

  it("renders neither the mock Registry Controls panel nor the sourceless tiles", async () => {
    fetchMock().mockImplementation(routeChannels());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    // The deleted REGISTRY_CONTROLS panel: title and its distinctive rows.
    expect(screen.queryByText("Registry Controls")).not.toBeInTheDocument();
    expect(screen.queryByText("Company scope enforced")).not.toBeInTheDocument();
    expect(screen.queryByText("Finance month impact")).not.toBeInTheDocument();
    // The two REGISTRY_SUMMARY tiles that had no live source are gone (they
    // stood permanently at "—" rather than showing anything real).
    expect(screen.queryByText("Unmapped revenue")).not.toBeInTheDocument();
    expect(screen.queryByText("Scoped changes")).not.toBeInTheDocument();
    // The two that survive are the ones derived from the fetched rows.
    expect(screen.getByText("Active channels")).toBeInTheDocument();
  });

  it("renders exactly the two derived tiles when the registry is empty", async () => {
    const { container } = renderRegistryWith(routeChannels([]));

    await waitFor(() =>
      expect(screen.getByText(/No channels in registry/i)).toBeInTheDocument(),
    );
    // Exactly two tiles: the sourceless pair is gone, so an empty registry can
    // no longer render a tile stuck at "—" beside the real zero counts.
    expect(summaryTilesIn(container)).toHaveLength(2);
    // Zero is a real derived count, not a placeholder or a mock number.
    expect(screen.getAllByText("0")).toHaveLength(2);
    expect(screen.queryByText("318")).not.toBeInTheDocument();
    expect(screen.queryByText("70")).not.toBeInTheDocument();
  });

  it("fires exactly one /channels and one /org-units fetch per mount", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(channelCalls()).toHaveLength(1);
    expect(callsTo((url) => url === "/org-units")).toHaveLength(1);
  });
});

describe("RegistryView Phase 2: org-unit names", () => {
  it("resolves company and sector display names from GET /org-units", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry();

    await waitFor(() =>
      // "United Studios" appears in the company cell AND the company dropdown.
      expect(screen.getAllByText("United Studios").length).toBeGreaterThanOrEqual(1),
    );
    expect(screen.getAllByText("Catalog Media").length).toBeGreaterThanOrEqual(1);
    // Sector resolves via the company unit's parent_id.
    expect(screen.getAllByText("TV").length).toBeGreaterThanOrEqual(1);
    // The raw company id is replaced by the display name in the table.
    expect(screen.queryByText("united-studios")).not.toBeInTheDocument();
  });

  it("falls back to raw company ids when GET /org-units fails (honest, non-blocking)", async () => {
    fetchMock().mockImplementation(
      routeRegistry({ orgUnits: () => jsonResponse({ detail: "nope" }, 403) }),
    );
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    // Raw ids render — never an invented name; the table still works.
    expect(screen.getByText("united-studios")).toBeInTheDocument();
    expect(screen.queryByText("United Studios")).not.toBeInTheDocument();
  });
});

describe("RegistryView Phase 2: Map action (PATCH /channels/{id}/mapping)", () => {
  const fillMappingForm = async () => {
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByLabelText("Channel"), {
      target: { value: "UC-MUSIC-31" },
    });
    fireEvent.change(screen.getByLabelText("Company"), {
      target: { value: "united-studios" },
    });
    fireEvent.change(screen.getByLabelText(/^Reason \(required, audited\)$/), {
      target: { value: "March source evidence received" },
    });
  };

  it("submits the mapping change and reloads channels on success", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry();
    await fillMappingForm();

    fireEvent.click(screen.getByRole("button", { name: /^submit mapping change$/i }));

    await waitFor(() =>
      expect(screen.getByText(/Mapping updated — audited/iu)).toBeInTheDocument(),
    );
    const patches = callsTo(
      (url, init) => url === "/channels/UC-MUSIC-31/mapping" && init?.method === "PATCH",
    );
    expect(patches).toHaveLength(1);
    expect(JSON.parse(String((patches[0][1] as RequestInit).body))).toEqual({
      primary_company_id: "united-studios",
      reason: "March source evidence received",
    });
    // Success refetches the channel list (initial mount + post-mutation reload).
    await waitFor(() => expect(channelCalls()).toHaveLength(2));
  });

  it("surfaces a 403 mapping failure inline and keeps the form for retry", async () => {
    fetchMock().mockImplementation(
      routeRegistry({
        mapping: () =>
          jsonResponse(
            { detail: "Missing permission: registry.manage_org_mapping" },
            403,
          ),
      }),
    );
    renderRegistry();
    await fillMappingForm();

    fireEvent.click(screen.getByRole("button", { name: /^submit mapping change$/i }));

    await waitFor(() =>
      expect(screen.getByText("Mapping change failed")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Missing permission: registry.manage_org_mapping"),
    ).toBeInTheDocument();
    // The reason input keeps its value so the operator can correct and retry.
    expect(screen.getByLabelText(/^Reason \(required, audited\)$/)).toHaveValue(
      "March source evidence received",
    );
  });

  it("drops a same-tick double-click: exactly one PATCH", async () => {
    const gate = deferred<Response>();
    let patchCalls = 0;
    fetchMock().mockImplementation(
      routeRegistry({
        mapping: () => {
          patchCalls += 1;
          return patchCalls === 1
            ? (gate.promise as unknown as Response)
            : jsonResponse({ detail: "duplicate" }, 409);
        },
      }),
    );
    renderRegistry();
    await fillMappingForm();

    const submit = screen.getByRole("button", { name: /^submit mapping change$/i });
    await act(async () => {
      fireEvent.click(submit);
      fireEvent.click(submit);
      gate.resolve(jsonResponse({ audit_event: { event_type: "CHANNEL_UPDATED" } }));
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(screen.getByText(/Mapping updated — audited/iu)).toBeInTheDocument(),
    );
    expect(
      callsTo((url, init) => url.endsWith("/mapping") && init?.method === "PATCH"),
    ).toHaveLength(1);
  });

  it("presets the mapping form channel when a row Map button is clicked", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry();
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );

    // UC-MUSIC-31 is the Export-block row, so its action button is "Map".
    fireEvent.click(screen.getByRole("button", { name: /^map$/i }));
    expect(
      (screen.getByLabelText("Channel") as HTMLSelectElement).value,
    ).toBe("UC-MUSIC-31");
  });

  it("clears a stale success note when a row Map button is clicked again", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry();
    await fillMappingForm();

    fireEvent.click(screen.getByRole("button", { name: /^submit mapping change$/i }));
    await waitFor(() =>
      expect(screen.getByText(/Mapping updated — audited/iu)).toBeInTheDocument(),
    );

    // Re-clicking a row Map button (even the SAME row) must reset the stale
    // note so a previous action's confirmation never renders under a new target.
    fireEvent.click(screen.getByRole("button", { name: /^map$/i }));
    expect(screen.queryByText(/Mapping updated — audited/iu)).not.toBeInTheDocument();
    expect(
      (screen.getByLabelText("Channel") as HTMLSelectElement).value,
    ).toBe("UC-MUSIC-31");
  });

  it("routes a mapped channel with MISSING_REVENUE_SOURCE to Review, not Map", async () => {
    // A channel that already has a primary_company_id but a missing revenue
    // source must not offer Map (which would audit an unrelated company remap).
    const mappedExportBlock: ChannelRegistryEntry = {
      youtube_channel_id: "UC-MAPPED-NOSRC",
      channel_name: "Mapped No Source",
      primary_company_id: "united-studios",
      cms_status: "INSIDE_CMS",
      content_owner_id: "ams/co-1",
      revenue_required: true,
      revenue_source_status: "MISSING_REVENUE_SOURCE",
      active: true,
    };
    fetchMock().mockImplementation(
      routeRegistry({ channels: () => jsonResponse([...CHANNELS, mappedExportBlock]) }),
    );
    renderRegistry();

    await waitFor(() =>
      expect(screen.getByText("Mapped No Source")).toBeInTheDocument(),
    );
    // The unmapped UC-MUSIC-31 still gets Map; the mapped Export-block must not.
    // With the fix: exactly 1 Map button (UC-MUSIC-31 only).
    const mapButtons = screen.getAllByRole("button", { name: /^map$/i });
    expect(mapButtons).toHaveLength(1);
  });
});

describe("RegistryView Phase 2: Assign action (propose account link)", () => {
  it("proposes an UNVERIFIED link with fixed provenance and reloads channels", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry();
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );

    // UC-SPORT-7 is the Evidence-due row, so its action button is "Assign".
    fireEvent.click(screen.getByRole("button", { name: /^assign$/i }));
    expect(screen.getByText(/Context: Sports Extra \(UC-SPORT-7\)/)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("AdSense account ID"), {
      target: { value: "pub-77" },
    });
    fireEvent.change(screen.getByLabelText("Content owner ID"), {
      target: { value: "owner-7" },
    });
    fireEvent.change(screen.getByLabelText(/^Proposal reason/), {
      target: { value: "owner statement received" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^propose link$/i }));

    await waitFor(() =>
      expect(screen.getByText(/Link proposed \(UNVERIFIED\)/)).toBeInTheDocument(),
    );
    const posts = callsTo(
      (url, init) => url === "/revenue/channel-account-links" && init?.method === "POST",
    );
    expect(posts).toHaveLength(1);
    const body = JSON.parse(String((posts[0][1] as RequestInit).body));
    expect(body).toMatchObject({
      adsense_account_id: "pub-77",
      content_owner_id: "owner-7",
      effective_month_end: null,
      provenance_kind: "OPERATOR_ASSERTED",
      provenance_payload: {},
      reason: "owner statement received",
    });
    expect(body.effective_month_start).toMatch(/^\d{4}-\d{2}$/);
    await waitFor(() => expect(channelCalls()).toHaveLength(2));
  });

  it("surfaces a 422 proposal failure inline", async () => {
    fetchMock().mockImplementation(
      routeRegistry({
        propose: () =>
          jsonResponse({ detail: "adsense_account_id is malformed" }, 422),
      }),
    );
    renderRegistry();
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );

    fireEvent.change(screen.getByLabelText("AdSense account ID"), {
      target: { value: "???" },
    });
    fireEvent.change(screen.getByLabelText("Content owner ID"), {
      target: { value: "owner-7" },
    });
    fireEvent.change(screen.getByLabelText(/^Proposal reason/), {
      target: { value: "r" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^propose link$/i }));

    await waitFor(() =>
      expect(screen.getByText("Link proposal failed")).toBeInTheDocument(),
    );
    expect(screen.getByText("adsense_account_id is malformed")).toBeInTheDocument();

    // Re-clicking a row Assign button resets the stale error note.
    fireEvent.click(screen.getByRole("button", { name: /^assign$/i }));
    expect(screen.queryByText("Link proposal failed")).not.toBeInTheDocument();
  });
});

describe("RegistryView Phase 2: Review action + gating", () => {
  it("navigates to Trace via onOpenTrace with the row's channel id", async () => {
    const onOpenTrace = vi.fn();
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry(true, true, onOpenTrace);
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );

    // UC-DRAMA-01 is the Approved row, so its action button is "Review".
    fireEvent.click(screen.getByRole("button", { name: /^review$/i }));
    expect(onOpenTrace).toHaveBeenCalledExactlyOnceWith("UC-DRAMA-01");
  });

  it("keeps write affordances disabled for read-only viewers; Review stays enabled", async () => {
    const onOpenTrace = vi.fn();
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry(false, true, onOpenTrace);
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );

    expect(screen.getByRole("button", { name: /^map$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^assign$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^review$/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^submit mapping change$/i })).toBeDisabled();
    expect(screen.getByRole("button", { name: /^propose link$/i })).toBeDisabled();
    // Import CSV is hidden (not disabled) without the capability — fail-closed.
    expect(screen.queryByRole("button", { name: /import csv/i })).not.toBeInTheDocument();
    expect(screen.getByLabelText("Channel")).toBeDisabled();
    expect(screen.getByLabelText("AdSense account ID")).toBeDisabled();
  });

  it("hides Import CSV even for registry managers without canImportChannels (PR-B gate)", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry(true);
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /import csv/i })).not.toBeInTheDocument();
  });

  it("opens the import stepper via Import CSV and Cancel restores the table untouched", async () => {
    fetchMock().mockImplementation(routeRegistry());
    renderRegistry(true, true, undefined, true);
    await waitFor(() =>
      expect(screen.getByText("UMS Drama")).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /import csv/i }));
    // The stepper's Upload step replaces the main panel's table content.
    expect(screen.getByRole("group", { name: "Import upload" })).toBeInTheDocument();
    expect(screen.queryByText("UMS Drama")).not.toBeInTheDocument();
    // No credentials are seeded, so the picker points the operator at Connectors.
    await waitFor(() =>
      expect(
        screen.getByText(/Register a youtube-analytics credential in Connectors first/i),
      ).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));
    // Cancel restores the table with NO refetch: still exactly one GET /channels.
    expect(screen.getByText("UMS Drama")).toBeInTheDocument();
    expect(channelCalls()).toHaveLength(1);
  });
});
