import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GroupsView } from "@/components/srcc/views/GroupsView";
import type {
  ChannelGroupApiEntry,
  ClearOwnerStampResponse,
  ContentOwnersResponse,
  GroupSyncResult,
  GroupUpdateResponse,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

// Three real-shaped groups exercising every table branch:
//  - g1: synced, active, 2 members, CMS id + owner stamp  -> Clear stamp + Archive
//  - g2: manual, active, 1 member, NULL cms id + NULL owner -> "manual" + unstamped
//  - g3: synced, archived, 0 members, CMS id + owner stamp  -> Clear stamp + Restore
const GROUPS: ChannelGroupApiEntry[] = [
  {
    id: "g1",
    name: "TV Sector",
    group_type: "SYNCED",
    active: true,
    channel_ids: ["UCa", "UCb"],
    cms_group_id: "deWjsA",
    content_owner_id: "COabc",
  },
  {
    id: "g2",
    name: "Manual Roster",
    group_type: "CUSTOM",
    active: true,
    channel_ids: ["UCc"],
    cms_group_id: null,
    content_owner_id: null,
  },
  {
    id: "g3",
    name: "Old Archive",
    group_type: "SYNCED",
    active: false,
    channel_ids: [],
    cms_group_id: "arch1",
    content_owner_id: "COxyz",
  },
];

const CLEAR_RESULT: ClearOwnerStampResponse = {
  id: "g1",
  name: "TV Sector",
  group_type: "SYNCED",
  active: true,
  channel_ids: ["UCa", "UCb"],
  cms_group_id: "deWjsA",
  content_owner_id: null,
  audit_event: {
    event_type: "GROUP_UPDATED",
    entity_type: "channel_group",
    entity_id: "g1",
    scope_type: "global",
    scope_id: null,
    reason: "wrong owner stamp",
    sensitive: false,
  },
};

const ARCHIVE_RESULT: GroupUpdateResponse = {
  id: "g2",
  name: "Manual Roster",
  group_type: "CUSTOM",
  active: false,
  channel_ids: ["UCc"],
  cms_group_id: null,
  content_owner_id: null,
  audit_event: { event_type: "GROUP_UPDATED" },
};

// Content-owner fixtures for the picker. The backend's least-privilege
// /connectors/content-owners route returns ONLY the account_id strings of
// ACTIVE youtube-analytics credentials — the ACTIVE/connector-key filtering
// happens server-side (pinned by tests/api/test_connectors_api.py), so the
// picker renders exactly the ids it is given.
const ownersResponse = (accountIds: string[]): ContentOwnersResponse => {
  return { items: accountIds.map((account_id) => ({ account_id })) };
};
const DEFAULT_OWNERS = ownersResponse(["OWNERaaa"]);

// Dry-run diff with a CONFLICT (+ a CREATE) and unknown channels: exercises the
// warn row tone, the disabled Apply, and the remedy + unknown notes.
const DRY_RUN_CONFLICT: GroupSyncResult = {
  dry_run: true,
  content_owner_id: "OWNERaaa",
  counts: { CREATE: 1, CONFLICT: 1 },
  unknown_channel_total: 3,
  non_channel_member_count: 0,
  groups: [
    {
      cms_group_id: "cmsCreate",
      outcome: "CREATE",
      title: "New Sector",
      local_group_id: null,
      name_change: null,
      active_change: null,
      members_added: ["UCa", "UCb"],
      members_removed: [],
      unknown_channel_ids: ["UCx"],
      unknown_channel_count: 1,
      will_adopt_content_owner: true,
    },
    {
      cms_group_id: "cmsConflict",
      outcome: "CONFLICT",
      title: "Owned Elsewhere",
      local_group_id: "g9",
      name_change: null,
      active_change: null,
      members_added: [],
      members_removed: [],
      unknown_channel_ids: [],
      unknown_channel_count: 0,
      will_adopt_content_owner: false,
    },
  ],
};

// Conflict-free dry-run (a rename with member deltas) -> Apply is allowed.
const DRY_RUN_CLEAN: GroupSyncResult = {
  dry_run: true,
  content_owner_id: "OWNERaaa",
  counts: { RENAME: 1 },
  unknown_channel_total: 0,
  non_channel_member_count: 0,
  groups: [
    {
      cms_group_id: "cmsRename",
      outcome: "RENAME",
      title: "Renamed Group",
      local_group_id: "g5",
      name_change: ["Old Name", "New Name"],
      active_change: null,
      members_added: ["UCn"],
      members_removed: ["UCm"],
      unknown_channel_ids: [],
      unknown_channel_count: 0,
      will_adopt_content_owner: false,
    },
  ],
};

// The applied (dry_run:false) result echoed on the Applied step. UNCHANGED:0 is
// present to prove zero counts are hidden.
const APPLY_RESULT: GroupSyncResult = {
  dry_run: false,
  content_owner_id: "OWNERaaa",
  counts: { RENAME: 1, UNCHANGED: 0, CREATE: 2 },
  unknown_channel_total: 0,
  non_channel_member_count: 0,
  groups: [],
};

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

/**
 * Reduce a request URL to its pathname + (decoded) query string, stripping any
 * origin so routing is independent of VITE_API_BASE_URL (resolveUrl prefixes
 * relative paths with the configured base). A bare "/groups" and an absolute
 * "https://api.example/groups" both reduce to "/groups"; the content-owners
 * route keeps its connector_key query because the hook builds it deterministically.
 */
const pathAndQuery = (input: unknown): string => {
  try {
    const parsed = new URL(urlOf(input), "http://test.local");
    const query = parsed.search ? `?${parsed.searchParams.toString()}` : "";
    return `${parsed.pathname}${query}`;
  } catch {
    return urlOf(input);
  }
};

const methodOf = (init: unknown): string => {
  return ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase();
};

type SyncBody = { content_owner_id: string; dry_run: boolean; reason: string };

type RouteOverrides = {
  groups?: () => Response;
  contentOwners?: () => Response;
  sync?: (body: SyncBody) => Response;
  clear?: () => Response;
  archive?: () => Response;
};

type Route = {
  method: string;
  /** Exact path, or a path PREFIX when `prefix` is set (id-bearing routes). */
  path: string;
  prefix?: true;
  /** Resolve the route: the test's override if present, else its default. */
  respond: (overrides: RouteOverrides, init: unknown) => Promise<Response>;
};

// Route table for the fetch router below, matched in order on method + path.
// The view fetches /groups AND (for a manager) /connectors/content-owners on
// mount, and effects fire child-before-parent, so the mount order is not
// guaranteed — key on method+path instead of a positional queue. Each route
// has a sensible default; tests override only what they assert. An
// un-overridden sync POST rejects, so a test that does not expect one fails
// loudly.
const ROUTES: Route[] = [
  {
    method: "GET",
    path: "/groups",
    respond: (overrides) =>
      Promise.resolve((overrides.groups ?? (() => jsonResponse(GROUPS)))()),
  },
  {
    method: "GET",
    path: "/connectors/content-owners?connector_key=youtube-analytics",
    respond: (overrides) =>
      Promise.resolve(
        (overrides.contentOwners ?? (() => jsonResponse(DEFAULT_OWNERS)))(),
      ),
  },
  {
    method: "POST",
    path: "/channels/groups/sync",
    respond: (overrides, init) => {
      if (!overrides.sync) return Promise.reject(new Error("unexpected sync POST"));
      const body = JSON.parse(String((init as RequestInit).body)) as SyncBody;
      return Promise.resolve(overrides.sync(body));
    },
  },
  {
    method: "DELETE",
    path: "/groups/",
    prefix: true,
    respond: (overrides) =>
      Promise.resolve((overrides.clear ?? (() => jsonResponse(CLEAR_RESULT)))()),
  },
  {
    method: "PATCH",
    path: "/groups/",
    prefix: true,
    respond: (overrides) =>
      Promise.resolve((overrides.archive ?? (() => jsonResponse(ARCHIVE_RESULT)))()),
  },
];

/** Does this request line hit `route`? Pathname+query, not raw URL, so an
 *  absolute URL built by resolveUrl (VITE_API_BASE_URL set) still matches. */
const routeMatches = (route: Route, method: string, url: string): boolean => {
  if (route.method !== method) return false;
  const target = pathAndQuery(url);
  return route.prefix ? target.startsWith(route.path) : target === route.path;
};

/** Install the URL-keyed fetch router, with the given per-route overrides. */
const routeFetch = (overrides: RouteOverrides = {}) => {
  fetchMock().mockImplementation((input: unknown, init: unknown) => {
    const method = methodOf(init);
    const route = ROUTES.find((candidate) =>
      routeMatches(candidate, method, urlOf(input)),
    );
    if (!route) {
      return Promise.reject(
        new Error(`unrouted ${method} ${pathAndQuery(input)}`),
      );
    }
    return route.respond(overrides, init);
  });
};

const callsMatching = (
  predicate: (path: string, init: unknown) => boolean,
) => {
  return fetchMock().mock.calls.filter(([input, init]) =>
    predicate(pathAndQuery(input), init),
  );
};

/** How many times the group LIST (GET /groups) was fetched (mount + reloads). */
const groupGetCount = (): number => {
  return callsMatching((path, init) => path === "/groups" && methodOf(init) === "GET")
    .length;
};

/** All POSTs to the sync route, parsed bodies in call order. */
const syncPosts = (): SyncBody[] => {
  return callsMatching(
    (path, init) => path === "/channels/groups/sync" && methodOf(init) === "POST",
  ).map(([, init]) => JSON.parse(String((init as RequestInit).body)) as SyncBody);
};

const renderGroups = (canManageGroups = true) => {
  return render(
    <TenantProvider initialSlug="ums">
      <GroupsView canManageGroups={canManageGroups} />
    </TenantProvider>,
  );
};

/** The <tr> whose name cell holds `name`. */
const rowByName = (name: string): HTMLElement => {
  const row = screen.getByText(name).closest("tr");
  if (!row) throw new Error(`no row for ${name}`);
  return row;
};

/**
 * Manager flow helper: wait for credentials, select the ACTIVE owner, open the
 * stepper, enter `reason`, and fire the dry-run. `sync` decides the dry-run /
 * apply responses (defaults: clean dry-run, then APPLY_RESULT).
 */
const openStepperAndRunDryRun = async (
  reason: string,
  sync: (body: SyncBody) => Response = (body) =>
    jsonResponse(body.dry_run ? DRY_RUN_CLEAN : APPLY_RESULT),
) => {
  routeFetch({ sync });
  renderGroups();
  const picker = await screen.findByLabelText("Content owner");
  await waitFor(() =>
    expect(within(picker).getByRole("option", { name: "OWNERaaa" })).toBeInTheDocument(),
  );
  fireEvent.change(picker, { target: { value: "OWNERaaa" } });
  fireEvent.click(screen.getByRole("button", { name: /sync \(dry-run\)/i }));
  fireEvent.change(screen.getByLabelText(/Reason \(required, audited\)/i), {
    target: { value: reason },
  });
  fireEvent.click(screen.getByRole("button", { name: /run dry-run/i }));
};

describe("GroupsView table", () => {
  it("renders group rows from GET /groups with owner stamp, manual cms, unstamped badge, and member counts", async () => {
    routeFetch();
    renderGroups();

    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    // Owner stamp text renders verbatim.
    expect(screen.getByText("COabc")).toBeInTheDocument();
    expect(screen.getByText("COxyz")).toBeInTheDocument();
    // NULL cms id -> muted "manual".
    expect(screen.getByText("manual")).toBeInTheDocument();
    // NULL owner -> the unstamped marker, tagged for adopt-ability.
    expect(screen.getByTestId("owner-unstamped")).toBeInTheDocument();
    // Member counts = channel_ids.length, per row.
    expect(within(rowByName("TV Sector")).getByText("2")).toBeInTheDocument();
    expect(within(rowByName("Manual Roster")).getByText("1")).toBeInTheDocument();
    expect(within(rowByName("Old Archive")).getByText("0")).toBeInTheDocument();
    // The CMS group id renders for the synced row.
    expect(screen.getByText("deWjsA")).toBeInTheDocument();
  });

  it("renders NO action buttons anywhere when the viewer cannot manage groups", async () => {
    routeFetch();
    renderGroups(false);

    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: /clear stamp/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^archive$/i }),
    ).not.toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /^restore$/i }),
    ).not.toBeInTheDocument();
    // The sync cluster is manager-only, so a read-only viewer gets the plain
    // header (title + subtitle): no Sync button and no owner picker at all.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
    expect(screen.queryByLabelText("Content owner")).not.toBeInTheDocument();
  });

  it("shows the empty state when there are no groups", async () => {
    routeFetch({ groups: () => jsonResponse([]) });
    renderGroups();

    await waitFor(() =>
      expect(screen.getByText(/No groups yet/i)).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/import a roster, then sync your content owner/i),
    ).toBeInTheDocument();
  });
});

