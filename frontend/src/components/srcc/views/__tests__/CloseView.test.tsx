import { fireEvent, render, screen, waitFor } from "@testing-library/react";
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

function jsonResponse(body: unknown, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

function urlOf(input: unknown): string {
  if (typeof input === "string") return input;
  if (input instanceof URL) return input.toString();
  if (input instanceof Request) return input.url;
  return String(input);
}

// Route the two CloseView GETs (status vs /readiness) to separate responders.
function routeFetch(opts: {
  status: () => Response;
  readiness: () => Response;
  lock?: () => Response;
  unlock?: () => Response;
}) {
  return (input: unknown) => {
    const url = urlOf(input);
    if (url.endsWith("/readiness")) return Promise.resolve(opts.readiness());
    if (url.endsWith("/lock") && opts.lock) return Promise.resolve(opts.lock());
    if (url.endsWith("/unlock") && opts.unlock)
      return Promise.resolve(opts.unlock());
    return Promise.resolve(opts.status());
  };
}

function fetchMock() {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

function renderCloseView(canCloseMonth = true) {
  return render(
    <TenantProvider initialSlug="ums">
      <CloseView permissions={{ canCloseMonth }} />
    </TenantProvider>,
  );
}

describe("CloseView wired to finance-close", () => {
  it("shows a loading state before the responses resolve", () => {
    fetchMock().mockReturnValue(new Promise<Response>(() => {}));
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

  it("locks the month via the confirm + reason flow and refetches status", async () => {
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
    vi.spyOn(window, "prompt").mockReturnValue("March close complete");
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderCloseView();
    const lockButton = await screen.findByRole("button", { name: /^lock month$/i });
    fireEvent.click(lockButton);

    // After the successful POST + refetch, the status flips to LOCKED.
    await waitFor(() =>
      expect(screen.getAllByText("LOCKED").length).toBeGreaterThan(0),
    );
    expect(
      fetchMock().mock.calls.some(([input]) =>
        urlOf(input).endsWith("/lock"),
      ),
    ).toBe(true);
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
    vi.spyOn(window, "prompt").mockReturnValue("force lock");
    vi.spyOn(window, "confirm").mockReturnValue(true);

    renderCloseView();
    const lockButton = await screen.findByRole("button", { name: /^lock month$/i });
    fireEvent.click(lockButton);

    await waitFor(() =>
      expect(screen.getByText("Action failed")).toBeInTheDocument(),
    );
    expect(
      screen.getByText(/unresolved close blockers \(2 blockers\)/i),
    ).toBeInTheDocument();
  });
});
