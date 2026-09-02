import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import RegistryView from "@/components/srcc/views/RegistryView";
import type {
  ChannelImportResult,
  ChannelRegistryEntry,
  ContentOwnersResponse,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";
import { WriteInFlightProvider } from "@/contexts/WriteInFlightContext";
import {
  UNSCOPED_IMPORT_SCOPE,
  importScopeFor,
} from "@/contexts/UnsettledImportContext";
import {
  importPlanJsonResponse as jsonResponse,
  withDisplayDigest,
} from "../../../helpers/displayDigestFixtures";

// RegistryImportFlow is exercised THROUGH RegistryView (the GroupsView.test.tsx
// idiom for GroupsSyncFlow): the capability gate, the table swap, and the
// done-refetch are view wiring, so the flow is tested where it actually runs.
// Capability gating itself (hidden without canImportChannels, shown with) is
// pinned in RegistryView.test.tsx alongside the other gating tests.

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
  // The unsettled-import flag mirrors into localStorage ON PURPOSE, so it
  // outlives a browser reload. That makes it leak between tests unless it is
  // cleared here — the leak is the feature working, not a bug to design away.
  globalThis.localStorage.clear();
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
  "youtube_channel_id,channel_name\nUCaaaaaaaaaaaaaaaaaaaaaa,Alpha Channel\nUCbbbbbbbbbbbbbbbbbbbbbb,Beta Channel\n";

const rosterFile = () => {
  return new File([CSV_TEXT], "roster.csv", { type: "text/csv" });
};

// Clean dry-run plan: a CREATE (empty diff by design) + an UPDATE carrying a
// field-level diff and a group effect. UNCHANGED:0 proves zero counts hide.
const DRY_RUN_PLAN: ChannelImportResult = withDisplayDigest({
  dry_run: true,
  content_owner_id: "OWNERaaa",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
  plan_fingerprint: "plan-clean-v1",
  rows: [
    {
      row_number: 1,
      youtube_channel_id: "UCaaaaaaaaaaaaaaaaaaaaaa",
      outcome: "CREATE",
      channel_name: "Alpha Channel",
      group_id: null,
      group_action: null,
      revenue_required: true,
      // A CREATE is ALWAYS born with a classification, so the planner emits
      // {from: null, to: ...} — never null. A null here would be a shape the
      // backend cannot produce (review #184).
      revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
      changes: {},
      reason: null,
    },
    {
      row_number: 2,
      youtube_channel_id: "UCbbbbbbbbbbbbbbbbbbbbbb",
      outcome: "UPDATE",
      channel_name: "Beta Channel",
      group_id: "g1",
      group_action: "CREATE",
      revenue_required: false,
      // This row FLIPS revenue_required (see `changes` below), and the write
      // re-derives the source status on exactly that flip.
      revenue_source_status: { from: "MISSING_REVENUE_SOURCE", to: "PERFORMANCE_ONLY" },
      changes: {
        channel_name: { from: "Old Beta", to: "Beta Channel" },
        revenue_required: { from: true, to: false },
      },
      reason: null,
    },
  ],
});

// The applied echo of the same plan (identical shape, dry_run:false).
const APPLY_RESULT: ChannelImportResult = { ...DRY_RUN_PLAN, dry_run: false };

// A plan holding an ERROR row: Apply must be blocked client-side because the
// API would 422 the whole file (all-or-nothing).
const DRY_RUN_ERRORS: ChannelImportResult = withDisplayDigest({
  dry_run: true,
  content_owner_id: "OWNERaaa",
  cms_status: "INSIDE_CMS",
  counts: { CREATE: 1, UPDATE: 0, UNCHANGED: 0, ERROR: 1 },
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
});

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

const DEFAULT_WRITE_LATCH = { reason: null, setReason: () => undefined };

/** The tree under test, separately from rendering it, so a test can re-render
 * the same registry with one prop changed (the scope-settling test does). */
const registryTree = (
  writeLatch: { reason: string | null; setReason: (reason: string | null) => void },
  // Defaults to the capability-poor operator: the seeded import roles hold
  // MANAGE_CHANNELS + MANAGE_GROUPS but NOT VIEW_AUDIT_LOG.
  canViewAudit: boolean,
  importScopeSettled: boolean,
  importScope?: string,
) => {
  return (
    <TenantProvider initialSlug="ums">
      <WriteInFlightProvider value={writeLatch}>
        <RegistryView
          canManageRegistry
          canImportChannels
          canViewFinance
          canViewAudit={canViewAudit}
          importScopeSettled={importScopeSettled}
          importScope={importScope}
        />
      </WriteInFlightProvider>
    </TenantProvider>
  );
};

const renderRegistry = (
  writeLatch: {
    reason: string | null;
    setReason: (reason: string | null) => void;
  } = DEFAULT_WRITE_LATCH,
  canViewAudit = false,
  importScopeSettled = true,
) => {
  return render(registryTree(writeLatch, canViewAudit, importScopeSettled));
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
/** The render result of the most recent runDryRunToPreview, for the one test
 * that needs to re-render the same tree with a prop changed. */
let previewView: ReturnType<typeof render> | null = null;

const runDryRunToPreview = async (
  // Same widening as RouteOverrides.importPost: the in-flight guard tests hand
  // in a responder whose APPLY leg is a pending promise.
  importPost: (form: FormData) => Response | Promise<Response>,
  importScopeSettled = true,
): Promise<File> => {
  routeFetch({ importPost });
  const view = renderRegistry(DEFAULT_WRITE_LATCH, false, importScopeSettled);
  await openImport();
  const file = await fillUpload();
  fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
  await waitFor(() =>
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
  );
  previewView = view;
  return file;
};


describe("RegistryImportFlow stepper (through RegistryView)", () => {
  it("runs the happy path: upload -> dry-run preview -> apply -> applied counts + refetch", async () => {
    const file = await runDryRunToPreview(cleanImport);

    // The dry-run POST is multipart FormData with exactly the five wire
    // fields. cms_status is sent explicitly (same value as the route default)
    // so the echoed target can be checked against a value the client named.
    expect(importPosts()).toHaveLength(1);
    const dryRunForm = importPosts()[0];
    expect([...dryRunForm.keys()].sort()).toEqual([
      "cms_status",
      "content_owner_id",
      "dry_run",
      "file",
      "reason",
    ]);
    expect(dryRunForm.get("cms_status")).toBe("INSIDE_CMS");
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
    expect(screen.getByText("UCaaaaaaaaaaaaaaaaaaaaaa")).toBeInTheDocument();
    expect(screen.getByText("Beta Channel")).toBeInTheDocument();
    expect(screen.getByText("UCbbbbbbbbbbbbbbbbbbbbbb")).toBeInTheDocument();
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
    const refreshed: ChannelImportResult = withDisplayDigest({
      ...DRY_RUN_PLAN,
      plan_fingerprint: "plan-refreshed-v2",
      counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
      rows: [
        {
          ...DRY_RUN_PLAN.rows[0],
          outcome: "UPDATE",
          // An UPDATE has a prior status, so `from` cannot be null the way a
          // CREATE's is — and this row is spread from the CREATE fixture.
          revenue_source_status: null,
          changes: { channel_name: { from: "Someone Else", to: "Alpha Channel" } },
        },
      ],
    });
    let applyCount = 0;
    await runDryRunToPreview((form) => {
      if (form.get("dry_run") === "true") return jsonResponse(DRY_RUN_PLAN);
      applyCount += 1;
      // The second apply echoes the REFRESHED plan, because that is what the
      // route does: it returns the payload whose fingerprint it compared
      // against, so a success always carries the digest the request was bound
      // to. Answering with APPLY_RESULT here returned `plan-clean-v1` for a
      // request bound to `plan-refreshed-v2` — a shape the backend cannot
      // produce, and one the hook now rejects (review #184, codex P2).
      return applyCount === 1
        ? jsonResponse({ detail: refreshed }, 409)
        : jsonResponse({ ...refreshed, dry_run: false });
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByText(/no longer does what you approved/i)).toBeInTheDocument(),
    );

    // The first apply carried the plan the operator had on screen — BOTH
    // tokens, from the same approved plan object (review #184, C1).
    expect(importPosts()[1].get("expected_plan_fingerprint")).toBe("plan-clean-v1");
    expect(importPosts()[1].get("expected_display_digest")).toBe(DRY_RUN_PLAN.display_digest);
    // The dry run itself binds to nothing — there is no prior plan to honour.
    expect(importPosts()[0].get("expected_plan_fingerprint")).toBeNull();
    expect(importPosts()[0].get("expected_display_digest")).toBeNull();

    // The refreshed plan REPLACED the stale one: the row now reads UPDATE and
    // its real diff is on screen, so the operator re-approves reality.
    expect(screen.getByText("UPDATE")).toBeInTheDocument();
    expect(
      screen.getByText("channel_name: Someone Else → Alpha Channel"),
    ).toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();

    // Re-approving now sends the REFRESHED tokens, not the stale ones.
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );
    expect(importPosts()[2].get("expected_plan_fingerprint")).toBe("plan-refreshed-v2");
    expect(importPosts()[2].get("expected_display_digest")).toBe(refreshed.display_digest);
  });

  it("refuses a refreshed plan describing a DIFFERENT owner", async () => {
    // The 409/422 detail REPLACES the preview and becomes what the operator
    // re-approves — and the next Apply still sends the captured owner. So a
    // refreshed plan for another owner would be reviewed against one target
    // and applied against another. The 2xx path already refused that; this
    // path is where it matters more (review #184, self-review).
    const foreign: ChannelImportResult = withDisplayDigest({
      ...DRY_RUN_PLAN,
      content_owner_id: "OWNERzzz",
      plan_fingerprint: "plan-foreign-v2",
    });
    await runDryRunToPreview((form) => {
      if (form.get("dry_run") === "true") return jsonResponse(DRY_RUN_PLAN);
      return jsonResponse({ detail: foreign }, 409);
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

    // Wait for a POSITIVE signal first. A bare negative assertion passes on the
    // first tick — before the click has dispatched anything — so it would go
    // green even if the apply never ran, hiding a regression in exactly the
    // fail-closed behaviour this test exists to pin (review #184, qodo).
    await waitFor(() => expect(screen.getByText("Apply failed")).toBeInTheDocument());
    // And the apply really did settle: the button is live again for a retry.
    expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled();

    // Fail closed: the ORDINARY refusal banner, not the re-approve copy, so the
    // operator keeps the plan they actually reviewed.
    expect(
      screen.queryByText(/no longer does what you approved/i),
    ).not.toBeInTheDocument();
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();
    // The foreign plan never reached the screen: its owner is absent, and the
    // reviewed owner is still the one on display.
    expect(screen.queryByText(/OWNERzzz/)).not.toBeInTheDocument();
    expect(screen.getByText(/OWNERaaa/)).toBeInTheDocument();
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

  it("releases the nav latch when apply-id generation itself throws", async () => {
    // Structural guard for the window between `arm()` and the try whose
    // `finally` owns the release: a setup throw there — modeled here as the
    // apply id's entropy source failing — dispatches no request, so nothing
    // but that `finally` can ever free the shell's navigation. Before the fix
    // the latch stayed armed until a manual reload.
    const setReason = vi.fn();
    routeFetch({
      importPost: (form) =>
        jsonResponse(form.get("dry_run") === "true" ? DRY_RUN_PLAN : APPLY_RESULT),
    });
    renderRegistry({ reason: null, setReason });
    await openImport();
    await fillUpload();
    fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
    );

    const uuidSpy = vi.spyOn(globalThis.crypto, "randomUUID").mockImplementation(() => {
      throw new Error("entropy unavailable");
    });
    try {
      fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

      // Armed by the click, then freed by the same handler's `finally`.
      await waitFor(() => expect(setReason).toHaveBeenLastCalledWith(null));
      expect(setReason.mock.calls[0][0]).toMatch(/cannot be aborted/iu);

      // A pre-dispatch throw is a DEFINITE failure: no request left the
      // browser, so nothing could have committed. The failure surfaces, the
      // exits recover, and Apply itself re-enables for a safe retry instead
      // of the reload-only indeterminate lockout.
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeEnabled();
      expect(
        await screen.findByText(/The import request failed\./iu),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled();
    } finally {
      uuidSpy.mockRestore();
    }
  });

  it("treats a LATE pre-fetch setup throw after admission as a definite non-dispatch", async () => {
    // The dispatch verdict belongs to the request, not to a flag flipped
    // before it: `onDispatched` fires inside the API client immediately
    // before `fetch`, so a throw from any LATER setup step — here FormData
    // assembly inside the import hook, after admission already recorded the
    // pending apply — still counts as "nothing left the browser". Before the
    // fix the flow marked itself dispatched before calling the hook, so this
    // exact failure was misclassified as a possibly-committed write and
    // locked Apply behind the reload-only indeterminate contract.
    await runDryRunToPreview(cleanImport);

    const appendSpy = vi
      .spyOn(globalThis.FormData.prototype, "append")
      .mockImplementation(() => {
        throw new Error("multipart assembly failed");
      });
    try {
      fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

      // Definite pre-dispatch treatment: the admission is retired, the
      // failure surfaces as a retryable error, and every exit recovers.
      expect(
        await screen.findByText(/The import request failed\./iu),
      ).toBeInTheDocument();
      expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled();
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeEnabled();

      // And the proof of non-dispatch: fetch saw only the dry run. No apply
      // POST was ever constructed into a request.
      expect(importPosts()).toHaveLength(1);
      expect(importPosts()[0].get("dry_run")).toBe("true");
    } finally {
      appendSpy.mockRestore();
    }
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

  it("carries an unsettled import out of the flow and blocks a duplicate", async () => {
    // The exit reloads, but that GET races the POST it is trying to observe,
    // so the restored table can be PRE-write while looking authoritative. The
    // harm is the next step: reopening the importer off that table and
    // submitting the same roster again, appending a second unconditional
    // CHANNEL_IMPORTED. Leaving the flow must therefore keep saying the
    // outcome is unknown — and must not re-arm "Import CSV" until the operator
    // says they have checked. Blocking the exit instead is not an option: a
    // group-bearing roster never auto-settles, so that operator would be
    // trapped with no way out at all.
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

    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    // The warning outlives the flow that raised it.
    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(/may still be committing/i);
    // RegistryView here has no canViewAudit, which is the seeded
    // revenue_operations_admin / data_steward case: MANAGE_CHANNELS +
    // MANAGE_GROUPS does not imply VIEW_AUDIT_LOG. The notice must not send
    // them to a view that will refuse them (review #184, codex P2).
    expect(notice).toHaveTextContent(/cannot open the audit trail/i);
    expect(notice).toHaveTextContent(/re-open import csv and preview/i);

    // The importer stays OPENABLE on purpose — re-previewing the same roster
    // is the operator's reconciliation surface, and for a role without audit
    // access it is the only one they have. The duplicate is blocked a layer
    // down instead, which the "ANOTHER tab" test pins directly.
    expect(screen.getByRole("button", { name: /import csv/i })).toBeEnabled();

    // Reloading does NOT clear it — a fresh GET still cannot prove the write
    // landed, so only an explicit acknowledgement retires the notice.
    fireEvent.click(within(notice).getByRole("button", { name: /reload registry/i }));
    await waitFor(() => expect(channelGetCount()).toBe(3));
    expect(screen.getByRole("status")).toBeInTheDocument();

    fireEvent.click(
      within(screen.getByRole("status")).getByRole("button", {
        name: /accounted for/i,
      }),
    );
    await waitFor(() =>
      expect(screen.queryByRole("status")).not.toBeInTheDocument(),
    );

    // Still exactly two POSTs — nothing here re-submitted the roster.
    expect(importPosts()).toHaveLength(2);
  });

  it("does not blame another tab for THIS tab's own apply", async () => {
    // This tab raises the shared unsettled record the moment it admits its own
    // apply, so a naive read of that flag made the disabled Apply button say
    // "Another tab has an import whose outcome is not settled yet" about the
    // request this very tab was running (review #184, qodo).
    const applyGate = deferredResponse();
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : applyGate.pending,
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    const applyButton = await screen.findByRole("button", { name: /applying…/iu });
    expect(applyButton).toBeDisabled();
    expect(applyButton.getAttribute("title")).toBeNull();

    applyGate.release(jsonResponse(APPLY_RESULT));
    await screen.findByRole("group", { name: "Import applied" });
  });

  it("says Applying… only for an APPLY, never for the read-only re-plan", async () => {
    // `busy` covers both requests. Labelling the reconciliation dry run
    // "Applying…" tells the operator a write is running while nothing is being
    // written — during an indeterminate state, of all moments.
    const applyGate = deferredResponse();
    const replanGate = deferredResponse();
    let dryRuns = 0;
    await runDryRunToPreview((form) => {
      if (form.get("dry_run") !== "true") {
        return applyGate.pending;
      }
      dryRuns += 1;
      return dryRuns === 1 ? jsonResponse(DRY_RUN_PLAN) : replanGate.pending;
    });

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    applyGate.reject(new TypeError("Failed to fetch"));
    await waitFor(() =>
      expect(screen.getByText(/may have committed/i)).toBeInTheDocument(),
    );

    // Re-plan in flight: busy is true, but no write is running.
    fireEvent.click(screen.getByRole("button", { name: /check whether it landed/iu }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /checking…|check whether/iu })).toBeDisabled(),
    );
    expect(screen.queryByRole("button", { name: /applying…/iu })).not.toBeInTheDocument();

    replanGate.release(jsonResponse(DRY_RUN_PLAN));
    await waitFor(() =>
      expect(screen.getByText(/does not match this roster/i)).toBeInTheDocument(),
    );
  });

  it("refuses Apply while ANOTHER tab has an apply of unknown outcome", async () => {
    // Both tabs already hold a preview, so the disabled "Import CSV" control
    // never applied to this one — it was already inside the flow when the
    // other tab dispatched. Its own `indeterminate` knows nothing about that
    // request, so without consulting the shared store both POSTs run, and
    // whichever settles first clears the other's protection (review #184,
    // codex P1).
    await runDryRunToPreview(() => jsonResponse(DRY_RUN_PLAN));
    expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled();

    // The other tab dispatches. jsdom shares one localStorage but does not
    // synthesise the cross-document event, so raise it explicitly — this is
    // exactly what a real second tab's write delivers here.
    // The records are scoped; a standalone RegistryView uses the unscoped
    // bucket, so the other tab's key has to live in the same one to be seen.
    const otherTabKey = `ums.unsettledChannelImport.${UNSCOPED_IMPORT_SCOPE}.apply-from-another-tab`;
    globalThis.localStorage.setItem(otherTabKey, "1");
    fireEvent(globalThis.window, new StorageEvent("storage", { key: otherTabKey }));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled(),
    );
    // Only the dry run went out; this tab's apply never dispatched.
    expect(importPosts()).toHaveLength(1);
  });

  it("withholds Apply until the import SCOPE has settled", async () => {
    // The scope is built from the tenant id, and a session whose body carries
    // no tenant gets that id from /tenants/me AFTER the shell has rendered. An
    // apply admitted in that window files its pending record under the
    // missing-tenant namespace, and the very next render moves the namespace —
    // so the record is orphaned: this tab's own guard no longer sees it, and
    // neither does another tab (review #184, codex P2).
    await runDryRunToPreview(() => jsonResponse(DRY_RUN_PLAN), false);

    const applyButton = screen.getByRole("button", { name: /^apply$/i });
    expect(applyButton).toBeDisabled();
    // And it says WHY, rather than presenting a dead control.
    expect(applyButton).toHaveAttribute("title", expect.stringMatching(/which workspace/i));
    // The read-only dry run went out; no apply did.
    expect(importPosts()).toHaveLength(1);
  });

  it("releases Apply once the scope settles, rather than wedging", async () => {
    // The complement, and the reason a FAILED tenant resolution counts as
    // settled upstream: a guard that never releases would lock an operator out
    // of importing entirely, which is worse than the degraded namespace it was
    // protecting.
    await runDryRunToPreview(() => jsonResponse(DRY_RUN_PLAN), false);
    expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();

    previewView?.rerender(registryTree(DEFAULT_WRITE_LATCH, false, true));

    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled(),
    );
  });

  it("reports a NON-DISPATCH when admission itself throws", async () => {
    // navigator.locks.request() rejects before ever invoking its callback in a
    // restricted or opaque-origin document (SecurityError) and in a detached
    // one. No key is written and no request is sent — but the rejection used to
    // reach handleApplyFailure, where a non-ApiError classifies as
    // INDETERMINATE: the flow would say the import may have committed, freeze
    // Back, and offer to reconcile a write that never left the browser
    // (review #184, codex P2).
    const locks = {
      request: () => Promise.reject(new DOMException("denied", "SecurityError")),
    };
    Object.defineProperty(globalThis.navigator, "locks", {
      value: locks,
      configurable: true,
    });
    try {
      routeFetch({ importPost: () => jsonResponse(DRY_RUN_PLAN) });
      render(registryTree(DEFAULT_WRITE_LATCH, false, true));
      await openImport();
      await fillUpload();
      fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
      await waitFor(() =>
        expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
      );

      fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));

      // Told plainly that nothing was sent...
      await waitFor(() => expect(screen.getByText(/nothing was sent/i)).toBeInTheDocument());
      // ...and NOT told the opposite. No indeterminate copy, no reconcile offer.
      expect(screen.queryByText(/may still be committing/i)).not.toBeInTheDocument();
      expect(screen.queryByRole("button", { name: /check the registry/i })).toBeNull();
      // The dry run is the only POST: admission failed before the apply.
      expect(importPosts()).toHaveLength(1);
      // Still on Preview, and retryable rather than wedged.
      expect(screen.getByRole("button", { name: /^apply$/i })).toBeEnabled();
    } finally {
      Reflect.deleteProperty(globalThis.navigator, "locks");
    }
  });

  it("settles an apply whose SCOPE migrated while it was in flight", async () => {
    // A tenant that resolves after a failed bootstrap migrates the pending
    // record to the new scope. The in-flight continuation used to hold a
    // settleApply bound to the PRE-migration scope, so a clean 2xx removed the
    // now-empty old key and left the migrated one behind — warning about an
    // import that had demonstrably completed, and blocking the next apply until
    // the operator manually acknowledged it (review #184, codex P2).
    const before = importScopeFor(null, "user-1");
    const after = importScopeFor("tenant-1", "user-1");
    const apply = deferredResponse();
    routeFetch({
      importPost: (form) =>
        form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : apply.pending,
    });
    const view = render(registryTree(DEFAULT_WRITE_LATCH, false, true, before));
    await openImport();
    await fillUpload();
    fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(importPosts()).toHaveLength(2));

    // The tenant resolves mid-apply: same operator, new namespace.
    view.rerender(registryTree(DEFAULT_WRITE_LATCH, false, true, after));
    await act(async () => {
      apply.release(jsonResponse(APPLY_RESULT));
      await apply.pending;
    });

    // The write landed and the flow says so — with no unsettled record left in
    // either namespace.
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /back to registry/i }));
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    expect(screen.queryByText(/may still be committing/i)).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /import csv/i })).toBeEnabled();
  });

  it("settles an apply when the scope changes WITHOUT an adoption", async () => {
    // The mirror of the migration case, and it fails the other way. A scope
    // change that is not a same-user tenant resolution does NOT adopt — a
    // different operator, or a tenant switch — so the record stays where it
    // was recorded, and settling only through the latest binding is a no-op
    // there: a completed apply would leave a pending key behind, warning about
    // a write that demonstrably landed (review #184, qodo).
    const before = importScopeFor("tenant-1", "user-1");
    const afterOtherUser = importScopeFor("tenant-1", "user-2");
    const apply = deferredResponse();
    routeFetch({
      importPost: (form) =>
        form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : apply.pending,
    });
    const view = render(registryTree(DEFAULT_WRITE_LATCH, false, true, before));
    await openImport();
    await fillUpload();
    fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(importPosts()).toHaveLength(2));

    // A non-adopting scope change lands while the apply is outstanding.
    view.rerender(registryTree(DEFAULT_WRITE_LATCH, false, true, afterOtherUser));
    await act(async () => {
      apply.release(jsonResponse(APPLY_RESULT));
      await apply.pending;
    });

    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import applied" })).toBeInTheDocument(),
    );
    // Nothing pending survives in the scope that RECORDED it.
    expect(
      Object.keys(globalThis.localStorage).filter((key) => key.includes(before)),
    ).toEqual([]);
  });

  it("stays acknowledgeable when one apply settles while another is pending", async () => {
    // The lockout the id-capture fix could produce. The capture effect only
    // fires on a false -> true transition, so with A warned and B admitted
    // afterwards, A settling leaves the flag UP and the captured list pinned
    // to A. Every further acknowledgement then replays a list that retires
    // nothing, and the operator cannot import until a reload (review #184,
    // codex P2). The handler re-captures what remains, so the second click
    // acknowledges the warning still on screen.
    const scope = UNSCOPED_IMPORT_SCOPE;
    const keyFor = (id: string) => `ums.unsettledChannelImport.${scope}.${id}`;
    // A is outstanding before the view mounts: the warning goes up capturing A.
    globalThis.localStorage.setItem(keyFor("apply-A"), "1");
    routeFetch();
    renderRegistry();
    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    expect(screen.getByText(/may still be committing/i)).toBeInTheDocument();

    // Another tab admits B, then A settles. jsdom shares localStorage but does
    // not synthesise the cross-document event, so raise it explicitly.
    globalThis.localStorage.setItem(keyFor("apply-B"), "1");
    fireEvent(globalThis.window, new StorageEvent("storage", { key: keyFor("apply-B") }));
    globalThis.localStorage.removeItem(keyFor("apply-A"));
    fireEvent(globalThis.window, new StorageEvent("storage", { key: keyFor("apply-A") }));

    // First acknowledgement: B is deliberately preserved, so the warning stays.
    fireEvent.click(screen.getByRole("button", { name: /this import is accounted for/i }));
    expect(screen.getByText(/may still be committing/i)).toBeInTheDocument();

    // Second acknowledgement clears it — no reload needed.
    fireEvent.click(screen.getByRole("button", { name: /this import is accounted for/i }));
    await waitFor(() =>
      expect(screen.queryByText(/may still be committing/i)).not.toBeInTheDocument(),
    );
    expect(screen.getByRole("button", { name: /import csv/i })).toBeEnabled();
  });

  it("points an AUDIT-capable operator at the audit trail instead", async () => {
    // The other half of the capability branch. An operator who can open
    // AuditView gets the evidence that actually settles authorship; one who
    // cannot must not be sent there (review #184, codex P2).
    const applyGate = deferredResponse();
    routeFetch({
      importPost: (form) =>
        form.get("dry_run") === "true" ? jsonResponse(DRY_RUN_PLAN) : applyGate.pending,
    });
    renderRegistry(undefined, true);
    await openImport();
    await fillUpload();
    fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));
    await waitFor(() =>
      expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument(),
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() =>
      expect(screen.getByRole("button", { name: /^cancel$/i })).toBeDisabled(),
    );
    applyGate.reject(new TypeError("Failed to fetch"));
    await waitFor(() =>
      expect(screen.getByText(/may have committed/i)).toBeInTheDocument(),
    );
    fireEvent.click(screen.getByRole("button", { name: /^cancel$/i }));

    const notice = await screen.findByRole("status");
    expect(notice).toHaveTextContent(/CHANNEL_IMPORTED/);
    expect(notice).not.toHaveTextContent(/cannot open the audit trail/i);
    expect(
      within(notice).getByRole("button", { name: /checked the audit trail/i }),
    ).toBeInTheDocument();
  });

  it("does not raise the unsettled notice after a normal applied exit", async () => {
    // The complement: a 2xx apply is settled, so leaving Applied must NOT
    // leave the registry wearing a warning or lock the importer.
    //
    // The apply leg must answer with APPLY_RESULT, not the dry-run body: the
    // hook now requires the response's `dry_run` to match the request, and a
    // preview payload returned to an apply is a shape the backend cannot
    // produce. Reusing DRY_RUN_PLAN here was the fixture asserting against an
    // impossible response (review #184, codex P2).
    await runDryRunToPreview((form) =>
      jsonResponse(form.get("dry_run") === "true" ? DRY_RUN_PLAN : APPLY_RESULT),
    );
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await screen.findByRole("button", { name: /back to registry/i });
    fireEvent.click(screen.getByRole("button", { name: /back to registry/i }));

    await waitFor(() => expect(screen.getByText("UMS Drama")).toBeInTheDocument());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: /import csv/i })).toBeEnabled();
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

  it("settles an indeterminate apply by re-planning: reports the state it can prove", async () => {
    // The way OUT of "unknown", using the endpoint the flow already has. The
    // apply is ONE all-or-nothing transaction, so an all-UNCHANGED re-plan is
    // decisive: the roster IS the registry, therefore the write committed.
    // NO group keys: `outcome` is computed from channel inventory alone, so
    // all-UNCHANGED is only proof for a roster that owes no group effects.
    const settled: ChannelImportResult = withDisplayDigest({
      ...DRY_RUN_PLAN,
      plan_fingerprint: "plan-settled",
      counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 2, ERROR: 0 },
      rows: DRY_RUN_PLAN.rows.map((row) => ({
        ...row,
        outcome: "UNCHANGED",
        changes: {},
        // An UNCHANGED row leaves the classification alone, so the planner
        // reports no transition — these are spread from a CREATE fixture.
        revenue_source_status: null,
        group_id: null,
        group_action: null,
      })),
    });
    await reachIndeterminate(() => jsonResponse(settled));

    fireEvent.click(screen.getByRole("button", { name: /check whether it landed/iu }));

    await waitFor(() =>
      expect(screen.getByText(/registry now matches this roster/i)).toBeInTheDocument(),
    );
    // The reconciliation used the DRY RUN, so nothing was written to find out.
    expect(importPosts()[2].get("dry_run")).toBe("true");

    // It reports the STATE, not authorship: inventory equality cannot prove
    // that THIS request committed (the apply may never have reached the
    // backend while another writer landed the same values), so the flow does
    // not advance to a step that claims "your import was applied", and it
    // points at the audit trail instead.
    expect(screen.queryByRole("group", { name: "Import applied" })).not.toBeInTheDocument();
    expect(screen.getByText(/check the Audit view/i)).toBeInTheDocument();
    // Apply stays refused, so no duplicate CHANNEL_IMPORTED is possible.
    expect(screen.getByRole("button", { name: /^apply$/i })).toBeDisabled();
  });

  it("will not call a group-bearing roster applied on an all-UNCHANGED re-plan", async () => {
    // The planner computes `outcome` from channel inventory and never loads
    // memberships, so a roster whose channels already match can still owe the
    // group attachments the lost apply was supposed to make. Declaring that
    // "applied" would report a half-written import as done.
    const channelsMatch: ChannelImportResult = withDisplayDigest({
      ...DRY_RUN_PLAN,
      plan_fingerprint: "plan-groups-pending",
      counts: { CREATE: 0, UPDATE: 0, UNCHANGED: 2, ERROR: 0 },
      // row[1] keeps its group_id "g1" — the effect that cannot be verified.
      rows: DRY_RUN_PLAN.rows.map((row) => ({
        ...row,
        outcome: "UNCHANGED",
        changes: {},
        revenue_source_status: null,
      })),
    });
    await reachIndeterminate(() => jsonResponse(channelsMatch));

    fireEvent.click(screen.getByRole("button", { name: /check whether it landed/iu }));

    await waitFor(() =>
      expect(screen.getByText(/cannot tell whether those memberships/i)).toBeInTheDocument(),
    );
    // Explicitly NOT applied, and not falsely reported as "did not commit".
    expect(screen.queryByRole("group", { name: "Import applied" })).not.toBeInTheDocument();
    expect(screen.queryByText(/does not match this roster/i)).not.toBeInTheDocument();
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

    await waitFor(() => expect(screen.getByText(/does not match this roster/i)).toBeInTheDocument());
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
            {
              // Header fields present and VALID on purpose: without them the
              // shared validator rejects on the header and never reaches the
              // rows, so the test would pass while covering nothing of what
              // it claims (review #184, qodo).
              detail: {
                dry_run: false,
                content_owner_id: "OWNERaaa",
                cms_status: "INSIDE_CMS",
                rows: [{}],
                counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
                plan_fingerprint: "x",
                display_digest: "x-digest",
              },
            },
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

    // The decisive assertion: a retry is still BOUND to the plan on screen —
    // by both tokens.
    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(importPosts()).toHaveLength(3));
    expect(importPosts()[2].get("expected_plan_fingerprint")).toBe("plan-clean-v1");
    expect(importPosts()[2].get("expected_display_digest")).toBe(DRY_RUN_PLAN.display_digest);
  });

  it("refuses a refreshed plan with no display digest rather than unbinding the apply", async () => {
    // The digest's own copy of the no-fingerprint hazard (review #184, C1):
    // rows, counts and the fingerprint all present, only display_digest
    // missing. Accepting it would replace the preview with a plan whose next
    // Apply sends no expected_display_digest — silently dropping the
    // disclosed-plan half of the binding. Fail closed instead.
    const noDigest = { ...DRY_RUN_PLAN } as Record<string, unknown>;
    delete noDigest.display_digest;
    await runDryRunToPreview((form) =>
      form.get("dry_run") === "true"
        ? jsonResponse(DRY_RUN_PLAN)
        : jsonResponse({ detail: noDigest }, 409),
    );

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(screen.getByText("Apply failed")).toBeInTheDocument());

    // The approved preview — and its tokens — survive untouched.
    expect(screen.getByRole("group", { name: "Import preview" })).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: /^apply$/i }));
    await waitFor(() => expect(importPosts()).toHaveLength(3));
    expect(importPosts()[2].get("expected_plan_fingerprint")).toBe("plan-clean-v1");
    expect(importPosts()[2].get("expected_display_digest")).toBe(DRY_RUN_PLAN.display_digest);
  });

  it("discloses the revenue source status the write derives", async () => {
    // The write re-classifies revenue_source_status whenever revenue_required
    // flips, and that status drives missing_official_revenue and the
    // registry's recommended action. `changes` never carries it — it holds the
    // operator's own field edits — so without this cell the operator approves
    // a finance-source mutation nothing on screen mentions.
    const plan: ChannelImportResult = {
      ...DRY_RUN_PLAN,
      // In row_number order, because the backend sorts its entries and this
      // fixture had them reversed — a shape no response can carry.
      rows: [
        // A CREATE has no prior status, so only the value it is born with.
        {
          ...DRY_RUN_PLAN.rows[0],
          revenue_source_status: { from: null, to: "MISSING_REVENUE_SOURCE" },
        },
        // A flip OFF: a proven official classification is being replaced.
        {
          ...DRY_RUN_PLAN.rows[1],
          revenue_source_status: {
            from: "OFFICIAL_CMS_REVENUE",
            to: "PERFORMANCE_ONLY",
          },
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
    // Needs its own plan — DRY_RUN_PLAN's rows both DO move the classification
    // (a CREATE is born with one, and its UPDATE row flips the flag), which is
    // the shape the backend actually emits for them.
    const nameOnly: ChannelImportResult = {
      ...DRY_RUN_PLAN,
      counts: { CREATE: 0, UPDATE: 1, UNCHANGED: 0, ERROR: 0 },
      rows: [
        {
          ...DRY_RUN_PLAN.rows[1],
          revenue_required: true,
          revenue_source_status: null,
          changes: { channel_name: { from: "Old Beta", to: "Beta Channel" } },
        },
      ],
    };
    await runDryRunToPreview(() => jsonResponse(nameOnly));

    expect(screen.getByText("channel_name: Old Beta → Beta Channel")).toBeInTheDocument();
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

  it("REFUSES a plan whose group action is not a declared literal", async () => {
    // Supersedes the earlier "degrade to the bare group key" behaviour, and
    // the reason is worth stating: degrading looked safe because it renders
    // nothing WRONG, but it renders nothing at all about whether the apply
    // mints a new finance-scope SECTOR group or joins an existing one — the
    // most consequential thing a Group_ID row does, and the reason the column
    // exists. Silence on that is not a safe fallback, so the payload is now
    // rejected at the boundary instead (review #184, codex P2).
    //
    // "toString" is deliberate: it is a prototype-chain name, so this also
    // pins that the literal check is a value comparison, not a property probe.
    const oddPlan = {
      ...DRY_RUN_PLAN,
      rows: [{ ...DRY_RUN_PLAN.rows[1], group_id: "g9", group_action: "toString" }],
    };
    routeFetch({ importPost: () => jsonResponse(oddPlan) });
    renderRegistry();
    await openImport();
    await fillUpload();
    fireEvent.click(within(uploadPanel()).getByRole("button", { name: /^preview$/i }));

    // No preview is ever built from it, and the operator is told it failed
    // rather than being shown a plan with a silent group column.
    expect(await screen.findByText(/the import request failed/i)).toBeInTheDocument();
    expect(screen.queryByRole("group", { name: "Import preview" })).not.toBeInTheDocument();
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
