import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import { DEFAULT_MONTH, MONTH_OPTIONS } from "@/components/srcc/shared";
import CloseView from "@/components/srcc/views/CloseView";
import type {
  FinanceCloseReadinessResponse,
  FinanceMonthCloseStatus,
} from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

// The armed confirm button names the month the view is on, which is the rolling
// DEFAULT_MONTH — derive the matcher from it instead of a literal that ages out.
const CONFIRM_LOCK_LABEL = new RegExp(`^confirm lock ${DEFAULT_MONTH}$`, "i");

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const OPEN_STATUS: FinanceMonthCloseStatus = {
  month: "2026-03",
  status: "OPEN",
  allocation_method: null,
  allocation_rule_payload: {},
  locked_by: null,
  locked_at: null,
  unlocked_by: null,
  unlocked_at: null,
};

const LOCKED_STATUS: FinanceMonthCloseStatus = {
  month: "2026-03",
  status: "LOCKED",
  allocation_method: "gross_revenue_proportional",
  allocation_rule_payload: {},
  locked_by: "00000000-0000-0000-0000-0000000000aa",
  locked_at: "2026-04-01T12:00:00+00:00",
  unlocked_by: null,
  unlocked_at: null,
};

const READINESS_READY: FinanceCloseReadinessResponse = {
  month: "2026-03",
  ready: true,
  blockers: [],
};

