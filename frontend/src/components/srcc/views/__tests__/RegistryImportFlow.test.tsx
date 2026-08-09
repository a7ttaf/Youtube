import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RegistryView from "@/components/srcc/views/RegistryView";
import type {
  ChannelImportResult,
  ChannelRegistryEntry,
  ContentOwnersResponse,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

// RegistryImportFlow is exercised THROUGH RegistryView (the GroupsView.test.tsx
// idiom for GroupsSyncFlow): the capability gate, the table swap, and the
// done-refetch are view wiring, so the flow is tested where it actually runs.
// Capability gating itself (hidden without canImportChannels, shown with) is
// pinned in RegistryView.test.tsx alongside the other gating tests.

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

// One registry row is enough here: these tests assert the table's PRESENCE
// (swapped out by the stepper, restored on cancel/done), not its derivations —
// RegistryView.test.tsx owns those.
const CHANNELS: ChannelRegistryEntry[] = [
  {
    youtube_channel_id: "UC-DRAMA-01",
    channel_name: "UMS Drama",
    primary_company_id: "united-studios",
    cms_status: "INSIDE_CMS",
    content_owner_id: "ams/content-owner-1",
    revenue_required: true,
    revenue_source_status: "OFFICIAL_CMS_REVENUE",
    active: true,
  },
];

// The least-privilege /connectors/content-owners shape: ACTIVE youtube-analytics
// account ids only (server-side filtering is the backend's pinned contract).
const ownersResponse = (accountIds: string[]): ContentOwnersResponse => {
  return { items: accountIds.map((account_id) => ({ account_id })) };
};
const DEFAULT_OWNERS = ownersResponse(["OWNERaaa"]);

const CSV_TEXT =
  "youtube_channel_id,channel_name\nUCa,Alpha Channel\nUCb,Beta Channel\n";

const rosterFile = () => {
  return new File([CSV_TEXT], "roster.csv", { type: "text/csv" });
};

// Clean dry-run plan: a CREATE (empty diff by design) + an UPDATE carrying a
// field-level diff and a group effect. UNCHANGED:0 proves zero counts hide.
const DRY_RUN_PLAN: ChannelImportResult = {
  dry_run: true,
  content_owner_id: "OWNERaaa",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, UPDATE: 1, UNCHANGED: 0 },
  rows: [
    {
      row_number: 1,
      youtube_channel_id: "UCa",
      outcome: "CREATE",
      channel_name: "Alpha Channel",
      group_id: null,
      revenue_required: true,
      changes: {},
      reason: null,
    },
    {
      row_number: 2,
      youtube_channel_id: "UCb",
      outcome: "UPDATE",
      channel_name: "Beta Channel",
      group_id: "g1",
      revenue_required: false,
      changes: {
        channel_name: { from: "Old Beta", to: "Beta Channel" },
        revenue_required: { from: true, to: false },
      },
      reason: null,
    },
  ],
};

// The applied echo of the same plan (identical shape, dry_run:false).
const APPLY_RESULT: ChannelImportResult = { ...DRY_RUN_PLAN, dry_run: false };

// A plan holding an ERROR row: Apply must be blocked client-side because the
// API would 422 the whole file (all-or-nothing).
const DRY_RUN_ERRORS: ChannelImportResult = {
  dry_run: true,
  content_owner_id: "OWNERaaa",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, ERROR: 1 },
  rows: [
    DRY_RUN_PLAN.rows[0],
    {
      row_number: 2,
      youtube_channel_id: null,
      outcome: "ERROR",
      channel_name: null,
      group_id: null,
      revenue_required: null,
      changes: {},
      reason: "missing youtube_channel_id",
    },
  ],
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

/** Reduce a request URL to pathname + decoded query (origin-independent). */
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

/** The multipart POST body, or a loud failure when it is not FormData. */
const requireFormDataBody = (init: unknown): FormData => {
  const body = (init as RequestInit).body;
  if (!(body instanceof FormData)) {
    throw new Error("expected the request body to be FormData");
  }
  return body;
};

