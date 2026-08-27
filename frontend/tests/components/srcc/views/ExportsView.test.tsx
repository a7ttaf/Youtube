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
  // The base-URL tests stub VITE_API_BASE_URL; unstub so it never leaks into a
  // later test that expects the default (relative) href.
  vi.unstubAllEnvs();
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

// A QUEUED finance job: no persisted artifact yet, but downloadable because the
// backend GET route generates on demand. The action is labelled "Generate".
const QUEUED_FINANCE_JOB: ExportJob = {
  ...READY_JOB,
  id: "22222222-2222-2222-2222-222222222222",
  status: "QUEUED",
  file_url: null,
  artifact_filename: null,
  artifact_content_type: null,
  artifact_byte_size: null,
  artifact_checksum_sha256: null,
  completed_at: null,
};

// A CANCELLED job is not downloadable (no artifact, no generation).
const CANCELLED_JOB: ExportJob = {
  ...READY_JOB,
  id: "33333333-3333-3333-3333-333333333333",
  status: "CANCELLED",
  file_url: null,
  completed_at: null,
};

// A FAILED job surfaces its failure_reason instead of a link.
const FAILED_JOB: ExportJob = {
  ...READY_JOB,
  id: "44444444-4444-4444-4444-444444444444",
  status: "FAILED",
  file_url: null,
  failure_reason: "Month is not locked",
  completed_at: null,
};

// A QUEUED analytics CSV job: exposes the generate-on-demand CSV route.
const CSV_JOB: ExportJob = {
  ...QUEUED_FINANCE_JOB,
  id: "55555555-5555-5555-5555-555555555555",
  export_type: "ANALYTICS_SUMMARY_CSV",
};

const EMPTY_LIST: ExportListResponse = {
  items: [],
  pagination: { limit: 50, offset: 0, returned: 0, has_more: false },
};