const READINESS_BLOCKED: FinanceCloseReadinessResponse = {
  month: "2026-03",
  ready: false,
  blockers: [
    {
      blocker_type: "PENDING_MANUAL_OVERRIDES",
      severity: "HIGH",
      count: 2,
      message: "2 pending manual overrides require approval before locking 2026-03.",
    },
    {
      blocker_type: "MISSING_REVENUE_FACTS",
      severity: "HIGH",
      count: 1,
      message: "1 revenue-required channel has no revenue facts for 2026-03.",
    },
  ],
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const urlOf = (input: unknown): string => {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
};

/** Pick the optional POST responder (lock/unlock) that matches `url`, if any. */
const postResponderFor = (
  url: string,
  opts: { lock?: () => Response; unlock?: () => Response },
): (() => Response) | undefined => {
  if (url.endsWith("/lock")) return opts.lock;
  if (url.endsWith("/unlock")) return opts.unlock;
  return undefined;
};

// Route the two CloseView GETs (status vs /readiness) to separate responders.
const routeFetch = (opts: {
  status: () => Response;
  readiness: () => Response;
  lock?: () => Response;
  unlock?: () => Response;
}) => {
  return (input: unknown) => {
    const url = urlOf(input);
    if (url.endsWith("/readiness")) return Promise.resolve(opts.readiness());
    const post = postResponderFor(url, opts);
    if (post) return Promise.resolve(post());
    return Promise.resolve(opts.status());
  };
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

/** Resolve a promise from outside via a deferred, to keep a fetch pending. */
const deferred = <T,>() => {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
};

const renderCloseView = (canCloseMonth = true, canUnlockMonth = true) => {
  return render(
    <TenantProvider initialSlug="ums">
      <CloseView permissions={{ canCloseMonth, canUnlockMonth }} />
    </TenantProvider>,
  );
};

describe("CloseView wired to finance-close", () => {
  it("shows a loading state before the responses resolve", () => {
    fetchMock().mockReturnValue(
      // A never-resolving promise keeps both GETs pending so the loading state shows.
      new Promise<Response>(() => {
        /* intentionally never settles */
      }),
    );
    renderCloseView();
    expect(screen.getByText("Loading month close")).toBeInTheDocument();
  });

  it("renders the real OPEN status and the readiness blocker checklist", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        status: () => jsonResponse(OPEN_STATUS),
        readiness: () => jsonResponse(READINESS_BLOCKED),
      }),
    );
    renderCloseView();

    // Status summary shows OPEN + the blocker count.
    await waitFor(() =>
      expect(screen.getAllByText("OPEN").length).toBeGreaterThan(0),
    );
    expect(screen.getByText("Blocked")).toBeInTheDocument();
    expect(screen.getByText("2 blockers")).toBeInTheDocument();

    // Each blocker message + type renders in the checklist.
    expect(
      screen.getByText(
        "2 pending manual overrides require approval before locking 2026-03.",
      ),
    ).toBeInTheDocument();
    expect(
      screen.getByText(
        "1 revenue-required channel has no revenue facts for 2026-03.",
      ),
    ).toBeInTheDocument();
  });

  it("renders a ready banner and a LOCKED status with timestamps", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        status: () => jsonResponse(LOCKED_STATUS),
        readiness: () => jsonResponse(READINESS_READY),
      }),
    );
    renderCloseView();

    await waitFor(() =>
      expect(screen.getAllByText("LOCKED").length).toBeGreaterThan(0),
    );
    expect(screen.getByText("Month is ready to lock")).toBeInTheDocument();
    // The locked actor id is surfaced in the Lock Controls detail grid.
    expect(
      screen.getByText("00000000-0000-0000-0000-0000000000aa"),
    ).toBeInTheDocument();
  });

  it("shows a no-permission message on a 403 ApiError from the status read", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        status: () =>
          jsonResponse({ detail: "Missing permission: view:revenue" }, 403),
        readiness: () =>
          jsonResponse({ detail: "Missing permission: lock:finance-month" }, 403),
      }),
    );
    renderCloseView();

    await waitFor(() =>
      expect(screen.getAllByText("No permission").length).toBeGreaterThan(0),
    );
  });

  it("renders the honest not-started state when the month has no close row (404)", async () => {    // The rolling default opens on the CURRENT calendar month, which has no
    // finance_month_close row until a finance write creates one — so its status
    // GET 404s by construction. That must read as "not started", never as
    // "Request failed (404)" over the whole summary.
    fetchMock().mockImplementation(
      routeFetch({
        status: () =>
          jsonResponse({ detail: "Finance month close record not found" }, 404),
        readiness: () => jsonResponse(READINESS_BLOCKED),
      }),
    );
    renderCloseView();

    await waitFor(() =>
      expect(screen.getByText("No close record yet")).toBeInTheDocument(),
    );
    const summary = screen.getByLabelText("Month close summary");
    expect(within(summary).getByText("OPEN")).toBeInTheDocument();
    // The Month tile falls back to the month the view is on, not an em dash.
    expect(within(summary).getByText(DEFAULT_MONTH)).toBeInTheDocument();
    // Both status displays agree on the absent-row fallback: the Lock Controls
    // badge says OPEN too, not "—" — one screen, one status (PR #211 review).
    const lockPanel = screen.getByText("Lock Controls").closest("section");
    expect(lockPanel).not.toBeNull();
    expect(within(lockPanel as HTMLElement).getByText("OPEN")).toBeInTheDocument();
    // No error tile anywhere, and nothing claiming the request failed.
    expect(screen.queryByRole("alert")).toBeNull();
    expect(screen.queryByText(/request failed/i)).toBeNull();
    // The readiness panel keeps rendering from its own 200 response.
    expect(within(summary).getByText("Blocked")).toBeInTheDocument();
    expect(within(summary).getByText("2 blockers")).toBeInTheDocument();
    expect(
      screen.getByText(
        "2 pending manual overrides require approval before locking 2026-03.",
      ),
    ).toBeInTheDocument();
  });

  // Regression (PR #211 review, qodo "Month switch flashes false OPEN"): the
  // previous month's no-record verdict must not render as the switched-to
  // month's status. useAsync clears data/flips loading in its effect — one
  // paint frame after the selection changes — so the derivation now tracks
  // which month the settled verdict belongs to and treats a mismatch as
  // unknown. RTL's act() flushes that effect, so the observable contract here
  // is the pending-read window: while the new month's read has not settled,
  // neither the summary nor the lock badge may show a verdict.
  it("shows no status verdict for a switched-to month until its read settles", async () => {
    const otherMonth = MONTH_OPTIONS[1];
    expect(otherMonth).not.toBe(DEFAULT_MONTH);
    const pending = deferred<Response>();
    fetchMock().mockImplementation((input: unknown) => {
      const url = urlOf(input);
      if (url.endsWith("/readiness")) {
        return Promise.resolve(jsonResponse(READINESS_BLOCKED));
      }
      if (url.includes(`/finance-close/${otherMonth}`)) {
        return pending.promise;
      }
      return Promise.resolve(
        jsonResponse({ detail: "Finance month close record not found" }, 404),
      );
    });
    renderCloseView();

    // The first month settled as not-started — its verdict is honest.
    await waitFor(() =>
      expect(screen.getByText("No close record yet")).toBeInTheDocument(),
    );
    const summary = screen.getByLabelText("Month close summary");
    expect(within(summary).getByText("OPEN")).toBeInTheDocument();

    // Switch to the other month: its read is still pending, so no verdict may
    // render anywhere — not the summary tiles, not the lock badge.
    fireEvent.change(screen.getByLabelText("Month"), {
      target: { value: otherMonth },
    });
    const summaryAfter = screen.getByLabelText("Month close summary");
    expect(within(summaryAfter).queryByText("OPEN")).toBeNull();
    expect(within(summaryAfter).getByText("Loading month close")).toBeInTheDocument();
    const lockPanel = screen.getByText("Lock Controls").closest("section");
    expect(lockPanel).not.toBeNull();
    expect(within(lockPanel as HTMLElement).queryByText("OPEN")).toBeNull();

    // Once the switched-to month's read settles as no-record, the verdict is
    // honest again.
    pending.resolve(
      jsonResponse({ detail: "Finance month close record not found" }, 404),
    );
    await waitFor(() =>
      expect(
        within(screen.getByLabelText("Month close summary")).getByText("OPEN"),
      ).toBeInTheDocument(),
    );
  });

  it("still replaces the summary with the error tile on a 500 status read", async () => {
    // Only 404 is remapped; every other failure keeps today's role="alert" tile.
    fetchMock().mockImplementation(
      routeFetch({
        status: () => jsonResponse({ detail: "close lookup exploded" }, 500),
        readiness: () => jsonResponse(READINESS_READY),
      }),
    );
    renderCloseView();

    await waitFor(() =>
      expect(screen.getByText("Request failed (500)")).toBeInTheDocument(),
    );
    const summary = screen.getByLabelText("Month close summary");
    expect(summary).toHaveAttribute("role", "alert");
    expect(within(summary).getByText("close lookup exploded")).toBeInTheDocument();
    expect(screen.queryByText("No close record yet")).toBeNull();
    // A failed status read is UNKNOWN, not open: the Lock Controls badge shows
    // an em dash instead of asserting OPEN for a month whose state it does not
    // have (PR #211 review). (The detail grid's actor cells also render em
    // dashes for a null row, so assert the absence of OPEN plus the presence of
    // at least the badge's dash.)
    const lockPanel = screen.getByText("Lock Controls").closest("section");
    expect(lockPanel).not.toBeNull();
    expect(within(lockPanel as HTMLElement).queryByText("OPEN")).toBeNull();
    expect(
      within(lockPanel as HTMLElement).getAllByText("—").length,
    ).toBeGreaterThan(0);
  });

  it("disables Lock for a viewer without close permission", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        status: () => jsonResponse(OPEN_STATUS),
        readiness: () => jsonResponse(READINESS_READY),
      }),
    );
    renderCloseView(false);

    const lockButton = await screen.findByRole("button", { name: /^lock month$/i });
    expect(lockButton).toBeDisabled();
  });

  it("locks the month via the reason + arm/confirm flow and refetches status", async () => {
    let statusCall = 0;
    fetchMock().mockImplementation(
      routeFetch({
        status: () => {
          statusCall += 1;
          // First load OPEN; after lock + refetch, return LOCKED.
          return jsonResponse(statusCall === 1 ? OPEN_STATUS : LOCKED_STATUS);
        },
        readiness: () => jsonResponse(READINESS_READY),
        lock: () => jsonResponse({ ...LOCKED_STATUS, audit_event: {} }),
      }),
    );

    renderCloseView();
    const lockButton = await screen.findByRole("button", { name: /^lock month$/i });
    // The reason is required: the action stays disabled until one is typed.
    expect(lockButton).toBeDisabled();

    fireEvent.change(screen.getByLabelText(/reason \(required, audited\)/i), {
      target: { value: "March close complete" },
    });

    // First click arms the action (the button switches to a confirm label).
    fireEvent.click(lockButton);
    const confirmButton = await screen.findByRole("button", {
      name: CONFIRM_LOCK_LABEL,
    });
    // Second click executes the POST.
    fireEvent.click(confirmButton);

    // After the successful POST + refetch, the status flips to LOCKED.
    await waitFor(() =>
      expect(screen.getAllByText("LOCKED").length).toBeGreaterThan(0),
    );
    expect(
      fetchMock().mock.calls.some(([input]) =>
        urlOf(input).endsWith("/lock"),
      ),
    ).toBe(true);
    // The reason is sent in the POST body exactly as typed.
    const lockCall = fetchMock().mock.calls.find(([input]) =>
      urlOf(input).endsWith("/lock"),
    );
    if (!lockCall) throw new Error("expected a /lock request to have been made");
    const lockInit = lockCall[1] as RequestInit | undefined;
    expect(JSON.parse(String(lockInit?.body))).toMatchObject({
      reason: "March close complete",
    });
  });

  it("drops a same-tick double-click on the armed confirm: exactly one /lock POST, no error banner", async () => {
    // The /lock response stays PENDING across both confirm clicks (a deferred),
    // so the first POST is still in flight — armed is still "lock", busy has not
    // committed to the DOM, and the second click runs off the same render. Only
    // the synchronous in-flight ref can drop it. A real 409 would fire if the
    // duplicate POST went through.
    const lockDeferred = deferred<Response>();
    let lockCalls = 0;
    let statusCall = 0;
    fetchMock().mockImplementation(
      routeFetch({
        status: () => {
          statusCall += 1;
          return jsonResponse(statusCall === 1 ? OPEN_STATUS : LOCKED_STATUS);
        },
        readiness: () => jsonResponse(READINESS_READY),
        lock: () => {
          lockCalls += 1;
          // The FIRST POST gets the pending deferred; a (regression) second POST
          // would 409, surfacing the misleading banner this test guards against.
          return lockCalls === 1
            ? (lockDeferred.promise as unknown as Response)
            : jsonResponse(
                { detail: "Finance month is already LOCKED." },
                409,
              );
        },
      }),
    );

    renderCloseView();
    const lockButton = await screen.findByRole("button", { name: /^lock month$/i });

    fireEvent.change(screen.getByLabelText(/reason \(required, audited\)/i), {
      target: { value: "March close complete" },
    });

    // First click arms the action (the button switches to a confirm label).
    fireEvent.click(lockButton);
    const confirmButton = await screen.findByRole("button", {
      name: CONFIRM_LOCK_LABEL,
    });

    // Double-click the armed confirm before busy=true re-renders: both clicks run
    // off the same render closure, so the state `busy` guard cannot catch the
    // second — only the synchronous in-flight ref drops it.
    await act(async () => {
      fireEvent.click(confirmButton);
      fireEvent.click(confirmButton);
      // Now settle the first (only) POST and let the refetch flip to LOCKED.
      lockDeferred.resolve(jsonResponse({ ...LOCKED_STATUS, audit_event: {} }));
      await Promise.resolve();
    });

    await waitFor(() =>
      expect(screen.getAllByText("LOCKED").length).toBeGreaterThan(0),
    );

    // Exactly one /lock POST was dispatched; the duplicate click was dropped.
    const lockRequests = fetchMock().mock.calls.filter(([input]) =>
      urlOf(input).endsWith("/lock"),
    );
    expect(lockRequests).toHaveLength(1);
    // No misleading "Action failed" banner (the dropped second click never 409s).
    expect(screen.queryByText("Action failed")).not.toBeInTheDocument();
  });

  it("maps a 409 lock conflict to a clear inline message", async () => {
    fetchMock().mockImplementation(
      routeFetch({
        status: () => jsonResponse(OPEN_STATUS),
        readiness: () => jsonResponse(READINESS_BLOCKED),
        lock: () =>
          jsonResponse(
            {
              detail: {
                message: "Finance month has unresolved close blockers",
                blockers: READINESS_BLOCKED.blockers,
              },
            },
            409,
          ),
      }),
    );
    renderCloseView();
    const lockButton = await screen.findByRole("button", { name: /^lock month$/i });

    fireEvent.change(screen.getByLabelText(/reason \(required, audited\)/i), {
      target: { value: "force lock" },
    });
    // Arm, then confirm the lock so the conflicting POST fires.
    fireEvent.click(lockButton);
    fireEvent.click(
      await screen.findByRole("button", { name: CONFIRM_LOCK_LABEL }),
    );

    await waitFor(() =>
      expect(screen.getByText("Action failed")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/unresolved close blockers \(2 blockers\)/i),
    ).toBeInTheDocument();
  });
});
