import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ExportsView from "@/components/srcc/views/ExportsView";
import type { ExportJob, ExportListResponse } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
});

const READY_JOB: ExportJob = {
  id: "11111111-1111-1111-1111-111111111111",
  export_type: "FINANCE_EXCEL",
  scope_type: "company",
  scope_id: "company-a",
  scope_channel_ids: ["channel-a"],
  month: "2026-03",
  currency: "USD",
  requested_by: "user-1",
  status: "COMPLETED",
  file_url: "file-store://export/11111111/ums-finance.xlsx",
  artifact_filename: "ums-finance.xlsx",
  artifact_content_type:
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  artifact_byte_size: 4096,
  artifact_checksum_sha256: "a".repeat(64),
  failure_reason: null,
  month_lock_status: "LOCKED",
  include_confidence_notes: true,
  include_manual_override_notes: true,
  created_at: "2026-03-31T01:42:00+00:00",
  completed_at: "2026-03-31T01:43:00+00:00",
};

const EMPTY_LIST: ExportListResponse = {
  items: [],
  pagination: { limit: 50, offset: 0, returned: 0, has_more: false },
};

const POPULATED_LIST: ExportListResponse = {
  items: [READY_JOB],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
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

function methodOf(init: unknown): string {
  return ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase();
}

function fetchMock() {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
}

function renderExportsView(canCreateExport = true) {
  return render(
    <TenantProvider initialSlug="ums">
      <ExportsView canCreateExport={canCreateExport} />
    </TenantProvider>,
  );
}

describe("ExportsView wired to the exports endpoint", () => {
  it("renders the empty state when no export jobs exist", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EMPTY_LIST));
    renderExportsView();

    await waitFor(() =>
      expect(
        screen.getByText(/No export jobs yet/i),
      ).toBeInTheDocument(),
    );
    // No download links in the empty state.
    expect(
      screen.queryByRole("link", { name: /download/i }),
    ).not.toBeInTheDocument();
  });

  it("renders a populated list with a COMPLETED job and a download link to the proxied binary path", async () => {
    fetchMock().mockResolvedValue(jsonResponse(POPULATED_LIST));
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("FINANCE_EXCEL")).toBeInTheDocument(),
    );
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
    expect(screen.getByText("company · company-a")).toBeInTheDocument();

    const link = screen.getByRole("link", { name: /download xlsx/i });
    expect(link).toHaveAttribute(
      "href",
      "/exports/11111111-1111-1111-1111-111111111111/finance-workbook.xlsx",
    );
    expect(link).toHaveAttribute("download");
  });

  it("does NOT fetch the binary through the api client (download is a plain anchor only)", async () => {
    fetchMock().mockResolvedValue(jsonResponse(POPULATED_LIST));
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("FINANCE_EXCEL")).toBeInTheDocument(),
    );
    // The screen only fetched the JSON list; it never fetched the .xlsx route.
    expect(
      fetchMock().mock.calls.some(([input]) =>
        urlOf(input).includes("finance-workbook.xlsx"),
      ),
    ).toBe(false);
  });

  it("submits the request form (POST /exports) and refetches the list on success", async () => {
    // First GET = empty; the POST creates a QUEUED job; the post-POST refetch
    // returns the populated list.
    let listCallCount = 0;
    fetchMock().mockImplementation((input: unknown, init?: unknown) => {
      const url = urlOf(input);
      if (url === "/exports" && methodOf(init) === "POST") {
        return Promise.resolve(
          jsonResponse({ ...READY_JOB, status: "QUEUED", audit_event: {} }, 202),
        );
      }
      // GET /exports list: empty first, populated after the POST.
      listCallCount += 1;
      return Promise.resolve(
        jsonResponse(listCallCount <= 1 ? EMPTY_LIST : POPULATED_LIST),
      );
    });

    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );

    // Fill the required reason, then Generate.
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "Monthly finance close workbook" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^generate$/i }));

    // The success banner appears and the list refetches to show the new job.
    await waitFor(() =>
      expect(screen.getByText("Export requested")).toBeInTheDocument(),
    );
    await waitFor(() =>
      expect(screen.getByText("FINANCE_EXCEL")).toBeInTheDocument(),
    );

    // A POST to /exports actually fired.
    expect(
      fetchMock().mock.calls.some(
        ([input, init]) => urlOf(input) === "/exports" && methodOf(init) === "POST",
      ),
    ).toBe(true);
  });

  it("disables Generate until a reason is provided", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EMPTY_LIST));
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );
    // Global scope (default) needs no scope_id, but reason is required.
    expect(screen.getByRole("button", { name: /^generate$/i })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "Need it" },
    });
    expect(screen.getByRole("button", { name: /^generate$/i })).toBeEnabled();
  });

  it("maps a 403 from the list to a no-permission message", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: exports.analytics" }, 403),
    );
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("No permission")).toBeInTheDocument(),
    );
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("surfaces a POST error without crashing the screen", async () => {
    fetchMock().mockImplementation((input: unknown, init?: unknown) => {
      if (urlOf(input) === "/exports" && methodOf(init) === "POST") {
        return Promise.resolve(
          jsonResponse({ detail: "Unknown export_type: WAT" }, 422),
        );
      }
      return Promise.resolve(jsonResponse(EMPTY_LIST));
    });
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "trigger error" },
    });
    fireEvent.click(screen.getByRole("button", { name: /^generate$/i }));

    await waitFor(() =>
      expect(screen.getByText(/Export request failed/i)).toBeInTheDocument(),
    );
  });
});