const POPULATED_LIST: ExportListResponse = {
  items: [READY_JOB],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
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

const methodOf = (init: unknown): string => {
  return ((init as RequestInit | undefined)?.method ?? "GET").toUpperCase();
};

const fetchMock = () => {
  return globalThis.fetch as unknown as ReturnType<typeof vi.fn>;
};

const renderExportsView = (
  canCreateExport = true,
  {
    canExportFinance = true,
    canExportAnalytics = true,
    canViewRevenue = true,
  }: {
    canExportFinance?: boolean;
    canExportAnalytics?: boolean;
    canViewRevenue?: boolean;
  } = {},
) => {
  return render(
    <TenantProvider initialSlug="ums">
      <ExportsView
        canCreateExport={canCreateExport}
        canExportFinance={canExportFinance}
        canExportAnalytics={canExportAnalytics}
        canViewRevenue={canViewRevenue}
      />
    </TenantProvider>,
  );
};

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

  it("keeps the download href relative when VITE_API_BASE_URL is unset", async () => {
    // Explicitly empty base: the href must stay byte-identical to the
    // same-origin (proxied) relative path so the dev proxy injects the headers.
    vi.stubEnv("VITE_API_BASE_URL", "");
    fetchMock().mockResolvedValue(jsonResponse(POPULATED_LIST));
    renderExportsView();

    const link = await screen.findByRole("link", { name: /download xlsx/i });
    expect(link).toHaveAttribute(
      "href",
      "/exports/11111111-1111-1111-1111-111111111111/finance-workbook.xlsx",
    );
  });

  it("targets the configured API origin in the download href when VITE_API_BASE_URL is set", async () => {
    // With a separate API origin, the binary anchor must point at THAT origin —
    // not the frontend origin — matching the base-URL logic the JSON client uses.
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com/");
    fetchMock().mockResolvedValue(jsonResponse(POPULATED_LIST));
    renderExportsView();

    const link = await screen.findByRole("link", { name: /download xlsx/i });
    expect(link).toHaveAttribute(
      "href",
      "https://api.example.com/exports/11111111-1111-1111-1111-111111111111/finance-workbook.xlsx",
    );
  });

  it("renders no mock Export Guardrails panel", async () => {
    fetchMock().mockResolvedValue(jsonResponse(POPULATED_LIST));
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("FINANCE_EXCEL")).toBeInTheDocument(),
    );
    // Panel title, its policy badge, and the three fabricated guardrail rows.
    expect(screen.queryByText("Export Guardrails")).not.toBeInTheDocument();
    expect(screen.queryByText("Policy")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Revenue cells permission checked"),
    ).not.toBeInTheDocument();
    expect(screen.queryByText("Confidence notes required")).not.toBeInTheDocument();
    expect(screen.queryByText("Raw appendix restricted")).not.toBeInTheDocument();
    // The real, per-job status column is still the screen's status signal.
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
  });

  it("renders no guardrail rows in the empty state either", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EMPTY_LIST));
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );
    expect(screen.queryByText("Export Guardrails")).not.toBeInTheDocument();
    expect(
      screen.queryByText("Revenue cells permission checked"),
    ).not.toBeInTheDocument();
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

  it("exposes a Generate link for a QUEUED finance job (download routes generate on demand)", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({
        items: [QUEUED_FINANCE_JOB],
        pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
      }),
    );
    renderExportsView();

    const link = await screen.findByRole("link", { name: /generate xlsx/i });
    expect(link).toHaveAttribute(
      "href",
      "/exports/22222222-2222-2222-2222-222222222222/finance-workbook.xlsx",
    );
    expect(link).toHaveAttribute("download");
  });

  it("does not expose a download link for CANCELLED or FAILED jobs", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({
        items: [CANCELLED_JOB, FAILED_JOB],
        pagination: { limit: 50, offset: 0, returned: 2, has_more: false },
      }),
    );
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("Month is not locked")).toBeInTheDocument(),
    );
    expect(screen.getByText("Not ready")).toBeInTheDocument();
    expect(
      screen.queryByRole("link", { name: /download|generate/i }),
    ).not.toBeInTheDocument();
  });

  it("exposes a Generate CSV link for a queued analytics CSV job", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({
        items: [CSV_JOB],
        pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
      }),
    );
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("ANALYTICS_SUMMARY_CSV")).toBeInTheDocument(),
    );
    const link = screen.getByRole("link", { name: /generate csv/i });
    expect(link).toHaveAttribute(
      "href",
      "/exports/55555555-5555-5555-5555-555555555555/analytics-summary.csv",
    );
    expect(link).toHaveAttribute("download");
  });

  it("hides analytics CSV download links when revenue visibility is withheld", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({
        items: [CSV_JOB],
        pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
      }),
    );
    renderExportsView(true, {
      canExportFinance: false,
      canExportAnalytics: true,
      canViewRevenue: false,
    });

    await waitFor(() =>
      expect(screen.getByText("ANALYTICS_SUMMARY_CSV")).toBeInTheDocument(),
    );
    expect(
      screen.queryByRole("link", { name: /generate csv/i }),
    ).not.toBeInTheDocument();
    expect(screen.getByText("Not ready")).toBeInTheDocument();
  });

  it("offers the analytics CSV create option when analytics export and revenue visibility are both granted", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EMPTY_LIST));
    renderExportsView(true, { canExportFinance: false, canExportAnalytics: true });

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );

    const reportType = screen.getByLabelText("Report type") as HTMLSelectElement;
    expect(reportType).not.toBeDisabled();
    const optionLabels = Array.from(reportType.options).map((o) => o.textContent);
    expect(optionLabels).toEqual(["Analytics summary (CSV)"]);
    fireEvent.change(screen.getByLabelText("Reason"), {
      target: { value: "Need it" },
    });
    expect(screen.getByRole("button", { name: /^generate$/i })).not.toBeDisabled();
  });

  it("hides analytics CSV creation when revenue visibility is withheld", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EMPTY_LIST));
    renderExportsView(true, {
      canExportFinance: false,
      canExportAnalytics: true,
      canViewRevenue: false,
    });

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );

    const reportType = screen.getByLabelText("Report type") as HTMLSelectElement;
    expect(reportType).toBeDisabled();
    const optionLabels = Array.from(reportType.options).map((o) => o.textContent);
    expect(optionLabels).toEqual([
      "No export types are currently available for your role.",
    ]);
    expect(screen.getByRole("button", { name: /^generate$/i })).toBeDisabled();
  });

  it("hides the analytics CSV when analytics export is not permitted", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EMPTY_LIST));
    renderExportsView(true, { canExportFinance: true, canExportAnalytics: false });

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );

    const reportType = screen.getByLabelText("Report type") as HTMLSelectElement;
    const optionValues = Array.from(reportType.options).map((o) => o.value);
    expect(optionValues).not.toContain("ANALYTICS_SUMMARY_CSV");
    expect(optionValues).toContain("FINANCE_EXCEL");
    // The default selection is the first finance option.
    expect(reportType.value).toBe("FINANCE_EXCEL");
  });

  it("offers only USD as the export currency until exchange-rate support ships", async () => {
    fetchMock().mockResolvedValue(jsonResponse(EMPTY_LIST));
    renderExportsView(true);

    await waitFor(() =>
      expect(screen.getByText(/No export jobs yet/i)).toBeInTheDocument(),
    );

    // The backend 422s every non-USD currency (_normalize_currency), so the
    // form must not offer choices that are guaranteed to fail.
    const currency = screen.getByLabelText("Currency") as HTMLSelectElement;
    expect(Array.from(currency.options).map((o) => o.value)).toEqual(["USD"]);
  });
});
