import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RegistryView from "@/components/srcc/views/RegistryView";
import type {
  ChannelImportResult,
  ChannelRegistryEntry,
  ContentOwnersResponse,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";
import { WriteInFlightProvider } from "@/contexts/WriteInFlightContext";

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
  plan_fingerprint: "plan-clean-v1",
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

// The applied echo of the same plan (identical shape, dry_run:false).
const APPLY_RESULT: ChannelImportResult = { ...DRY_RUN_PLAN, dry_run: false };

// A plan holding an ERROR row: Apply must be blocked client-side because the
// API would 422 the whole file (all-or-nothing).
const DRY_RUN_ERRORS: ChannelImportResult = {
  dry_run: true,
  content_owner_id: "OWNERaaa",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, ERROR: 1 },
  plan_fingerprint: "plan-errors-v1",
  rows: [
    DRY_RUN_PLAN.rows[0],
    {
      row_number: 2,
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
  // Deliberately wider than the synchronous default: a Promise-returning
  // override lets a test HOLD the import POST in flight, which is the only way
  // to observe the mid-request exit guards (Cancel/Back) the flow now applies.
  importPost?: (form: FormData) => Response | Promise<Response>;
};

/** A pending Response plus its settlers, for holding a request in flight.
 * `reject` models a TRANSPORT failure — a fetch that never produced a
 * response, which is a different outcome from an HTTP error status. */
const deferredResponse = () => {
  let release!: (response: Response) => void;
  let reject!: (error: Error) => void;
  const pending = new Promise<Response>((resolve, fail) => {
    release = resolve;
    reject = fail;
  });
  return { pending, release, reject };
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

const renderRegistry = (
  writeLatch: { reason: string | null; setReason: (reason: string | null) => void } = {
    reason: null,
    setReason: () => undefined,
  },
) => {
  return render(
    <TenantProvider initialSlug="ums">
      <WriteInFlightProvider value={writeLatch}>
        <RegistryView canManageRegistry canImportChannels canViewFinance />
      </WriteInFlightProvider>
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
  // Same widening as RouteOverrides.importPost: the in-flight guard tests hand
  // in a responder whose APPLY leg is a pending promise.
  importPost: (form: FormData) => Response | Promise<Response>,
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
    // Both halves of the channel identity render: names are mutable and not
    // unique, so the durable youtube_channel_id must be visible for the
    // operator to tell which channel a CREATE/UPDATE will touch.
    expect(screen.getByText("UCa")).toBeInTheDocument();
    expect(screen.getByText("Beta Channel")).toBeInTheDocument();
    expect(screen.getByText("UCb")).toBeInTheDocument();
    expect(
      screen.getByText("channel_name: Old Beta → Beta Channel"),
    ).toBeInTheDocument();
    expect(screen.getByText("revenue_required: true → false")).toBeInTheDocument();
    // The applied CMS status is on screen: the request omits the form field
    // so the backend default lands, a CREATE row's empty `changes` shows it
    // nowhere else, and it decides whether connector ingest targets the
    // channel at all — so it must be visible before approval.
    expect(
      screen.getByText(/content owner OWNERaaa · CMS status INSIDE_CMS/i),
    ).toBeInTheDocument();
    expect(screen.getByText("g1")).toBeInTheDocument();
    // The Group cell says WHICH group write the key implies. "g1" resolves to
    // no existing group here, so this row MINTS a new SECTOR group — a
    // finance-scope object the bare key would have hidden from the operator
    // until the audit trail (review #184).
    expect(screen.getByText("new group")).toBeInTheDocument();

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

    // Applied step: the counts are labelled as the PLAN the operator approved,
    // never as a re-read of the write. The route answers an apply with its
    // pre-write payload while the backend tallies what it actually wrote into
    // the CHANNEL_IMPORTED audit event, so the bare "CREATE: 1 · UPDATE: 1"
    // line must NOT reappear here unqualified.
    expect(
      screen.getByText("Approved plan — CREATE: 1 · UPDATE: 1"),
    ).toBeInTheDocument();
    expect(screen.queryByText("CREATE: 1 · UPDATE: 1")).not.toBeInTheDocument();
    expect(
      screen.getByText(/durable record of what committed is the CHANNEL_IMPORTED/i),
    ).toBeInTheDocument();
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

  it("refuses BOTH exits while an apply is in flight, then re-enables them", async () => {
    // Hold the apply POST open so the flow stays mid-request for the whole
    // assertion block. The hook exposes no abort, and the backend commits
    // independently of this component: an exit taken here would neither stop
    // nor invalidate the write, so a late success would commit the roster
    // while the operator was shown a cancelled/abandoned import.
    const applyGate = deferredResponse();
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : applyGate.pending,
    );

    const cancelButton = () => screen.getByRole("button", { name: /^cancel$/i });
    const backButton = () => screen.getByRole("button", { name: /^back$/i });

    // Baseline: with nothing in flight, both exits are live.
    expect(cancelButton()).toBeEnabled();
    expect(backButton()).toBeEnabled();

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      // `u` flag for the analyzer's require-unicode-regexp rule, not for the
      // ellipsis: U+2026 is BMP and matches without it. The flag's real effect
      // here is strict-mode parsing of the pattern.
      expect(screen.getByRole("button", { name: /applying…/iu })).toBeInTheDocument(),
    );

    // Mid-apply: both exits are disabled and each says why.
    const inFlightNote = /cannot be aborted/i;
    expect(cancelButton()).toBeDisabled();
    expect(cancelButton().getAttribute("title")).toMatch(inFlightNote);
    expect(backButton()).toBeDisabled();
    expect(backButton().getAttribute("title")).toMatch(inFlightNote);

    // Clicking them anyway changes nothing: the flow is still on Preview and
    // the registry table has not been restored behind a committing write.
    fireEvent.click(cancelButton());
    fireEvent.click(backButton());
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    expect(screen.queryByText("UMS Drama")).not.toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Import upload" })).not.toBeInTheDocument();

    // Once the write lands the flow advances — and on Applied the stepper's
    // Cancel is GONE, not merely re-enabled: the import has committed, so a
    // control labelled Cancel would misstate the outcome. The step's own
    // "Back to Registry" is the exit, and it reloads.
    applyGate.release(jsonResponse(APPLY_RESULT));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /^cancel$/i })).not.toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /back to registry/i }),
    ).toBeInTheDocument();
    expect(importPosts()).toHaveLength(2);
  });

  it("binds the apply to the reviewed plan and re-approves on a 409 divergence", async () => {
    // The route re-plans from CURRENT state, so a row reviewed as CREATE can
    // become an UPDATE over a channel someone else created in the meantime.
    // The apply therefore carries the approved plan's fingerprint, and a
    // backend 409 replaces the preview so approval is re-sought against what
    // would actually be written (review #184).
    const refreshed: ChannelImportResult = {
      ...DRY_RUN_PLAN,
      plan_fingerprint: "plan-refreshed-v2",
      counts: { UPDATE: 1 },
      rows: [
        {
          ...DRY_RUN_PLAN.rows[0],
          outcome: "UPDATE",
          changes: { channel_name: { from: "Someone Else", to: "Alpha Channel" } },
        },
      ],
    };
    let applyCount = 0;
    await runDryRunToPreview((form) => {
      if (form.get("dry_run") === "true") return jsonResponse(DRY_RUN_PLAN);
      applyCount += 1;
      return applyCount === 1
        ? jsonResponse({ detail: refreshed }, 409)
        : jsonResponse(APPLY_RESULT);
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByText(/no longer does what you approved/i)).toBeInTheDocument(),
    );

    // The first apply carried the plan the operator had on screen.
    expect(importPosts()[1].get("expected_plan_fingerprint")).toBe("plan-clean-v1");
    // The dry run itself binds to nothing — there is no prior plan to honour.
    expect(importPosts()[0].get("expected_plan_fingerprint")).toBeNull();

    // The refreshed plan REPLACED the stale one: the row now reads UPDATE and
    // its real diff is on screen, so the operator re-approves reality.
    expect(screen.getByText("UPDATE")).toBeInTheDocument();
    expect(
      screen.getByText("channel_name: Someone Else → Alpha Channel"),
    ).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();

    // Re-approving now sends the REFRESHED fingerprint, not the stale one.
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );
    expect(importPosts()[2].get("expected_plan_fingerprint")).toBe("plan-refreshed-v2");
  });

  it("arms the shell nav latch BEFORE the apply request is dispatched", async () => {
    // The ordering property, not the rendered result. A DOM assertion cannot
    // prove this: fireEvent wraps the click in act(), which flushes effects,
    // so an effect-armed latch also looks armed by the time the test can
    // query the DOM. The real question is whether any moment exists in which
    // the request is running and the latch is not yet held — so this observes
    // the latch AT DISPATCH, inside the fetch mock, before React re-renders.
    const setReason = vi.fn();
    const armedAtDispatch: boolean[] = [];

    routeFetch({
      importPost: (form) => {
        if (form.get("dry_run") === "false") {
          armedAtDispatch.push(setReason.mock.calls.length > 0);
        }
        return jsonResponse(form.get("dry_run") === "true" ? DRY_RUN_PLAN : APPLY_RESULT);
      },
    });
    renderRegistry({ reason: null, setReason });
    await openImport();
    await fillUpload();
    fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
    );

    // A read-only dry-run must NOT have latched anything.
    expect(setReason).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );

    expect(armedAtDispatch).toEqual([true]);
    expect(setReason.mock.calls[0][0]).toMatch(/cannot be aborted/iu);
    // And it is released once the request settles.
    expect(setReason).toHaveBeenLastCalledWith(null);
  });

  it("treats a LOST apply response as indeterminate, not as a failure", async () => {
    // The client raises ApiError only once an HTTP response exists, so a
    // rejected fetch means the POST was dispatched and never answered — the
    // roster may already be committed, audit event and all. Re-arming Apply
    // would submit it a second time (a second unconditional CHANNEL_IMPORTED)
    // and a no-reload Cancel would restore a registry that no longer matches
    // the database.
    const applyGate = deferredResponse();
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : applyGate.pending,
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeDisabled(),
    );
    applyGate.reject(new TypeError("Failed to fetch"));

    await waitFor(() =>
      expect(screen.getByText(/may have committed/i)).toBeInTheDocument(),
    );

    // Retry is refused, and says why.
    const applyButton = screen.getByRole("button", { name: /^apply$/i });
    expect(applyButton).toBeDisabled();
    expect(applyButton.getAttribute("title")).toMatch(/may already have committed/iu);

    // The exit is live again, but it now takes the RELOADING path even though
    // `applied` is still null — the registry must be re-read to settle what
    // actually happened.
    const cancelButton = screen.getByRole("button", { name: /^cancel$/i });
    expect(cancelButton).toBeEnabled();
    fireEvent.click(cancelButton);
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    await waitFor(() => expect(channelGetCount()).toBe(2));
    // Exactly two POSTs: the dry run and the one apply. No blind retry.
    expect(importPosts()).toHaveLength(2);
  });

  it("locks the Upload inputs while the dry run is in flight", async () => {
    // The owner picker staying live during the preview was a real hole: an
    // operator could start a dry run for owner A, switch to B before it
    // resolved, and then Apply against B while the screen still showed A's
    // plan. The backend fingerprint now covers content_owner_id so such an
    // apply is a 409, but the UI should not create the confusing state at all.
    const dryRunGate = deferredResponse();
    routeFetch({ importPost: () => dryRunGate.pending });
    renderRegistry();
    await openImport();
    await fillUpload();

    const panel = uploadPanel();
    expect(within(panel).getByLabelText("Content owner")).toBeEnabled();

    fireEvent.click(within(panel).getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(
        within(uploadPanel()).getByRole("button", { name: /running…/iu }),
      ).toBeInTheDocument(),
    );

    const busyPanel = uploadPanel();
    expect(within(busyPanel).getByLabelText("Content owner")).toBeDisabled();
    expect(within(busyPanel).getByLabelText("Roster CSV")).toBeDisabled();
    expect(within(busyPanel).getByLabelText("Reason (required, audited)")).toBeDisabled();

    // Freed again once the request settles — the lock tracks the request.
    dryRunGate.release(jsonResponse(DRY_RUN_ERRORS));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /^back$/i }));
    expect(within(uploadPanel()).getByLabelText("Content owner")).toBeEnabled();
  });

  it("keeps Cancel available while the READ-ONLY dry run is in flight", async () => {
    // The guard is scoped to the write, not to `busy`. A dry run commits
    // nothing, so abandoning one is safe and the flow's Cancel-at-any-step
    // promise must hold — otherwise a slow or never-settling preview would
    // lock the operator inside the stepper for no safety gain.
    const dryRunGate = deferredResponse();
    routeFetch({ importPost: () => dryRunGate.pending });
    renderRegistry();
    await openImport();
    await fillUpload();

    fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(
        within(uploadPanel()).getByRole("button", { name: /running…/iu }),
      ).toBeInTheDocument(),
    );

    const cancelButton = screen.getByRole("button", { name: /^cancel$/i });
    expect(cancelButton).toBeEnabled();
    expect(cancelButton.getAttribute("title")).toBeNull();

    // And it really works: Cancel restores the table without a refetch.
    fireEvent.click(cancelButton);
    expect(screen.getByText("UMS Drama")).toBeInTheDocument();
    expect(channelGetCount()).toBe(1);

    dryRunGate.release(jsonResponse(DRY_RUN_PLAN));
  });

  it("re-enables the exits when an in-flight apply is DEFINITELY rejected", async () => {
    // The in-flight guard clears in the request's `finally`, not only on
    // success — a rejected apply must not leave the operator locked in.
    const applyGate = deferredResponse();
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : applyGate.pending,
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeDisabled(),
    );

    // 409 with a string detail ESTABLISHES rejection: nothing was written, so
    // both exits free and the roster is safe to change.
    applyGate.release(jsonResponse({ detail: "channel group was archived" }, 409));
    await waitFor(() => expect(screen.getByText("Apply failed")).toBeInTheDocument());
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^back$/i })).toBeEnabled();
  });

  it("frees Cancel but holds Back when an in-flight apply ends INDETERMINATE", async () => {
    // The in-flight guard still clears — the operator is not locked in, Cancel
    // works and forces a registry reload. Back is held for a different reason:
    // it leads to the Upload inputs that "Check whether it landed" re-plans,
    // and changing them would make that check answer about another roster.
    const applyGate = deferredResponse();
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : applyGate.pending,
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeDisabled(),
    );

    // A 5xx does not establish that the write was rejected.
    applyGate.release(jsonResponse({ detail: "boom" }, 500));
    await waitFor(() =>
      expect(screen.getByText("Apply outcome unknown")).toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeEnabled();
    expect(screen.getByRole("button", { name: /^back$/i })).toBeDisabled();
  });

  // The rule is "indeterminate unless rejection is ESTABLISHED". These pin the
  // two cases a naive `instanceof ApiError` check got backwards, plus the
  // control that proves the guard is not simply always-on.
  const applyOutcomeCases: Array<{
    name: string;
    respond: () => Response;
    unknown: boolean;
  }> = [
    // The client raises ApiError carrying the ORIGINAL 2xx status for a
    // malformed success body: the server said OK, so the import almost
    // certainly committed and only the body was unreadable.
    {
      name: "a malformed 2xx body",
      // The JSON content-type matters: it is what puts the client on its
      // strict-parse path, so the unparseable body becomes an ApiError
      // carrying status 200 rather than being handed back as text.
      respond: () =>
        new Response("not json", {
          status: 200,
          headers: { "Content-Type": "application/json" },
        }),
      unknown: true,
    },
    // A gateway failure: the request may have reached the backend and
    // committed, with the response lost on the way home.
    {
      name: "a gateway 502",
      respond: () => jsonResponse({ detail: "bad gateway" }, 502),
      unknown: true,
    },
    // Control: permissions are checked before any write, so a 403 really does
    // establish that nothing was written.
    {
      name: "a 403 permission refusal",
      respond: () => jsonResponse({ detail: "Missing permission" }, 403),
      unknown: false,
    },
    // The import route raises no 404 of its own, so the only 404 that can
    // reach this flow comes from the tenancy resolver, which answers inside
    // ASGI middleware without awaiting the app. The handler never ran, so
    // "may have committed" would lock out retries over a request that
    // provably wrote nothing.
    {
      name: "a tenancy 404",
      respond: () => jsonResponse({ detail: "Tenant 'ums' not found" }, 404),
      unknown: false,
    },
  ];

  for (const testCase of applyOutcomeCases) {
    const verdict = testCase.unknown ? "INDETERMINATE" : "a definite failure";
    it(`treats ${testCase.name} as ${verdict}`, async () => {
      await runDryRunToPreview((form) =>
        form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : testCase.respond(),
      );

      fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
      const title = testCase.unknown ? "Apply outcome unknown" : "Apply failed";
      await waitFor(() => expect(screen.getByText(title)).toBeInTheDocument());

      // Indeterminate blocks a blind retry (it would append a second
      // unconditional CHANNEL_IMPORTED); a definite refusal stays retryable.
      const applyButton = screen.getByRole("button", { name: /^apply$/i });
      if (testCase.unknown) {
        expect(screen.getByText(/may have committed/i)).toBeInTheDocument();
        expect(applyButton).toBeDisabled();
      } else {
        expect(screen.queryByText(/may have committed/i)).not.toBeInTheDocument();
        expect(applyButton).toBeEnabled();
      }
    });
  }

  it("ignores a rejection detail that is not a whole plan", async () => {
    // The refreshed-plan guard checks BOTH halves the preview renders. A
    // rows-only payload would otherwise pass, replace the preview, and crash
    // CountsStrip on Object.entries(undefined) — the step it was meant to
    // repair. It must fall through to the ordinary banner instead.
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true"
        ? jsonResponse(DRY_RUN_PLAN)
        : jsonResponse({ detail: { rows: [] } }, 409),
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(screen.getByText("Apply failed")).toBeInTheDocument());

    // The original preview is intact — not replaced by a half-formed payload.
    // (Unlabelled here: only the Applied step prefixes its counts strip.)
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    expect(screen.getByText("CREATE: 1 · UPDATE: 1")).toBeInTheDocument();
  });

  it("shows the pre-state 409 verbatim and keeps the preview to go back from", async () => {
    // Sending expected_plan_fingerprint opts this flow in to the backend's
    // strict pre-state check, whose 409 detail is a STRING (which row moved
    // and to what) rather than a refreshed plan. That message names the
    // channel and tells the operator what to do, so it must reach them word
    // for word instead of collapsing to "The import request failed".
    const detail =
      "channel UCB6sc84dcg6VQGB_d89sx2g changed during the import: the preview " +
      "showed channel_name 'Alpha Channel' -> 'Alpha News', but the stored value " +
      "is now 'Someone Else'; re-run the preview and review the change";
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true"
        ? jsonResponse(DRY_RUN_PLAN)
        : jsonResponse({ detail }, 409),
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(screen.getByText(detail)).toBeInTheDocument());

    // Rejected, not indeterminate: 409 is a definite refusal, so the operator
    // is offered Apply again rather than the "outcome unknown" dead end.
    expect(screen.getByText("Apply failed")).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /^back$/i })).toBeEnabled();
  });

  /** Drive to Preview, then fail the apply in a way that is INDETERMINATE. */
  const reachIndeterminate = async (afterApply: () => Response) => {
    let applyCount = 0;
    await runDryRunToPreview((form) => {
      if (form.get("dry_run") === "true") {
        return applyCount === 0 ? jsonResponse(DRY_RUN_PLAN) : afterApply();
      }
      applyCount += 1;
      // A gateway 502: the request may have reached the backend and committed.
      return jsonResponse({ detail: "bad gateway" }, 502);
    });
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByText("Apply outcome unknown")).toBeInTheDocument(),
    );
  };

  it("settles an indeterminate apply by re-planning: all-UNCHANGED means it landed", async () => {
    // The way OUT of "unknown", using the endpoint the flow already has. The
    // apply is ONE all-or-nothing transaction, so an all-UNCHANGED re-plan is
    // decisive: the roster IS the registry, therefore the write committed.
    // NO group keys: `outcome` is computed from channel inventory alone, so
    // all-UNCHANGED is only proof for a roster that owes no group effects.
    const settled: ChannelImportResult = {
      ...DRY_RUN_PLAN,
      plan_fingerprint: "plan-settled",
      counts: { UNCHANGED: 2 },
      rows: DRY_RUN_PLAN.rows.map((row) => ({
        ...row,
        outcome: "UNCHANGED",
        changes: {},
        group_id: null,
        group_action: null,
      })),
    };
    await reachIndeterminate(() => jsonResponse(settled));

    fireEvent.click(screen.getByRole("button", { name: /check whether it landed/iu }));

    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );
    // The reconciliation used the DRY RUN, so nothing was written to find out.
    expect(importPosts()[2].get("dry_run")).toBe("true");
  });

  it("will not call a group-bearing roster applied on an all-UNCHANGED re-plan", async () => {
    // The planner computes `outcome` from channel inventory and never loads
    // memberships, so a roster whose channels already match can still owe the
    // group attachments the lost apply was supposed to make. Declaring that
    // "applied" would report a half-written import as done.
    const channelsMatch: ChannelImportResult = {
      ...DRY_RUN_PLAN,
      plan_fingerprint: "plan-groups-pending",
      counts: { UNCHANGED: 2 },
      // row[1] keeps its group_id "g1" — the effect that cannot be verified.
      rows: DRY_RUN_PLAN.rows.map((row) => ({ ...row, outcome: "UNCHANGED", changes: {} })),
    };
    await reachIndeterminate(() => jsonResponse(channelsMatch));

    fireEvent.click(screen.getByRole("button", { name: /check whether it landed/iu }));

    await waitFor(() =>
      expect(screen.getByText(/cannot tell whether those memberships/i)).toBeInTheDocument(),
    );
    // Explicitly NOT applied, and not falsely reported as "did not commit".
    expect(screen.queryByRole("group", { name: "Import applied" })).not.toBeInTheDocument();
    expect(screen.queryByText(/still not committed/i)).not.toBeInTheDocument();
  });

  it("freezes the roster inputs while an apply's outcome is unknown", async () => {
    // Upload owns the file, owner and reason that "Check whether it landed"
    // re-plans. Backing out to change them would let the check answer about a
    // DIFFERENT roster and declare the earlier import applied on that evidence.
    await reachIndeterminate(() => jsonResponse(DRY_RUN_PLAN));

    const back = screen.getByRole("button", { name: /^back$/i });
    expect(back).toBeDisabled();
    expect(back.getAttribute("title")).toMatch(/would make that check answer about a different/iu);
    // Cancel stays open — it forces a registry reload rather than claiming
    // anything is known, so the operator is never trapped.
    expect(screen.getByRole("button", { name: /^cancel$/i })).toBeEnabled();
  });

  it("keeps an indeterminate apply unknown when the re-plan still shows changes", async () => {
    // The other half, and the reason the control is repeatable: the original
    // POST may still be running. A re-plan that still shows work pending does
    // NOT prove the import failed, so the flow must not claim it did.
    await reachIndeterminate(() => jsonResponse(DRY_RUN_PLAN));

    fireEvent.click(screen.getByRole("button", { name: /check whether it landed/iu }));

    await waitFor(() => expect(screen.getByText(/still not committed/i)).toBeInTheDocument());
    // Still on Preview, still offering another check — not a false verdict.
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: /check whether it landed/iu }),
    ).toBeEnabled();
  });

  it("refuses a refreshed plan whose rows are not renderable rows", async () => {
    // Array.isArray(rows) alone let `[{}]` through, and ChangesCell then threw
    // on Object.entries(undefined) — crashing the very step the refreshed plan
    // was meant to repair. Fail closed to the banner instead.
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true"
        ? jsonResponse(DRY_RUN_PLAN)
        : jsonResponse(
            { detail: { rows: [{}], counts: { UPDATE: 1 }, plan_fingerprint: "x" } },
            409,
          ),
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(screen.getByText("Apply failed")).toBeInTheDocument());

    // The approved preview still stands, and still renders.
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    expect(screen.getByText("CREATE: 1 · UPDATE: 1")).toBeInTheDocument();
  });

  it("refuses a refreshed plan with no fingerprint rather than unbinding the apply", async () => {
    // The dangerous shape: rows and counts present, plan_fingerprint missing.
    // Accepting it would replace the preview with a plan carrying no digest,
    // and the next Apply would send NO expected_plan_fingerprint — silently
    // turning the operator's bound apply into an unbound, file-wins one with
    // the backend's pre-state guard switched off. Fail closed instead.
    const noFingerprint = { ...DRY_RUN_PLAN } as Record<string, unknown>;
    delete noFingerprint.plan_fingerprint;
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true"
        ? jsonResponse(DRY_RUN_PLAN)
        : jsonResponse({ detail: noFingerprint }, 409),
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(screen.getByText("Apply failed")).toBeInTheDocument());

    // The approved preview — and its fingerprint — survive untouched.
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    expect(screen.getByText("CREATE: 1 · UPDATE: 1")).toBeInTheDocument();

    // The decisive assertion: a retry is still BOUND to the plan on screen.
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(importPosts()).toHaveLength(3));
    expect(importPosts()[2].get("expected_plan_fingerprint")).toBe("plan-clean-v1");
  });

  it("discloses the revenue source status the write derives", async () => {
    // The write re-classifies revenue_source_status whenever revenue_required
    // flips, and that status drives missing_official_revenue and the
    // registry's recommended action. `changes` never carries it — it holds the
    // operator's own field edits — so without this cell the operator approves
    // a finance-source mutation nothing on screen mentions.
    const plan: ChannelImportResult = {
      ...DRY_RUN_PLAN,
      rows: [
        // A flip OFF: a proven official classification is being replaced.
        {
          ...DRY_RUN_PLAN.rows[1],
          revenue_source_status: {
            from: "OFFICIAL_CMS_REVENUE",
            to: "PERFORMANCE_ONLY",
          },
        },
        // A CREATE has no prior status, so only the value it is born with.
        {
          ...DRY_RUN_PLAN.rows[0],
          revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
        },
      ],
    };
    await runDryRunToPreview(() => jsonResponse(plan));

    expect(
      screen.getByText("source: OFFICIAL_CMS_REVENUE → PERFORMANCE_ONLY"),
    ).toBeInTheDocument();
    expect(screen.getByText("source: MISSING_REVENUE_SOURCE")).toBeInTheDocument();
  });

  it("says nothing about the source status when the write leaves it alone", async () => {
    // Anti-noise: the status is re-derived ONLY when revenue_required flips,
    // so a roster refreshing names must not read as a finance reclassification.
    await runDryRunToPreview(() => jsonResponse(DRY_RUN_PLAN));

    expect(screen.queryByText(/^source:/)).not.toBeInTheDocument();
  });

  it("distinguishes a group JOIN from a group CREATE, and claims neither without one", async () => {
    // Same key, opposite effects: the operator must be able to tell "this
    // adds a channel to a group you already own" from "this mints a new
    // finance-scope group", and a row with no key must promise nothing.
    const joinPlan: ChannelImportResult = {
      ...DRY_RUN_PLAN,
      rows: [
        { ...DRY_RUN_PLAN.rows[1], group_id: "g-existing", group_action: "JOIN" },
        { ...DRY_RUN_PLAN.rows[0], row_number: 3, group_id: null, group_action: null },
      ],
    };
    await runDryRunToPreview(() => jsonResponse(joinPlan));

    expect(screen.getByText("g-existing")).toBeInTheDocument();
    expect(screen.getByText("adds to existing")).toBeInTheDocument();
    expect(screen.queryByText("new group")).not.toBeInTheDocument();

    // The keyless row's Group cell is a bare dash — no effect claimed.
    const keylessRow = screen.getByText("Alpha Channel").closest("tr");
    expect(keylessRow).not.toBeNull();
    const groupCell = within(keylessRow as HTMLElement).getAllByRole("cell")[4];
    expect(groupCell.textContent).toBe("—");
  });

  it("falls back to the bare group key when the wire sends an unknown action", async () => {
    // Prototype-chain + unknown-literal hardening, matching the outcome chip:
    // a value the UI does not recognise must degrade to the key alone rather
    // than render a wrong claim about a group write.
    const oddPlan = {
      ...DRY_RUN_PLAN,
      rows: [{ ...DRY_RUN_PLAN.rows[1], group_id: "g9", group_action: "toString" }],
    };
    await runDryRunToPreview(() => jsonResponse(oddPlan));

    const row = screen.getByText("g9").closest("tr");
    expect(row).not.toBeNull();
    const groupCell = within(row as HTMLElement).getAllByRole("cell")[4];
    expect(groupCell.textContent).toBe("g9");
  });

  it("renders a muted dash for each half an ERROR row's channel identity lacks", async () => {
    await runDryRunToPreview((form) =>
      jsonResponse(form.get("dry_run") === "true" ? DRY_RUN_ERRORS : APPLY_RESULT),
    );

    // The ERROR row carries neither channel_name nor youtube_channel_id, so
    // BOTH lines of its channel cell fall back to a dash — the name/id
    // fallback chain the preview no longer uses cannot hide one behind the
    // other.
    const errorRow = screen.getByText("missing youtube_channel_id").closest("tr");
    expect(errorRow).not.toBeNull();
    const channelCell = within(errorRow as HTMLElement).getAllByRole("cell")[1];
    expect(channelCell.textContent).toBe("——");
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