describe("GroupsView clear-stamp flow", () => {
  it("opens a confirm panel, blocks a blank reason, then DELETEs the stamp and refetches", async () => {
    routeFetch({ clear: () => jsonResponse(CLEAR_RESULT) });
    renderGroups();

    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    // Clear stamp exists only on stamped rows; open g1's panel.
    fireEvent.click(
      within(rowByName("TV Sector")).getByRole("button", {
        name: /clear stamp/i,
      }),
    );

    // Panel opened: explanatory line + reason input + Confirm/Cancel.
    expect(screen.getByText(/Erases the owner stamp/i)).toBeInTheDocument();
    const reasonInput = screen.getByLabelText(/Reason \(required, audited\)/i);
    const confirm = screen.getByRole("button", { name: /^confirm$/i });
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeInTheDocument();
    // Blank reason -> Confirm disabled.
    expect(confirm).toBeDisabled();

    // A reason WITH spaces is valid (only blank / NUL are rejected).
    fireEvent.change(reasonInput, { target: { value: "wrong owner stamp" } });
    expect(confirm).toBeEnabled();
    fireEvent.click(confirm);

    // The DELETE fired with the encoded reason query param + DELETE method.
    await waitFor(() =>
      expect(
        callsMatching((url, init) => methodOf(init) === "DELETE"),
      ).toHaveLength(1),
    );
    const deleteCall = callsMatching((url, init) => methodOf(init) === "DELETE")[0];
    expect(urlOf(deleteCall[0])).toBe(
      "/groups/g1/content-owner?reason=wrong%20owner%20stamp",
    );
    // Groups refetched after success: mount GET + reload GET = 2.
    await waitFor(() => expect(groupGetCount()).toBe(2));
    // Panel closed on success.
    expect(
      screen.queryByLabelText(/Reason \(required, audited\)/i),
    ).not.toBeInTheDocument();
  });

  it("keeps the panel open and shows the backend 409 detail verbatim in a banner", async () => {
    routeFetch({
      clear: () => jsonResponse({ detail: "Group has no owner stamp to clear." }, 409),
    });
    renderGroups();

    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    fireEvent.click(
      within(rowByName("TV Sector")).getByRole("button", {
        name: /clear stamp/i,
      }),
    );
    fireEvent.change(
      screen.getByLabelText(/Reason \(required, audited\)/i),
      { target: { value: "wrong owner stamp" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    // The banner shows the backend detail verbatim.
    await waitFor(() =>
      expect(
        screen.getByText("Group has no owner stamp to clear."),
      ).toBeInTheDocument(),
    );
    // Panel stays open for retry (reason input + Confirm still present).
    expect(
      screen.getByLabelText(/Reason \(required, audited\)/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^confirm$/i }),
    ).toBeInTheDocument();
    // No reload on failure: only the mount GET fired for the list.
    await waitFor(() =>
      expect(
        callsMatching((url, init) => methodOf(init) === "DELETE"),
      ).toHaveLength(1),
    );
    expect(groupGetCount()).toBe(1);
  });

  it("shows generic copy on a 500 and never renders raw backend internals", async () => {
    routeFetch({
      clear: () =>
        jsonResponse(
          {
            detail:
              'psycopg.DataError: relation "audit_logs" at db-host-04.internal:5432',
          },
          500,
        ),
    });
    renderGroups();

    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    fireEvent.click(
      within(rowByName("TV Sector")).getByRole("button", {
        name: /clear stamp/i,
      }),
    );
    fireEvent.change(
      screen.getByLabelText(/Reason \(required, audited\)/i),
      { target: { value: "wrong owner stamp" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    // Qodo rule 1052052 guard: a 5xx detail is internal diagnostics, so the UI
    // shows generic copy + the numeric status — never the raw payload.
    await waitFor(() =>
      expect(
        screen.getByText("The change could not be saved (HTTP 500)."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/psycopg\.DataError/)).not.toBeInTheDocument();
    expect(screen.queryByText(/db-host-04\.internal/)).not.toBeInTheDocument();
    // Panel stays open for retry, and no reload fired.
    expect(
      screen.getByLabelText(/Reason \(required, audited\)/i),
    ).toBeInTheDocument();
    expect(groupGetCount()).toBe(1);
  });

  it("disables Confirm when the reason contains a NUL character", async () => {
    routeFetch();
    renderGroups();

    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    fireEvent.click(
      within(rowByName("TV Sector")).getByRole("button", {
        name: /clear stamp/i,
      }),
    );
    fireEvent.change(
      screen.getByLabelText(/Reason \(required, audited\)/i),
      { target: { value: "bad\u0000reason" } },
    );
    // The backend 422-rejects NUL in audit reasons; the UI blocks it up front.
    expect(screen.getByRole("button", { name: /^confirm$/i })).toBeDisabled();
  });
});

describe("GroupsView archive flow", () => {
  it("PATCHes the group inactive with the reason, then refetches", async () => {
    routeFetch({ archive: () => jsonResponse(ARCHIVE_RESULT) });
    renderGroups();

    await waitFor(() =>
      expect(screen.getByText("Manual Roster")).toBeInTheDocument(),
    );
    // g2 is active -> the action reads "Archive".
    fireEvent.click(
      within(rowByName("Manual Roster")).getByRole("button", {
        name: /^archive$/i,
      }),
    );
    fireEvent.change(
      screen.getByLabelText(/Reason \(required, audited\)/i),
      { target: { value: "stale group" } },
    );
    fireEvent.click(screen.getByRole("button", { name: /^confirm$/i }));

    await waitFor(() =>
      expect(
        callsMatching((url, init) => url === "/groups/g2" && methodOf(init) === "PATCH"),
      ).toHaveLength(1),
    );
    const patchCall = callsMatching(
      (url, init) => url === "/groups/g2" && methodOf(init) === "PATCH",
    )[0];
    expect(
      JSON.parse(String((patchCall[1] as RequestInit).body)),
    ).toEqual({ active: false, reason: "stale group" });
    // Refetched after success: mount GET + reload GET = 2.
    await waitFor(() => expect(groupGetCount()).toBe(2));
  });
});

describe("GroupsView sync header / owner picker", () => {
  it("feeds the picker from /connectors/content-owners, never /connectors/credentials", async () => {
    routeFetch();
    renderGroups();

    const picker = await screen.findByLabelText("Content owner");
    await waitFor(() =>
      expect(
        within(picker).getByRole("option", { name: "OWNERaaa" }),
      ).toBeInTheDocument(),
    );
    // Qodo PR #174 regression guard: the picker must read the least-privilege,
    // MANAGE_GROUPS-gated route — not the MANAGE_CONNECTORS-gated credential
    // list, which 403s for group managers without connector management.
    await waitFor(() =>
      expect(
        callsMatching((url) => url.startsWith("/connectors/content-owners")),
      ).toHaveLength(1),
    );
    expect(
      callsMatching((url) => url.startsWith("/connectors/credentials")),
    ).toHaveLength(0);
  });

  it("lists exactly the account ids the backend returns", async () => {
    routeFetch({
      contentOwners: () => jsonResponse(ownersResponse(["OWNERaaa", "OWNERbbb"])),
    });
    renderGroups();

    const picker = await screen.findByLabelText("Content owner");
    await waitFor(() =>
      expect(
        within(picker).getByRole("option", { name: "OWNERbbb" }),
      ).toBeInTheDocument(),
    );
    // Placeholder + the two returned owners, in backend order; the
    // ACTIVE/connector-key filtering is the backend's contract, not the UI's.
    const optionValues = within(picker)
      .getAllByRole("option")
      .map((option) => (option as HTMLOptionElement).value);
    expect(optionValues).toEqual(["", "OWNERaaa", "OWNERbbb"]);
    expect(picker).toBeEnabled();
  });

  it("disables the picker and points at Connectors when no content owner exists", async () => {
    routeFetch({ contentOwners: () => jsonResponse(ownersResponse([])) });
    renderGroups();

    const picker = await screen.findByLabelText("Content owner");
    await waitFor(() =>
      expect(
        screen.getByText(
          /Register a youtube-analytics credential in Connectors first\./i,
        ),
      ).toBeInTheDocument(),
    );
    expect(picker).toBeDisabled();
    // Only the placeholder option — no ownerable rows.
    expect(within(picker).getAllByRole("option")).toHaveLength(1);
  });
});

describe("GroupsView sync button gating", () => {
  it("hides the Sync button from a viewer who cannot manage groups", async () => {
    routeFetch();
    renderGroups(false);

    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("button", { name: /sync \(dry-run\)/i }),
    ).not.toBeInTheDocument();
  });

  it("disables the Sync button until a content owner is selected", async () => {
    routeFetch();
    renderGroups();

    const syncButton = await screen.findByRole("button", {
      name: /sync \(dry-run\)/i,
    });
    // No selection yet -> disabled.
    expect(syncButton).toBeDisabled();

    const picker = await screen.findByLabelText("Content owner");
    await waitFor(() =>
      expect(
        within(picker).getByRole("option", { name: "OWNERaaa" }),
      ).toBeInTheDocument(),
    );
    fireEvent.change(picker, { target: { value: "OWNERaaa" } });
    expect(syncButton).toBeEnabled();
  });
});

describe("GroupsView sync stepper", () => {
  it("POSTs the dry-run with {content_owner_id, dry_run:true, reason} after entering a reason", async () => {
    await openStepperAndRunDryRun("quarterly sync");

    await waitFor(() => expect(syncPosts()).toHaveLength(1));
    expect(syncPosts()[0]).toEqual({
      content_owner_id: "OWNERaaa",
      dry_run: true,
      reason: "quarterly sync",
    });
  });

  it("renders the preview diff with a CONFLICT row (warn tone), a disabled Apply, and the remedy + unknown notes", async () => {
    await openStepperAndRunDryRun("cms sync", (body) =>
      jsonResponse(body.dry_run ? DRY_RUN_CONFLICT : APPLY_RESULT),
    );

    // Advanced to Preview.
    await waitFor(() =>
      expect(screen.getByText("Review the plan")).toBeInTheDocument(),
    );
    // One row per group: the CREATE + CONFLICT titles render.
    expect(screen.getByText("New Sector")).toBeInTheDocument();
    expect(screen.getByText("Owned Elsewhere")).toBeInTheDocument();
    // Outcome chips.
    expect(screen.getByText("CREATE")).toBeInTheDocument();
    expect(screen.getByText("CONFLICT")).toBeInTheDocument();
    // The CONFLICT row carries data-tone="warn".
    expect(screen.getByText("Owned Elsewhere").closest("tr")).toHaveAttribute(
      "data-tone",
      "warn",
    );
    // Member deltas for the CREATE row (+2 added / -0 removed).
    expect(screen.getByText("+2 / -0")).toBeInTheDocument();
    // Apply disabled while conflicted, with the explanatory 409 title.
    const applyButton = screen.getByRole("button", { name: /^apply$/i });
    expect(applyButton).toBeDisabled();
    expect(applyButton).toHaveAttribute(
      "title",
      "The API refuses conflicted plans (409)",
    );
    // Remedy line + unknown-channels note.
    expect(screen.getByText(/Conflicts block apply/i)).toBeInTheDocument();
    expect(
      screen.getByText(/3 channels are in CMS but not in the registry/i),
    ).toBeInTheDocument();
  });

  it("applies a conflict-free plan and refetches groups from the Applied step", async () => {
    await openStepperAndRunDryRun("apply me");

    await waitFor(() =>
      expect(screen.getByText("Review the plan")).toBeInTheDocument(),
    );
    const applyButton = screen.getByRole("button", { name: /^apply$/i });
    expect(applyButton).toBeEnabled();
    fireEvent.click(applyButton);

    // Applied step: non-zero counts + reason echo (UNCHANGED:0 is hidden).
    await waitFor(() =>
      expect(screen.getByText("Sync applied")).toBeInTheDocument(),
    );
    expect(screen.getByText("RENAME: 1")).toBeInTheDocument();
    expect(screen.getByText("CREATE: 2")).toBeInTheDocument();
    expect(screen.queryByText("UNCHANGED: 0")).not.toBeInTheDocument();
    expect(screen.getByText("Reason: apply me")).toBeInTheDocument();

    // The apply POST fired with dry_run:false.
    expect(syncPosts()).toHaveLength(2);
    expect(syncPosts()[1].dry_run).toBe(false);

    // Before "Back to groups": only the mount read. After: a reload (2).
    expect(groupGetCount()).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: /back to groups/i }));
    await waitFor(() => expect(groupGetCount()).toBe(2));
  });

  it("shows a 503 detail with a Connectors pointer, stays on Reason, and re-enables retry", async () => {
    await openStepperAndRunDryRun("try sync", () =>
      jsonResponse({ detail: "Credential unavailable." }, 503),
    );

    // Banner shows the backend detail verbatim + the 503 Connectors pointer.
    await waitFor(() =>
      expect(screen.getByText(/Credential unavailable\./)).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/check the credential in the Connectors view\./i),
    ).toBeInTheDocument();
    // Still on the Reason step: reason input + Run dry-run present, no Apply.
    expect(
      screen.getByLabelText(/Reason \(required, audited\)/i),
    ).toBeInTheDocument();
    const runButton = screen.getByRole("button", { name: /run dry-run/i });
    expect(runButton).toBeEnabled(); // re-enabled for a retry
    expect(
      screen.queryByRole("button", { name: /^apply$/i }),
    ).not.toBeInTheDocument();
  });

  it("restores the table without refetching groups when Cancel is used from Preview", async () => {
    await openStepperAndRunDryRun("peek");

    await waitFor(() =>
      expect(screen.getByText("Review the plan")).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    // Table restored (no apply committed) and no reload fired.
    await waitFor(() =>
      expect(screen.getByText("TV Sector")).toBeInTheDocument(),
    );
    expect(groupGetCount()).toBe(1);
  });
});
