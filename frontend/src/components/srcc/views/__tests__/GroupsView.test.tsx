import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { GroupsView } from "@/components/srcc/views/GroupsView";
import type {
  ChannelGroupApiEntry,
  ClearOwnerStampResponse,
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

function methodOf(init: unknown): string { // skipcq: JS-0067
  return ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase();
}

// Queue one Response per expected request, in order. An unexpected extra fetch
// exhausts the queue and rejects with undefined so the over-fetch fails loudly.
function queue(...responses: Response[]) { // skipcq: JS-0067
  const fm = fetchMock();
  for (const response of responses) fm.mockResolvedValueOnce(response);
}

function callsMatching( // skipcq: JS-0067
  predicate: (url: string, init: unknown) => boolean,
) {
  return fetchMock().mock.calls.filter(([input, init]) =>
    predicate(urlOf(input), init),
  );
}

function renderGroups(canManageGroups = true) { // skipcq: JS-0067
  return render(
    <TenantProvider initialSlug="ums">
      <GroupsView canManageGroups={canManageGroups} />
    </TenantProvider>,
  );
}

/** The <tr> whose name cell holds `name`. */
function rowByName(name: string): HTMLElement { // skipcq: JS-0067
  const row = screen.getByText(name).closest("tr");
  if (!row) throw new Error(`no row for ${name}`);
  return row;
}

describe("GroupsView table", () => {
  it("renders group rows from GET /groups with owner stamp, manual cms, unstamped badge, and member counts", async () => {
    queue(jsonResponse(GROUPS));
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
    queue(jsonResponse(GROUPS));
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
    // The header carries no Sync affordance yet (Task 6), so the whole view
    // is button-free for a read-only viewer.
    expect(screen.queryAllByRole("button")).toHaveLength(0);
  });

  it("shows the empty state when there are no groups", async () => {
    queue(jsonResponse([]));
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
    queue(
      jsonResponse(GROUPS), // mount read
      jsonResponse(CLEAR_RESULT), // DELETE clear
      jsonResponse(GROUPS), // reload after mutation
    );
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
    // Groups refetched after success: mount GET + DELETE + reload GET = 3.
    await waitFor(() => expect(fetchMock().mock.calls).toHaveLength(3));
    // Panel closed on success.
    expect(
      screen.queryByLabelText(/Reason \(required, audited\)/i),
    ).not.toBeInTheDocument();
  });

  it("keeps the panel open and shows the backend 409 detail verbatim in a banner", async () => {
    queue(
      jsonResponse(GROUPS), // mount read
      jsonResponse({ detail: "Group has no owner stamp to clear." }, 409),
    );
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
    // Panel stays open for retry (reason input + Confirm still present); no reload.
    expect(
      screen.getByLabelText(/Reason \(required, audited\)/i),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /^confirm$/i }),
    ).toBeInTheDocument();
    expect(fetchMock().mock.calls).toHaveLength(2);
  });

  it("disables Confirm when the reason contains a NUL character", async () => {
    queue(jsonResponse(GROUPS));
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
    queue(
      jsonResponse(GROUPS), // mount read
      jsonResponse(ARCHIVE_RESULT), // PATCH archive
      jsonResponse(GROUPS), // reload after mutation
    );
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
    // Refetched after success: mount GET + PATCH + reload GET = 3.
    await waitFor(() => expect(fetchMock().mock.calls).toHaveLength(3));
  });
});