type RouteOverrides = {
  contentOwners?: () => Response;
  importPost?: (form: FormData) => Response;
};

type Route = {
  method: string;
  path: string;
  /** Resolve the route: the test's override if present, else its default. */
  respond: (overrides: RouteOverrides, init: unknown) => Promise<Response>;
};

// Route table matched on method + pathAndQuery. The view fetches /channels and
// /org-units on mount; opening the stepper mounts the owner picker's
// content-owners read. An un-overridden import POST rejects, so a test that
// does not expect one fails loudly.
const ROUTES: Route[] = [
  {
    method: "GET",
    path: "/channels",
    respond: () => Promise.resolve(jsonResponse(CHANNELS)),
  },
  {
    method: "GET",
    path: "/org-units",
    respond: () => Promise.resolve(jsonResponse([])),
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
    path: "/channels/import",
    respond: (overrides, init) => {
      if (!overrides.importPost) {
        return Promise.reject(new Error("unexpected import POST"));
      }
      return Promise.resolve(overrides.importPost(requireFormDataBody(init)));
    },
  },
];

/** Does this request line hit `route`? Pathname + query, not raw URL. */
const routeMatches = (route: Route, method: string, url: string): boolean => {
  return route.method === method && pathAndQuery(url) === route.path;
};

/** Install the URL-keyed fetch router, with the given per-route overrides. */
const routeFetch = (overrides: RouteOverrides = {}) => {
  fetchMock().mockImplementation((input: unknown, init: unknown) => {
    const method = methodOf(init);
    const route = ROUTES.find((candidate) =>
      routeMatches(candidate, method, urlOf(input)),
    );
    if (!route) {
      return Promise.reject(new Error(`unrouted ${method} ${pathAndQuery(input)}`));
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

/** How many times GET /channels fired (mount + done-reloads). */
const channelGetCount = (): number => {
  return callsMatching(
    (path, init) => path === "/channels" && methodOf(init) === "GET",
  ).length;
};

/** All POSTs to /channels/import, FormData bodies in call order. */
const importPosts = (): FormData[] => {
  return callsMatching(
    (path, init) => path === "/channels/import" && methodOf(init) === "POST",
  ).map(([, init]) => requireFormDataBody(init));
};

const renderRegistry = () => {
  return render(
    <TenantProvider initialSlug="ums">
      <RegistryView canManageRegistry canImportChannels canViewFinance />
    </TenantProvider>,
  );
};

/** The Upload step's panel — queries are scoped inside it because the Map
 * side panel carries an identically-labelled reason input. */
const uploadPanel = (): HTMLElement => {
  return screen.getByRole("group", { name: "Import upload" });
};

/** Wait for the registry table, then open the stepper via Import CSV. */
const openImport = async () => {
  await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
  fireEvent.click(screen.getByRole("button", { name: /import csv/i }));
  expect(uploadPanel()).toBeInTheDocument();
};

/**
 * Fill the Upload step: the roster file, the seeded owner (waiting for the
 * picker's async load), and the audited reason. Returns the chosen File so
 * callers can assert the multipart part's identity.
 */
const fillUpload = async (reason = "monthly roster load"): Promise<File> => {
  const panel = uploadPanel();
  const picker = within(panel).getByLabelText("Content owner");
  await waitFor(() =>
    expect(
      within(picker).getByRole("option", { name: "OWNERaaa" }),
    ).toBeInTheDocument(),
  );
  fireEvent.change(picker, { target: { value: "OWNERaaa" } });
  const file = rosterFile();
  fireEvent.change(within(panel).getByLabelText("Roster CSV"), {
    target: { files: [file] },
  });
  fireEvent.change(within(panel).getByLabelText("Reason (required, audited)"), {
    target: { value: reason },
  });
  return file;
};

/** Import-route responder for the clean plan: dry-run -> plan, apply -> echo. */
const cleanImport = (form: FormData): Response => {
  return jsonResponse(form.get("dry_run") === "true" ? DRY_RUN_PLAN : APPLY_RESULT);
};

/** Render, open the stepper, fill Upload, fire the dry-run, await Preview. */
const runDryRunToPreview = async (
  importPost: (form: FormData) => Response,
): Promise<File> => {
  routeFetch({ importPost });
  renderRegistry();
  await openImport();
  const file = await fillUpload();
  fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
  await waitFor(() =>
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
  );
  return file;
};

describe("RegistryImportFlow stepper (through RegistryView)", () => {
  it("runs the happy path: upload -> dry-run preview -> apply -> applied counts + refetch", async () => {
    const file = await runDryRunToPreview(cleanImport);

    // The dry-run POST is multipart FormData with exactly the four wire
    // fields (cms_status omitted -> backend default INSIDE_CMS applies).
    expect(importPosts()).toHaveLength(1);
    const dryRunForm = importPosts()[0];
    expect([...dryRunForm.keys()].sort()).toEqual([
      "content_owner_id",
      "dry_run",
      "file",
      "reason",
    ]);
    expect(dryRunForm.get("content_owner_id")).toBe("OWNERaaa");
    expect(dryRunForm.get("dry_run")).toBe("true");
    expect(dryRunForm.get("reason")).toBe("monthly roster load");
    const filePart = dryRunForm.get("file");
    expect(filePart).toBeInstanceOf(File);
    expect((filePart as File).name).toBe("roster.csv");

    // Preview: non-zero counts strip (UNCHANGED:0 hidden), per-row outcome
    // chips, the joined field diff, and the group effect.
    expect(screen.getByText("CREATE: 1 · UPDATE: 1")).toBeInTheDocument();
    expect(screen.queryByText(/UNCHANGED: 0/)).not.toBeInTheDocument();
    expect(screen.getByText("CREATE")).toBeInTheDocument();
    expect(screen.getByText("UPDATE")).toBeInTheDocument();
    expect(screen.getByText("Alpha Channel")).toBeInTheDocument();
    expect(
      screen.getByText("channel_name: Old Beta → Beta Channel"),
    ).toBeInTheDocument();
    expect(screen.getByText("revenue_required: true → false")).toBeInTheDocument();
    expect(screen.getByText("g1")).toBeInTheDocument();

    // Spec-mandated revenue flag column: the CREATE row's diff is EMPTY by
    // design, so this cell is the only preview surface for its
    // revenue_required=true (the finance-sensitive default when view_revenue
    // is absent from the CSV) before the all-or-nothing apply.
    expect(screen.getByText("Revenue")).toBeInTheDocument();
    expect(screen.getByText("Yes")).toBeInTheDocument();
    expect(screen.getByText("No")).toBeInTheDocument();

    // Apply is allowed on the clean plan and fires dry_run:false with the
    // SAME roster file the dry run sent.
    const applyButton = screen.getByRole("button", { name: /^apply$/i });
    expect(applyButton).toBeEnabled();
    fireEvent.click(applyButton);
    await waitFor(() =>
      expect(
        screen.getByRole("group", { name: "Import applied" }),
      ).toBeInTheDocument(),
    );
    expect(importPosts()).toHaveLength(2);
    expect(importPosts()[1].get("dry_run")).toBe("false");
    expect(importPosts()[1].get("file")).toBe(file);

    // Applied step: counts + reason echo. The refetch waits for Back to
    // Registry — leaving the stepper is what reloads the table.
    expect(screen.getByText("CREATE: 1 · UPDATE: 1")).toBeInTheDocument();
    expect(screen.getByText("Reason: monthly roster load")).toBeInTheDocument();
    expect(channelGetCount()).toBe(1);
    fireEvent.click(screen.getByRole("button", { name: /back to registry/i }));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    await waitFor(() => expect(channelGetCount()).toBe(2));
  });

  it("blocks Apply while any ERROR row exists, naming the all-or-nothing contract", async () => {
    await runDryRunToPreview((form) =>
      jsonResponse(form.get("dry_run") === "true" ? DRY_RUN_ERRORS : APPLY_RESULT),
    );

    // Apply is disabled with the explanatory 422 title.
    const applyButton = screen.getByRole("button", { name: /^apply$/i });
    expect(applyButton).toBeDisabled();
    expect(applyButton).toHaveAttribute(
      "title",
      "The API refuses plans with error rows (422)",
    );
    // The remedy note names the all-or-nothing contract.
    expect(screen.getByText(/Error rows block apply/i)).toBeInTheDocument();
    expect(screen.getByText(/all-or-nothing/i)).toBeInTheDocument();
    // The ERROR chip renders, and the backend's verbatim row reason sits on a
    // warn-toned row.
    expect(screen.getByText("ERROR")).toBeInTheDocument();
    const errorRow = screen.getByText("missing youtube_channel_id").closest("tr");
    expect(errorRow).toHaveAttribute("data-tone", "warn");
    // Only the dry-run POST ever fired — nothing attempted the apply.
    expect(importPosts()).toHaveLength(1);
  });

  it("restores the table untouched when Cancel is used from Preview", async () => {
    await runDryRunToPreview(cleanImport);

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    // Table restored; no refetch (still exactly one GET /channels) and no
    // further import POST — the previewed plan is simply discarded.
    expect(screen.getByText("UMS Drama")).toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: "Import preview" }),
    ).not.toBeInTheDocument();
    expect(channelGetCount()).toBe(1);
    expect(importPosts()).toHaveLength(1);
  });

  it("disables the owner picker with the Connectors pointer and blocks Preview without credentials", async () => {
    routeFetch({ contentOwners: () => jsonResponse(ownersResponse([])) });
    renderRegistry();
    await openImport();

    const panel = uploadPanel();
    const picker = within(panel).getByLabelText("Content owner");
    await waitFor(() =>
      expect(
        within(panel).getByText(
          /Register a youtube-analytics credential in Connectors first\./i,
        ),
      ).toBeInTheDocument(),
    );
    expect(picker).toBeDisabled();
    // Only the placeholder option — no ownerable entries.
    expect(within(picker).getAllByRole("option")).toHaveLength(1);

    // Even with a roster file + valid reason, Preview stays blocked: no owner
    // can be selected, so the dry-run can never fire.
    fireEvent.change(within(panel).getByLabelText("Roster CSV"), {
      target: { files: [rosterFile()] },
    });
    fireEvent.change(within(panel).getByLabelText("Reason (required, audited)"), {
      target: { value: "monthly roster load" },
    });
    expect(within(panel).getByRole("button", { name: /^preview$/i })).toBeDisabled();
    expect(importPosts()).toHaveLength(0);
  });

  it("collapses a 503 dry-run failure to generic copy + status and allows retry", async () => {
    routeFetch({
      importPost: () => jsonResponse({ detail: "Credential unavailable." }, 503),
    });
    renderRegistry();
    await openImport();
    await fillUpload();

    fireEvent.click(
      within(uploadPanel()).getByRole("button", { name: /^preview$/i }),
    );

    await waitFor(() =>
      expect(screen.getByText("Dry-run failed")).toBeInTheDocument(),
    );
    // Unlike the sync flow, the import route has NO canned-503 contract
    // (its 503s may carry raw diagnostics), so the banner shows the generic
    // fallback + numeric status: the raw detail never renders and no
    // Connectors pointer is appended.
    expect(
      screen.getByText("The import request failed (HTTP 503)."),
    ).toBeInTheDocument();
    expect(screen.queryByText("Credential unavailable.")).not.toBeInTheDocument();
    expect(screen.queryByText(/Connectors view/i)).not.toBeInTheDocument();
    // Still on Upload — never advanced — and re-enabled for a retry.
    expect(uploadPanel()).toBeInTheDocument();
    expect(
      screen.queryByRole("group", { name: "Import preview" }),
    ).not.toBeInTheDocument();
    expect(
      within(uploadPanel()).getByRole("button", { name: /^preview$/i }),
    ).toBeEnabled();
  });
});
