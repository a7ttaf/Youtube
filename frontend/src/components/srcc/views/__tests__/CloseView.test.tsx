import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import CloseView from "@/components/srcc/views/CloseView";
import type {
  FinanceCloseReadinessResponse,
  FinanceMonthCloseStatus,
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

// Route the two CloseView GETs (status vs /readiness) to separate responders.
const routeFetch = (opts: {
  status: () => Response;
  readiness: () => Response;
  lock?: () => Response;
  unlock?: () => Response;
}) => {
  return (input: unknown) => {
    const url = urlOf(input);
    const handlers = [
      ["/readiness", opts.readiness],
      ["/lock", opts.lock],
      ["/unlock", opts.unlock],
    ] as Array<[string, (() => unknown) | undefined]>;
    const mapping = handlers.find(([suffix, fn]) => fn && url.endsWith(suffix));
    const handler = mapping ? mapping[1]! : opts.status;
    return Promise.resolve(handler());
  };
};

const fetchMock = (): ReturnType<typeof vi.fn> => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

/** Resolve a promise from outside via a deferred, to keep a fetch pending. */
export function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((res) => {
    resolve = res;
  });
  return { promise, resolve };
}

const renderCloseView = (canCloseMonth = true) => {
  return render(
    <TenantProvider initialSlug="ums">
      <CloseView permissions={{ canCloseMonth }} />
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
      name: /^confirm lock 2026-03$/i,
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
      name: /^confirm lock 2026-03$/i,
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
      await screen.findByRole("button", { name: /^confirm lock 2026-03$/i }),
    );

    await waitFor(() =>
      expect(screen.getByText("Action failed")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/unresolved close blockers \(2 blockers\)/i),
    ).toBeInTheDocument();
  });
});
