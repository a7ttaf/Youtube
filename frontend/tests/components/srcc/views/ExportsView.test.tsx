import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ExportsView from "@/components/srcc/views/ExportsView";
import type { ExportJob, ExportListResponse, ExportType } from "@/lib/api/types";
import { TenantProvider } from "@/contexts/TenantContext";

const ORIGINAL_FETCH = globalThis.fetch;

beforeEach(() => {
  vi.stubGlobal("fetch", vi.fn());
});

afterEach(() => {
  globalThis.fetch = ORIGINAL_FETCH;
  vi.restoreAllMocks();
  // The base-URL tests stub VITE_API_BASE_URL; unstub so it never leaks into a
  // later test that expects the default relative API path.
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

const QUEUED_LIST: ExportListResponse = {
  items: [QUEUED_FINANCE_JOB],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
};

const COMPLETED_QUEUED_JOB: ExportJob = {
  ...QUEUED_FINANCE_JOB,
  status: "COMPLETED",
  file_url: "file-store://export/22222222/ums-finance-2026-08-global.xlsx",
  artifact_filename: "ums-finance-2026-08-global.xlsx",
  artifact_content_type:
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
  artifact_byte_size: 4096,
  artifact_checksum_sha256: "b".repeat(64),
  completed_at: "2026-08-31T01:43:00+00:00",
};

const COMPLETED_QUEUED_LIST: ExportListResponse = {
  items: [COMPLETED_QUEUED_JOB],
  pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
};

const jsonResponse = (body: unknown, status = 200) => {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};

const noContentResponse = () => new Response(null, { status: 204 });

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
    // No download actions in the empty state.
    expect(
      screen.queryByRole("button", { name: /^(download|generate) (xlsx|pdf|pptx|csv)$/i }),
    ).not.toBeInTheDocument();
  });

  it("renders a populated list with a COMPLETED job and a download action", async () => {
    fetchMock().mockResolvedValue(jsonResponse(POPULATED_LIST));
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("FINANCE_EXCEL")).toBeInTheDocument(),
    );
    expect(screen.getByText("COMPLETED")).toBeInTheDocument();
    expect(screen.getByText("company · company-a")).toBeInTheDocument();

    const button = screen.getByRole("button", { name: /download xlsx/i });
    expect(button).toBeEnabled();
  });

  it("authenticates preparation, then starts a same-origin native GET without buffering a Blob", async () => {
    let clickedHref: string | null = null;
    const blobSpy = vi.spyOn(Response.prototype, "blob");
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedHref = this.getAttribute("href");
    });
    vi.stubEnv("VITE_API_BASE_URL", "");
    fetchMock().mockImplementation((input: unknown) =>
      urlOf(input).includes("finance-workbook.xlsx?prepare=true")
        ? Promise.resolve(noContentResponse())
        : Promise.resolve(jsonResponse(POPULATED_LIST)),
    );
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /download xlsx/i }));
    await waitFor(() => expect(clickedHref).not.toBeNull());
    const prepareCall = fetchMock().mock.calls.find(([input]) =>
      urlOf(input).includes("finance-workbook.xlsx?prepare=true"),
    );
    expect(prepareCall).toBeDefined();
    expect(urlOf(prepareCall?.[0])).toBe(
      "/exports/11111111-1111-1111-1111-111111111111/finance-workbook.xlsx?prepare=true",
    );
    const headers = new Headers((prepareCall?.[1] as RequestInit).headers);
    expect(headers.get("X-UMS-Tenant")).toBe("ums");
    expect((prepareCall?.[1] as RequestInit).cache).toBe("no-store");
    expect(clickedHref).toBe(
      "/exports/11111111-1111-1111-1111-111111111111/finance-workbook.xlsx",
    );
    expect(blobSpy).not.toHaveBeenCalled();
  });

  it("uses the configured API origin only for preparation and keeps the real GET same-origin", async () => {
    let clickedHref: string | null = null;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedHref = this.getAttribute("href");
    });
    vi.stubEnv("VITE_API_BASE_URL", "https://api.example.com/");
    fetchMock().mockImplementation((input: unknown) =>
      urlOf(input).includes("finance-workbook.xlsx?prepare=true")
        ? Promise.resolve(noContentResponse())
        : Promise.resolve(jsonResponse(POPULATED_LIST)),
    );
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /download xlsx/i }));
    await waitFor(() => expect(clickedHref).not.toBeNull());
    const prepareCall = fetchMock().mock.calls.find(([input]) =>
      urlOf(input).includes("finance-workbook.xlsx?prepare=true"),
    );
    expect(prepareCall).toBeDefined();
    expect(urlOf(prepareCall?.[0])).toBe(
      "https://api.example.com/exports/11111111-1111-1111-1111-111111111111/finance-workbook.xlsx?prepare=true",
    );
    const headers = new Headers((prepareCall?.[1] as RequestInit).headers);
    expect(headers.get("X-UMS-Tenant")).toBe("ums");
    expect(clickedHref).toBe(
      "/exports/11111111-1111-1111-1111-111111111111/finance-workbook.xlsx",
    );
  });

  it.each([
    ["EXECUTIVE_PDF", "executive.pdf", "PDF"],
    ["BRANDED_SLIDE_PACK", "branded-slide-pack.pptx", "PPTX"],
    ["ANALYTICS_SUMMARY_CSV", "analytics-summary.csv", "CSV"],
  ] as Array<[ExportType, string, string]>)(
    "prepares %s through its protected route and starts the matching native GET",
    async (exportType, routeSuffix, format) => {
      const list: ExportListResponse = {
        ...POPULATED_LIST,
        items: [{ ...READY_JOB, export_type: exportType }],
      };
      let clickedHref: string | null = null;
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        clickedHref = this.getAttribute("href");
      });
      fetchMock().mockImplementation((input: unknown) =>
        urlOf(input).includes(`${routeSuffix}?prepare=true`)
          ? Promise.resolve(noContentResponse())
          : Promise.resolve(jsonResponse(list)),
      );
      renderExportsView();

      fireEvent.click(
        await screen.findByRole("button", {
          name: new RegExp(`download ${format}`, "i"),
        }),
      );
      const expectedPath = `/exports/${READY_JOB.id}/${routeSuffix}`;
      await waitFor(() => expect(clickedHref).toBe(expectedPath));
      expect(
        fetchMock().mock.calls.some(
          ([input]) => urlOf(input) === `${expectedPath}?prepare=true`,
        ),
      ).toBe(true);
    },
  );

  it("uses a deterministic filename fallback while a queued artifact is prepared", async () => {
    let clickedFilename: string | undefined;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedFilename = this.download;
    });
    fetchMock().mockImplementation((input: unknown) =>
      urlOf(input).includes("finance-workbook.xlsx?prepare=true")
        ? Promise.resolve(noContentResponse())
        : Promise.resolve(jsonResponse(QUEUED_LIST)),
    );
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /generate xlsx/i }));
    await waitFor(() =>
      expect(clickedFilename).toBe(
        "export-22222222-2222-2222-2222-222222222222.xlsx",
      ),
    );
  });

  it("uses safe persisted artifact metadata as the native anchor fallback", async () => {
    let clickedFilename: string | undefined;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clickedFilename = this.download;
    });
    fetchMock().mockImplementation((input: unknown) =>
      urlOf(input).includes("finance-workbook.xlsx?prepare=true")
        ? Promise.resolve(noContentResponse())
        : Promise.resolve(jsonResponse(POPULATED_LIST)),
    );
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /download xlsx/i }));
    await waitFor(() => expect(clickedFilename).toBe("ums-finance.xlsx"));
  });

  it.each([
    ["path traversal", "../../unsafe.xlsx"],
    ["C1 lower-bound control", "unsafe\u0080.xlsx"],
    ["C1 upper-bound control", "unsafe\u009f.xlsx"],
    ["Windows reserved device name", "CON"],
    ["Windows reserved name with extension", "con.txt"],
    ["Windows reserved parallel port", "LPT1.xlsx"],
  ])(
    "rejects %s in persisted filename metadata and uses the deterministic fallback",
    async (_caseName, artifactFilename) => {
      let clickedFilename: string | undefined;
      vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(function (
        this: HTMLAnchorElement,
      ) {
        clickedFilename = this.download;
      });
      const unsafeMetadataList: ExportListResponse = {
        ...POPULATED_LIST,
        items: [{ ...READY_JOB, artifact_filename: artifactFilename }],
      };
      fetchMock().mockImplementation((input: unknown) =>
        urlOf(input).includes("finance-workbook.xlsx?prepare=true")
          ? Promise.resolve(noContentResponse())
          : Promise.resolve(jsonResponse(unsafeMetadataList)),
      );
      renderExportsView();

      fireEvent.click(await screen.findByRole("button", { name: /download xlsx/i }));
      await waitFor(() =>
        expect(clickedFilename).toBe(
          "export-11111111-1111-1111-1111-111111111111.xlsx",
        ),
      );
    },
  );

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

  it("does not fetch a binary route before the operator activates its download action", async () => {
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
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
    expect(anchorClick).not.toHaveBeenCalled();
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

  it("maps a 403 from the list to export-specific no-permission copy", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({ detail: "Missing permission: exports.analytics" }, 403),
    );
    renderExportsView();

    await waitFor(() =>
      expect(screen.getByText("No permission")).toBeInTheDocument(),
    );
    expect(
      screen.getByText("Your role cannot view export jobs."),
    ).toBeInTheDocument();
    expect(screen.queryByText(/net revenue/i)).not.toBeInTheDocument();
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

  it("exposes a Generate action for a QUEUED finance job (download routes generate on demand)", async () => {
    fetchMock().mockResolvedValue(
      jsonResponse({
        items: [QUEUED_FINANCE_JOB],
        pagination: { limit: 50, offset: 0, returned: 1, has_more: false },
      }),
    );
    renderExportsView();

    expect(
      await screen.findByRole("button", { name: /generate xlsx/i }),
    ).toBeEnabled();
  });

  it("reloads exactly once after a successful queued preparation and anchor dispatch", async () => {
    let listCallCount = 0;
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    fetchMock().mockImplementation((input: unknown) => {
      const url = urlOf(input);
      if (url.includes("finance-workbook.xlsx?prepare=true")) {
        return Promise.resolve(noContentResponse());
      }
      listCallCount += 1;
      return Promise.resolve(
        jsonResponse(
          listCallCount === 1 ? QUEUED_LIST : COMPLETED_QUEUED_LIST,
        ),
      );
    });
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /generate xlsx/i }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: /download xlsx/i }),
      ).toBeInTheDocument(),
    );
    expect(listCallCount).toBe(2);
    expect(anchorClick).toHaveBeenCalledTimes(1);
  });

  it("maps a 403 preparation failure to permission copy without starting or reloading", async () => {
    let listCallCount = 0;
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    fetchMock().mockImplementation((input: unknown) => {
      if (urlOf(input).includes("finance-workbook.xlsx?prepare=true")) {
        return Promise.resolve(
          jsonResponse({ detail: "Missing permission: exports.finance" }, 403),
        );
      }
      listCallCount += 1;
      return Promise.resolve(jsonResponse(POPULATED_LIST));
    });
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /download xlsx/i }));
    await waitFor(() =>
      expect(
        screen.getByText("Your role cannot download this export."),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText(/net revenue/i)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(listCallCount).toBe(1);
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("surfaces a non-403 preparation failure as generic copy without leaking backend detail", async () => {
    let listCallCount = 0;
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    fetchMock().mockImplementation((input: unknown) => {
      if (urlOf(input).includes("finance-workbook.xlsx?prepare=true")) {
        // Internal diagnostics in the backend detail must never reach the UI.
        return Promise.resolve(
          jsonResponse(
            { detail: "Artifact is not available: s3://internal-bucket/secret" },
            503,
          ),
        );
      }
      listCallCount += 1;
      return Promise.resolve(jsonResponse(POPULATED_LIST));
    });
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /download xlsx/i }));
    await waitFor(() =>
      expect(
        screen.getByText(
          "The export could not be prepared. Try again, or contact an operator if it keeps failing.",
        ),
      ).toBeInTheDocument(),
    );
    // The backend's own detail text stays off the screen entirely.
    expect(screen.queryByText(/Artifact is not available/i)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toBeInTheDocument();
    expect(listCallCount).toBe(1);
    expect(anchorClick).not.toHaveBeenCalled();
  });

  it("deduplicates same-tick clicks while preparation is pending", async () => {
    let resolvePreparation: (response: Response) => void = () => undefined;
    const pendingPreparation = new Promise<Response>((resolve) => {
      resolvePreparation = resolve;
    });
    const anchorClick = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    fetchMock().mockImplementation((input: unknown) =>
      urlOf(input).includes("finance-workbook.xlsx?prepare=true")
        ? pendingPreparation
        : Promise.resolve(jsonResponse(POPULATED_LIST)),
    );
    renderExportsView();

    const button = await screen.findByRole("button", { name: /download xlsx/i });
    fireEvent.click(button);
    fireEvent.click(button);
    expect(
      fetchMock().mock.calls.filter(([input]) =>
        urlOf(input).includes("finance-workbook.xlsx?prepare=true"),
      ),
    ).toHaveLength(1);

    resolvePreparation(noContentResponse());
    await waitFor(() => expect(anchorClick).toHaveBeenCalledTimes(1));
  });

  it("surfaces a synchronous anchor-dispatch failure and skips reload", async () => {
    let listCallCount = 0;
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {
      throw new Error("Browser refused the download");
    });
    fetchMock().mockImplementation((input: unknown) => {
      if (urlOf(input).includes("finance-workbook.xlsx?prepare=true")) {
        return Promise.resolve(noContentResponse());
      }
      listCallCount += 1;
      return Promise.resolve(jsonResponse(POPULATED_LIST));
    });
    renderExportsView();

    fireEvent.click(await screen.findByRole("button", { name: /download xlsx/i }));
    // The raw exception message (browser-internal diagnostics) is never
    // rendered — the failure degrades to the stable failed-preparation copy.
    await waitFor(() =>
      expect(
        screen.getByText(
          "The export could not be prepared. Try again, or contact an operator if it keeps failing.",
        ),
      ).toBeInTheDocument(),
    );
    expect(screen.queryByText("Browser refused the download")).not.toBeInTheDocument();
    expect(listCallCount).toBe(1);
  });

  it("does not expose a download action for CANCELLED or FAILED jobs", async () => {
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
      screen.queryByRole("button", { name: /^(download|generate) (xlsx|pdf|pptx|csv)$/i }),
    ).not.toBeInTheDocument();
  });

  it("exposes a Generate CSV action for a queued analytics CSV job", async () => {
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
    expect(screen.getByRole("button", { name: /generate csv/i })).toBeEnabled();
  });

  it("hides analytics CSV download actions when revenue visibility is withheld", async () => {
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
      screen.queryByRole("button", { name: /generate csv/i }),
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
