import { useRef, useState } from "react";

import { ApiError, useApiClient } from "@/lib/api/client";
import type {
  ExportJob,
  ExportRequestBody,
  ExportScopeType,
  ExportType,
} from "@/lib/api/types";
import { useExportActions } from "@/lib/api/useExportActions";
import { useExports } from "@/lib/api/useExports";
import type { Severity } from "@/lib/mock/data";
import {
  Badge,
  DEFAULT_MONTH,
  Dot,
  formatTimestamp,
  MONTH_OPTIONS,
} from "../shared";
import { describeError } from "./CommandView";

// ============================================================================
// Purpose: The REAL-data Exports screen, extracted from AppShell. The operator
//   fills a request form (report type + scope + month + currency + reason),
//   "Generate" POSTs to /exports (creating a QUEUED job + audit event), and the
//   jobs table reloads from GET /exports. Each QUEUED or COMPLETED job exposes a
//   two-step download: the shared API client authenticates a prepare request so
//   typed generation/auth/storage failures stay visible, then a same-origin
//   native GET re-enters the protected route through the trusted gateway. The
//   browser download manager consumes that response without buffering the whole
//   artifact in the SPA heap. Loading / error / 403 states mirror CommandView
//   and TraceView. The mock Export Guardrails side panel was
//   DELETED in P1.4: its three rows carried fabricated On/Open/Blocked statuses
//   that no endpoint reports, and the exports API exposes no guardrail state to
//   derive them from — the per-job status column is the honest signal.
// Database/ORM: None (frontend) — consumes GET /exports (list), POST /exports
//   (create, server-side insert + audit), and protected prepare/native artifact
//   GETs; downloads are served by the backend.
// Standards: No client-side export authorization is invented — the backend gate
//   (EXPORT_REVENUE/ANALYTICS_REPORT + VIEW_* @scope, plus VIEW_FINALIZED_PAYMENTS
//   @finance_month for finance exports) is authoritative; a 403 surfaces as
//   no-permission copy. The browser never holds the gateway secret. Native file
//   transfer deliberately uses the same-origin /exports reverse-proxy contract
//   so both dev and production gateways inject authoritative identity again.
// Blast Radius: Export create (write path) + artifact download — both via the
//   backend's own guarded, audited routes only. No source-of-truth finance
//   number is computed or mutated client-side.
// Connections:
//   - File: frontend/src/lib/api/useExports.ts -> the list fetch hook.
//   - File: frontend/src/lib/api/useExportActions.ts -> the create POST hook.
//   - File: frontend/src/lib/api/types.ts -> ExportJob / ExportRequestBody.
//   - File: backend/ums_smart_revenue/api/exports.py:241 request_export / :361 list_exports.
// ============================================================================

// FIX: USD only — the backend rejects every non-USD export currency
// (reports/exports.py::_normalize_currency raises "currency must be USD until
// exchange-rate support is implemented"), so offering EGP/AED produced a
// valid-looking form whose submission was guaranteed to 422. Re-add display
// currencies once backend exchange-rate support ships (Docs/18).
const CURRENCY_OPTIONS = ["USD"];

// Honest empty-state copy when the caller's permissions leave zero creatable
// export types for their current backend grants.
const NO_CREATABLE_TYPES_MESSAGE =
  "No export types are currently available for your role.";

type DownloadRoute = {
  readonly path: (id: string) => string;
  readonly format: string;
  readonly requiresRevenueVisibility?: boolean;
};

type ResolvedDownload = {
  readonly path: string;
  readonly format: string;
};

const DOWNLOAD_ROUTES: Partial<Record<ExportType, DownloadRoute>> = {
  FINANCE_EXCEL: {
    path: (id) => `/exports/${id}/finance-workbook.xlsx`,
    format: "XLSX",
  },
  EXECUTIVE_PDF: {
    path: (id) => `/exports/${id}/executive.pdf`,
    format: "PDF",
  },
  BRANDED_SLIDE_PACK: {
    path: (id) => `/exports/${id}/branded-slide-pack.pptx`,
    format: "PPTX",
  },
  ANALYTICS_SUMMARY_CSV: {
    path: (id) => `/exports/${id}/analytics-summary.csv`,
    format: "CSV",
    requiresRevenueVisibility: true,
  },
};

/** Return whether the export type has a known binary download route. */
const hasDownloadRoute = (
  exportType: string,
): exportType is ExportType =>
  Object.prototype.hasOwnProperty.call(DOWNLOAD_ROUTES, exportType);

// ============================================================================
// Purpose: The real accepted export_type enum values (ALLOWED_EXPORT_TYPES), each
//   tagged with the per-type capability it needs and whether the create form may
//   currently OFFER it. The first three are finance exports; the CSV is analytics
//   and is downloadable through the dedicated analytics-summary route. The
//   backend remains authoritative for the additional finance.view_revenue gate
//   because the CSV carries revenue amounts.
// ============================================================================
type ReportTypeOption = {
  value: ExportType;
  label: string;
  capability: "finance" | "analytics";
  creatable: boolean;
  requiresRevenueVisibility?: boolean;
};

type ReportTypePermissions = {
  canExportFinance: boolean;
  canExportAnalytics: boolean;
  canViewRevenue: boolean;
};

const REPORT_TYPE_OPTIONS: ReportTypeOption[] = [
  { value: "FINANCE_EXCEL", label: "Finance workbook (XLSX)", capability: "finance", creatable: true },
  { value: "EXECUTIVE_PDF", label: "Executive summary (PDF)", capability: "finance", creatable: true },
  { value: "BRANDED_SLIDE_PACK", label: "Branded slide pack (PPTX)", capability: "finance", creatable: true },
  {
    value: "ANALYTICS_SUMMARY_CSV",
    label: "Analytics summary (CSV)",
    capability: "analytics",
    creatable: true,
    requiresRevenueVisibility: true,
  },
];

// The real accepted scope_type enum values (ALLOWED_EXPORT_SCOPE_TYPES). Global
// takes no scope_id; the others require one (the backend rejects a missing id).
const SCOPE_TYPE_OPTIONS: Array<{ value: ExportScopeType; label: string }> = [
  { value: "global", label: "Global (all channels)" },
  { value: "company", label: "Company" },
  { value: "sector", label: "Sector" },
  { value: "channel", label: "Channel" },
  { value: "group", label: "Group" },
];

/** Return whether the viewer holds the capability required by this report type. */
const hasReportCapability = (
  option: ReportTypeOption,
  permissions: ReportTypePermissions,
): boolean => {
  return option.capability === "analytics"
    ? permissions.canExportAnalytics
    : permissions.canExportFinance;
};

/** Return whether the viewer may create a report with its revenue visibility requirement. */
const hasCreateRevenueVisibility = (
  option: ReportTypeOption,
  permissions: ReportTypePermissions,
): boolean => {
  return !option.requiresRevenueVisibility || permissions.canViewRevenue;
};

/** Return whether a report type may be offered by the create form. */
const canOfferReportType = (
  option: ReportTypeOption,
  permissions: ReportTypePermissions,
): boolean => {
  return (
    option.creatable &&
    hasReportCapability(option, permissions) &&
    hasCreateRevenueVisibility(option, permissions)
  );
};

// The default report-type selection: the first offered option, or FINANCE_EXCEL
// when none is offered (the form is disabled in that state, so the fallback is
// never submittable). Extracted from ExportsView so the component's cyclomatic
// complexity stays below the DeepSource medium-risk threshold.
const defaultReportType = (options: ReportTypeOption[]): ExportType =>
  options[0]?.value ?? "FINANCE_EXCEL";

// ============================================================================
// Purpose: Keep the report-type selection valid when the offered set no longer
//   contains the current choice — e.g. the viewer's permissions change so the
//   selected type stops being offered. Returns the selection when it is still
//   offered, otherwise the caller's fallback. Extracted from ExportsView so the
//   component's cyclomatic complexity stays below the DeepSource medium-risk
//   threshold.
// Database/ORM: None (frontend) — a pure lookup over the already-filtered
//   option list.
// Standards: `options` is the permission-filtered list canOfferReportType
//   produced, so membership is checked against what the viewer may actually
//   create rather than against the full enum. Side-effect free and total.
// Blast Radius: Export create — this is the value that ends up in the
//   export_type field of POST /exports, so it decides what is actually
//   submitted when the visible selection has gone stale. Because the candidate
//   set is already permission-filtered, a stale selection degrades to an
//   offered type instead of silently submitting one the viewer may not create;
//   the backend's per-type gate remains authoritative either way.
// Connections: canOfferReportType + defaultReportType + canSubmitExportRequest
//   (local), exports.py request_export (authoritative validation).
//   - File: frontend/src/components/srcc/views/ExportsView.tsx ->
//     canOfferReportType builds `options`; defaultReportType supplies the
//     fallback; canSubmitExportRequest gates the submit itself.
//   - File: backend/ums_smart_revenue/api/exports.py:241 request_export -> the
//     authoritative export_type + permission validation.
// ============================================================================
const effectiveReportType = (
  options: ReportTypeOption[],
  selected: ExportType,
  fallback: ExportType,
): ExportType =>
  options.some((option) => option.value === selected) ? selected : fallback;

// ============================================================================
// Purpose: Map an export_type to its binary download route + artifact format.
//   Only one route is valid per type (the backend 422s a mismatched type). The
//   action verb (Download vs Generate) is derived from job status by the caller;
//   this returns the route and the format suffix only.
// Standards: The API client resolves the authenticated `?prepare=true` request
//   through its configured base URL. The subsequent binary GET intentionally
//   stays relative and same-origin so the production/dev reverse proxy injects
//   trusted identity and tenant headers while the browser streams to its native
//   download manager. No secret or bearer grant enters the URL.
// ============================================================================
/**
 * Returns the API-relative binary download route and artifact format for a job,
 * or null if the type has no GET route or its revenue gate is absent.
 */
const downloadFor = (
  job: ExportJob,
  canViewRevenue: boolean,
): ResolvedDownload | null => {
  const route = hasDownloadRoute(job.export_type)
    ? DOWNLOAD_ROUTES[job.export_type]
    : null;
  if (!route || (route.requiresRevenueVisibility && !canViewRevenue)) {
    // FIX: Analytics CSV artifacts include revenue amounts, so the UI must not
    // expose their direct GET route when finance.view_revenue is absent.
    return null;
  }
  const id = encodeURIComponent(job.id);
  return { path: route.path(id), format: route.format };
};

/**
 * Start a prepared artifact through the protected same-origin download route.
 *
 * Deliberately NOT resolved through the API-origin helper: the anchor GET
 * cannot carry the trusted-gateway headers, so it must ride the same-origin
 * gateway that injects them — only the prepare leg (a client fetch with
 * headers) uses the configured API origin. The pinned test
 * "uses the configured API origin only for preparation and keeps the real
 * GET same-origin" holds this contract. The server's Content-Disposition
 * filename stays authoritative in every deployment shape.
 */
const startNativeDownload = (path: string, filename: string): void => {
  const anchor = document.createElement("a");
  try {
    anchor.href = path;
    anchor.download = filename;
    document.body.appendChild(anchor);
    anchor.click();
  } finally {
    anchor.remove();
  }
};

// ============================================================================
// Purpose: Select a safe local fallback filename from persisted artifact
//   metadata or a deterministic job-id/format value. The protected GET's
//   Content-Disposition remains authoritative when the browser receives it.
// Database/ORM: None (frontend) — reads only typed ExportJob metadata.
// Standards: Reject control/path characters and normalize Windows-invalid
//   filename characters before a value reaches an anchor download attribute.
//   Never use metadata as a path.
// Blast Radius: Export artifact presentation only — no finance value,
//   authorization decision, or backend state is calculated client-side.
// Connections:
//   - File: backend/ums_smart_revenue/api/exports.py -> download routes emit
//     Content-Disposition and persist artifact_filename during preparation.
// ============================================================================
const REJECTED_DOWNLOAD_FILENAMES = new Set(["", ".", ".."]) as ReadonlySet<string>;

// FIX: Classify code units explicitly so C0, DEL, and the complete C1 range
// are rejected without embedding control characters in a regular expression.
const isUnsafeFilenameControl = (character: string): boolean => {
  const codeUnit = character.charCodeAt(0);
  if (codeUnit <= 0x1f) return true;
  return codeUnit >= 0x7f && codeUnit <= 0x9f;
};

/** Return whether a filename contains a C0, DEL, or C1 control code unit. */
const hasUnsafeFilenameControl = (value: string): boolean => {
  return Array.from(value).some(isUnsafeFilenameControl);
};

/** Return whether a filename could be interpreted as a path. */
const hasFilenamePathSeparator = (value: string): boolean => {
  return value.includes("/") || value.includes("\\");
};

/** Reject empty/dot names and path-shaped filename input. */
const hasUnsafeFilenameShape = (value: string): boolean => {
  return (
    REJECTED_DOWNLOAD_FILENAMES.has(value) || hasFilenamePathSeparator(value)
  );
};

// FIX: Windows reserves device names regardless of extension — "CON.txt" is
// just as unusable as "CON" — so a persisted artifact_filename equal to one
// of them must fall back to the deterministic job-id name instead of being
// handed to the anchor download attribute verbatim.
const WINDOWS_RESERVED_DEVICE_NAMES = new Set([
  "CON",
  "PRN",
  "AUX",
  "NUL",
  "COM1",
  "COM2",
  "COM3",
  "COM4",
  "COM5",
  "COM6",
  "COM7",
  "COM8",
  "COM9",
  "LPT1",
  "LPT2",
  "LPT3",
  "LPT4",
  "LPT5",
  "LPT6",
  "LPT7",
  "LPT8",
  "LPT9",
]) as ReadonlySet<string>;

/** Return whether a filename's extension-stripped base is a reserved device. */
const isWindowsReservedDeviceName = (value: string): boolean => {
  const base = value.split(".", 1)[0];
  return WINDOWS_RESERVED_DEVICE_NAMES.has(base.toUpperCase());
};

/** Normalize characters that Windows forbids in a local filename. */
const normalizeDownloadFilename = (value: string): string => {
  return value.replace(/[<>:"|?*]/g, "_").replace(/[. ]+$/g, "");
};

/** Select a safe local filename or reject unsafe backend and metadata input. */
const safeDownloadFilename = (
  value: string | null | undefined,
): string | null => {
  if (typeof value !== "string") return null;
  if (hasUnsafeFilenameControl(value)) return null;
  const trimmed = value.trim();
  if (hasUnsafeFilenameShape(trimmed)) return null;
  if (isWindowsReservedDeviceName(trimmed)) return null;
  const sanitized = normalizeDownloadFilename(trimmed);
  if (REJECTED_DOWNLOAD_FILENAMES.has(sanitized)) return null;
  if (isWindowsReservedDeviceName(sanitized)) return null;
  return sanitized;
};

/** Prefer persisted metadata, then a deterministic safe anchor fallback. */
const downloadFilenameFor = (
  job: ExportJob,
  format: string,
): string => {
  const artifactFilename = safeDownloadFilename(job.artifact_filename);
  const safeId = job.id.replace(/[^A-Za-z0-9_-]/g, "_");
  return artifactFilename ?? `export-${safeId}.${format.toLowerCase()}`;
};

// ============================================================================
// Purpose: The action verb for a downloadable job's action. A QUEUED job triggers
//   server-side generation on first click; a COMPLETED job serves cached bytes.
// ============================================================================
/** Returns "Generate" for QUEUED jobs (generate-on-demand) and "Download" otherwise. */
const downloadVerb = (job: ExportJob): string => {
  return job.status.toUpperCase() === "QUEUED" ? "Generate" : "Download";
};

/** Maps an export-job status to its Severity badge tone. */
const statusTone = (status: string): Severity => {
  switch (status.toUpperCase()) {
    case "COMPLETED":
      return "green";
    case "QUEUED":
      return "amber";
    case "FAILED":
      return "red";
    case "CANCELLED":
      return "blue";
    default:
      return "blue";
  }
};

// FIX: The backend download routes generate-on-demand: for a QUEUED job with no
// persisted artifact they build + persist + stream the bytes (exports.py
// download_*; the `served is None` branch). So both QUEUED and COMPLETED jobs
// are downloadable — QUEUED triggers server-side generation on first click,
// COMPLETED serves cached bytes. Only FAILED (shows failure_reason) and
// CANCELLED (no artifact) are not downloadable. Gating on COMPLETED + file_url
// previously hid the link for a freshly QUEUED job that the backend would serve.
/**
 * True when the backend can serve or generate this job's artifact: QUEUED
 * (generate-on-demand) and COMPLETED (cached) are downloadable; FAILED and
 * CANCELLED are not.
 */
const isDownloadable = (job: ExportJob): boolean => {
  const status = job.status.toUpperCase();
  return status === "QUEUED" || status === "COMPLETED";
};

/** Builds a human-readable scope label (e.g. "company · company-a"). */
const scopeLabel = (job: ExportJob): string => {
  if (job.scope_type === "global") return "Global";
  if (job.scope_id) return `${job.scope_type} · ${job.scope_id}`;
  return job.scope_type;
};

// ============================================================================
// Purpose: Decide whether the Generate button may fire. Every gate must hold —
//   creation permission, at least one offered report type, the audit reason
//   present, the scope id present when the scope is not global, and no request
//   already in flight. Extracted from ExportsView so the component's cyclomatic
//   complexity stays below the DeepSource medium-risk threshold.
// Database/ORM: None (frontend) — pure predicate over already-resolved form
//   state; issues no request and reads no store.
// Standards: No client-side export authorization is invented. `canCreateExport`
//   is the capability the backend already derived, so this gates the affordance
//   only; the backend's own permission + scope check stays authoritative and a
//   403 still surfaces as no-permission copy. Side-effect free and total — it
//   returns false rather than throwing on any incomplete form.
// Blast Radius: Export create (audited write) — this predicate is what keeps a
//   blank audit reason or a missing non-global scope id from ever reaching
//   POST /exports, and the `submitting` gate is what stops a double-click
//   filing a duplicate audited job. It is a usability gate, not the
//   authorization boundary.
// Connections: useExportActions.ts (create POST + in-flight flag),
//   exports.py request_export (authoritative permission/scope gate).
//   - File: frontend/src/lib/api/useExportActions.ts -> the create POST hook
//     whose `loading` flag feeds the in-flight gate.
//   - File: backend/ums_smart_revenue/api/exports.py:241 request_export -> the
//     authoritative permission/scope gate and the audited insert.
// ============================================================================
const canSubmitExportRequest = (form: {
  canCreateExport: boolean;
  hasCreatableType: boolean;
  reasonProvided: boolean;
  scopeIdProvided: boolean;
  submitting: boolean;
}): boolean =>
  form.canCreateExport &&
  form.hasCreatableType &&
  form.reasonProvided &&
  form.scopeIdProvided &&
  !form.submitting;

/** The Export Center panel header (title, description, and audited badge). */
const ExportCenterHeader = () => {
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="exportsTitle">Export Center</strong>
        <span>
          Request a permission-controlled package, then download the generated,
          checksum-audited artifact
        </span>
      </div>
      <Badge tone="violet">Audited export</Badge>
    </div>
  );
};

/**
 * The Report type field. Renders the creatable-type <select> when at least one
 * type is offered; otherwise it shows an honest disabled state (a single
 * non-selectable option carrying NO_CREATABLE_TYPES_MESSAGE) so a viewer with no
 * creatable types — e.g. analytics-only, since the CSV is held back — never sees
 * an enabled form with no options. The label is kept by the parent so the field
 * stays accessible by name in both states.
 */
const ReportTypeField = ({
  exportType,
  onExportType,
  reportTypeOptions,
  hasCreatableType,
  canCreateExport,
}: {
  exportType: ExportType;
  onExportType: (value: ExportType) => void;
  reportTypeOptions: ReportTypeOption[];
  hasCreatableType: boolean;
  canCreateExport: boolean;
}) => {
  if (!hasCreatableType) {
    return (
      <select id="exportReportType" disabled>
        <option>{NO_CREATABLE_TYPES_MESSAGE}</option>
      </select>
    );
  }
  return (
    <select
      id="exportReportType"
      value={exportType}
      disabled={!canCreateExport}
      onChange={(e) => onExportType(e.target.value as ExportType)}
    >
      {reportTypeOptions.map((o) => (
        <option key={o.value} value={o.value}>
          {o.label}
        </option>
      ))}
    </select>
  );
};

/** The export request form: report type, scope, month, currency, and reason. */
const RequestExportForm = ({
  exportType,
  onExportType,
  reportTypeOptions,
  hasCreatableType,
  scopeType,
  onScopeType,
  scopeId,
  onScopeId,
  requiresScopeId,
  month,
  onMonth,
  currency,
  onCurrency,
  reason,
  onReason,
  canCreateExport,
  canSubmit,
  submitting,
  onGenerate,
}: {
  exportType: ExportType;
  onExportType: (value: ExportType) => void;
  reportTypeOptions: ReportTypeOption[];
  hasCreatableType: boolean;
  scopeType: ExportScopeType;
  onScopeType: (value: ExportScopeType) => void;
  scopeId: string;
  onScopeId: (value: string) => void;
  requiresScopeId: boolean;
  month: string;
  onMonth: (value: string) => void;
  currency: string;
  onCurrency: (value: string) => void;
  reason: string;
  onReason: (value: string) => void;
  canCreateExport: boolean;
  canSubmit: boolean;
  submitting: boolean;
  onGenerate: () => void;
}) => {
  return (
    <div className="form-grid" aria-label="Request export" style={{ margin: 13 }}>
      <div className="field-row">
        <label htmlFor="exportReportType">Report type</label>
        <ReportTypeField
          exportType={exportType}
          onExportType={onExportType}
          reportTypeOptions={reportTypeOptions}
          hasCreatableType={hasCreatableType}
          canCreateExport={canCreateExport}
        />
      </div>
      <div className="field-row">
        <label htmlFor="exportScopeType">Scope type</label>
        <select
          id="exportScopeType"
          value={scopeType}
          disabled={!canCreateExport}
          onChange={(e) => onScopeType(e.target.value as ExportScopeType)}
        >
          {SCOPE_TYPE_OPTIONS.map((o) => (
            <option key={o.value} value={o.value}>
              {o.label}
            </option>
          ))}
        </select>
      </div>
      <div className="field-row">
        <label htmlFor="exportScopeId">Scope id</label>
        <input
          id="exportScopeId"
          value={scopeId}
          disabled={!canCreateExport || !requiresScopeId}
          placeholder={requiresScopeId ? "e.g. company-a" : "Not used for global"}
          onChange={(e) => onScopeId(e.target.value)}
        />
      </div>
      <div className="field-row">
        <label htmlFor="exportMonth">Month</label>
        <select
          id="exportMonth"
          value={month}
          disabled={!canCreateExport}
          onChange={(e) => onMonth(e.target.value)}
        >
          {MONTH_OPTIONS.map((m) => (
            <option key={m} value={m}>
              {m}
            </option>
          ))}
        </select>
      </div>
      <div className="field-row">
        <label htmlFor="exportCurrency">Currency</label>
        <select
          id="exportCurrency"
          value={currency}
          disabled={!canCreateExport}
          onChange={(e) => onCurrency(e.target.value)}
        >
          {CURRENCY_OPTIONS.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
      </div>
      <div className="field-row">
        <label htmlFor="exportReason">Reason</label>
        <input
          id="exportReason"
          value={reason}
          disabled={!canCreateExport}
          placeholder="Recorded on the audit event"
          onChange={(e) => onReason(e.target.value)}
        />
      </div>
      <div className="action-row">
        <button
          className="primary-button"
          type="button"
          disabled={!canSubmit}
          onClick={onGenerate}
        >
          {submitting ? "Generating…" : "Generate"}
        </button>
      </div>
    </div>
  );
};

/** Inline alert banner shown when an export request POST fails. */
const RequestError = ({ error }: { error: ApiError | Error }) => {
  const { title, detail } = describeError(
    error,
    "Your role cannot create this export.",
  );
  return (
    <div className="permission-band" role="alert" style={{ margin: 13 }}>
      <Dot tone="red" />
      <span>
        <strong>{title}</strong>
        <span>{`Export request failed — ${detail}`}</span>
      </span>
      <Badge tone="red">Not created</Badge>
    </div>
  );
};

/** Inline status banner confirming a newly requested export job. */
const RequestSuccess = ({ job }: { job: ExportJob }) => {
  return (
    <div className="permission-band" role="status" style={{ margin: 13 }}>
      <Dot tone="green" />
      <span>
        <strong>Export requested</strong>
        <span>{`${job.export_type} · ${scopeLabel(job)} · ${job.month}`}</span>
      </span>
      <Badge tone={statusTone(job.status)}>{job.status}</Badge>
    </div>
  );
};

/** The export jobs table header row (column labels). */
const ExportJobsTableHead = () => {
  return (
    <thead>
      <tr>
        <th scope="col">Type</th>
        <th scope="col">Scope</th>
        <th scope="col">Month</th>
        <th scope="col">Status</th>
        <th scope="col">Created</th>
        <th scope="col">Completed</th>
        <th scope="col">Download</th>
      </tr>
    </thead>
  );
};

// Downloads ride a security boundary: a failed prepare or anchor dispatch
// must show stable, high-level copy only. The backend's error body and any
// raw Error.message can carry internal diagnostics, so neither is ever
// rendered — the failure class (denied vs. failed) is all an operator needs.
const DOWNLOAD_PREPARE_FORBIDDEN_DETAIL = "Your role cannot download this export.";
const DOWNLOAD_PREPARE_FAILED_DETAIL =
  "The export could not be prepared. Try again, or contact an operator if it keeps failing.";

/** Normalize an unknown artifact-download failure into safe UI detail. */
const exportDownloadErrorDetail = (caught: unknown): string => {
  if (caught instanceof ApiError && caught.status === 403) {
    return DOWNLOAD_PREPARE_FORBIDDEN_DETAIL;
  }
  return DOWNLOAD_PREPARE_FAILED_DETAIL;
};

// ============================================================================
// Purpose: Prepare one authenticated, tenant-scoped artifact, dispatch its
//   same-origin native download, and reload authoritative job metadata.
// Database/ORM: None (frontend) — invokes protected backend export routes only.
// Standards: Shared API client owns prepare auth/base URL/tenant headers; the
//   native GET re-enters the production gateway and backend authorization.
//   Prepare failures and synchronous anchor-dispatch failures become safe UI
//   detail; the browser owns the independent GET result after dispatch.
// Blast Radius: Export artifact download only; no finance or auth decision is
//   calculated client-side; no artifact bytes are materialized in JavaScript.
// Connections:
//   - File: frontend/src/lib/api/client.ts -> get performs the guarded prepare.
//   - File: backend/ums_smart_revenue/api/exports.py -> prepare + download.
// ============================================================================
const ExportDownloadAction = ({
  job,
  download,
  onArtifactPrepared,
}: {
  job: ExportJob;
  download: ResolvedDownload;
  onArtifactPrepared: () => void;
}) => {
  const client = useApiClient();
  const [busy, setBusy] = useState(false);
  const [errorDetail, setErrorDetail] = useState<string | null>(null);
  const inFlightRef = useRef(false);

  /** Prepare, dispatch one download, and retain the latch until reload settles. */
  const startDownload = (): void => {
    // State updates are asynchronous; the ref closes the same-tick window
    // where two click handlers could otherwise both observe busy === false.
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true);
    setErrorDetail(null);

    /** Prepare, dispatch the native transfer, then reload authoritative metadata. */
    const prepareAndDownload = async (): Promise<void> => {
      // FIX: Response.blob() retained the complete artifact (up to the backend's
      // 500 MiB limit) in the SPA heap. Preparation returns no bytes; the actual
      // protected GET is consumed by the browser's native download manager.
      // Typed as undefined, not void: the prepare leg answers 204 and the
      // client's parseBody maps bodyless statuses to undefined.
      await client.get<undefined>(`${download.path}?prepare=true`, {
        // A cached 204 would skip generation and authorization on a later click.
        // The backend also marks both handshake legs no-store; request cache mode
        // makes the browser-side half of that contract explicit.
        cache: "no-store",
      });
      startNativeDownload(
        download.path,
        downloadFilenameFor(job, download.format),
      );
      onArtifactPrepared();
    };

    prepareAndDownload()
      .catch((caught: unknown) => {
        setErrorDetail(exportDownloadErrorDetail(caught));
      })
      .finally(() => {
        inFlightRef.current = false;
        setBusy(false);
      });
  };

  return (
    <>
      <button
        className="mini-button"
        type="button"
        disabled={busy}
        onClick={startDownload}
      >
        {busy
          ? `Preparing ${download.format}`
          : `${downloadVerb(job)} ${download.format}`}
      </button>
      {errorDetail ? (
        <span className="form-error" role="alert">
          {errorDetail}
        </span>
      ) : null}
    </>
  );
};

/** Return the honest unavailable state for a non-downloadable export job. */
const unavailableDownloadLabel = (job: ExportJob): string => {
  return job.status.toUpperCase() === "FAILED"
    ? job.failure_reason ?? "Failed"
    : "Not ready";
};

/**
 * Render a download action only for a ready/generatable job with an allowed
 * route; otherwise render the job's failure or not-ready state.
 */
const ExportDownloadCell = ({
  job,
  canViewRevenue,
  onArtifactPrepared,
}: {
  job: ExportJob;
  canViewRevenue: boolean;
  onArtifactPrepared: () => void;
}) => {
  if (!isDownloadable(job)) {
    return <span className="muted">{unavailableDownloadLabel(job)}</span>;
  }
  const download = downloadFor(job, canViewRevenue);
  if (!download) {
    return <span className="muted">{unavailableDownloadLabel(job)}</span>;
  }
  return (
    <ExportDownloadAction
      job={job}
      download={download}
      onArtifactPrepared={onArtifactPrepared}
    />
  );
};

/** A single export-job table row, including its status badge and download cell. */
const ExportJobRow = ({
  job,
  canViewRevenue,
  onArtifactPrepared,
}: {
  job: ExportJob;
  canViewRevenue: boolean;
  onArtifactPrepared: () => void;
}) => {
  return (
    <tr>
      <td>{job.export_type}</td>
      <td>{scopeLabel(job)}</td>
      <td>{job.month}</td>
      <td>
        <Badge tone={statusTone(job.status)}>{job.status}</Badge>
      </td>
      <td>{formatTimestamp(job.created_at)}</td>
      <td>{formatTimestamp(job.completed_at)}</td>
      <td>
        <ExportDownloadCell
          job={job}
          canViewRevenue={canViewRevenue}
          onArtifactPrepared={onArtifactPrepared}
        />
      </td>
    </tr>
  );
};

/** Renders the export jobs table, or the error / loading / empty placeholder. */
const ExportJobsTableBody = ({
  jobs,
  loading,
  error,
  canViewRevenue,
  onArtifactPrepared,
}: {
  jobs: ExportJob[];
  loading: boolean;
  error: ApiError | Error | null;
  canViewRevenue: boolean;
  onArtifactPrepared: () => void;
}) => {
  if (error) {
    const { title, detail } = describeError(
      error,
      "Your role cannot view export jobs.",
    );
    return (
      <div className="table-wrap" role="alert">
        <div style={{ padding: 16 }}>
          <strong>{title}</strong>
          <p className="item-sub">{detail}</p>
        </div>
      </div>
    );
  }

  if (loading && jobs.length === 0) {
    return (
      <div className="table-wrap" aria-busy="true">
        <div style={{ padding: 16 }} className="item-sub">
          Loading export jobs…
        </div>
      </div>
    );
  }

  if (jobs.length === 0) {
    return (
      <div className="table-wrap">
        <div style={{ padding: 16 }} className="item-sub">
          No export jobs yet. Request one above to generate a package.
        </div>
      </div>
    );
  }

  return (
    <div className="table-wrap">
      <table aria-label="Export jobs">
        <ExportJobsTableHead />
        <tbody>
          {jobs.map((job) => (
            <ExportJobRow
              key={job.id}
              job={job}
              canViewRevenue={canViewRevenue}
              onArtifactPrepared={onArtifactPrepared}
            />
          ))}
        </tbody>
      </table>
    </div>
  );
};

/** The export jobs section: header with a refresh control plus the jobs body. */
const ExportJobsTable = ({
  jobs,
  loading,
  error,
  onRefresh,
  canViewRevenue,
  onArtifactPrepared,
}: {
  jobs: ExportJob[];
  loading: boolean;
  error: ApiError | Error | null;
  onRefresh: () => void;
  canViewRevenue: boolean;
  onArtifactPrepared: () => void;
}) => {
  return (
    <>
      <div className="panel-header" style={{ marginTop: 13 }}>
        <div className="panel-title">
          <strong>Export Jobs</strong>
          <span>Your requested packages and their generated artifacts</span>
        </div>
        <button
          type="button"
          className="icon-button"
          aria-label="Refresh export jobs"
          title="Refresh export jobs"
          onClick={onRefresh}
        >
          ↻
        </button>
      </div>
      <ExportJobsTableBody
        jobs={jobs}
        loading={loading}
        error={error}
        canViewRevenue={canViewRevenue}
        onArtifactPrepared={onArtifactPrepared}
      />
    </>
  );
};

/**
 * The wired Exports screen: a permission-filtered request form plus the export
 * jobs table. Finance and analytics report types are offered only when the
 * caller's matching permission is granted; the default selection is the first
 * allowed report type.
 */
const ExportsView = ({
  canCreateExport,
  canExportFinance,
  canExportAnalytics,
  canViewRevenue,
}: {
  canCreateExport: boolean;
  canExportFinance: boolean;
  canExportAnalytics: boolean;
  canViewRevenue: boolean;
}) => {
  // Offer a report type only when it is currently creatable AND the caller holds
  // its per-type capability. Revenue-valued CSVs also require the scoped/global
  // revenue hint that /session/me derives from the backend grants.
  const reportTypePermissions = {
    canExportFinance,
    canExportAnalytics,
    canViewRevenue,
  };
  const reportTypeOptions = REPORT_TYPE_OPTIONS.filter((option) =>
    canOfferReportType(option, reportTypePermissions),
  );
  const hasCreatableType = reportTypeOptions.length > 0;
  const defaultExportType = defaultReportType(reportTypeOptions);

  const [exportType, setExportType] = useState<ExportType>(defaultExportType);
  const [scopeType, setScopeType] = useState<ExportScopeType>("global");
  const [scopeId, setScopeId] = useState<string>("");
  const [month, setMonth] = useState<string>(DEFAULT_MONTH);
  const [currency, setCurrency] = useState<string>("USD");
  const [reason, setReason] = useState<string>("");

  const effectiveExportType = effectiveReportType(
    reportTypeOptions,
    exportType,
    defaultExportType,
  );

  const { data, loading, error, reload } = useExports();
  const actions = useExportActions();

  const jobs = data?.items ?? [];
  const requiresScopeId = scopeType !== "global";
  const canSubmit = canSubmitExportRequest({
    canCreateExport,
    hasCreatableType,
    reasonProvided: reason.trim().length > 0,
    scopeIdProvided: !requiresScopeId || scopeId.trim().length > 0,
    submitting: actions.loading,
  });

  /**
   * Validates and POSTs the request form, then refetches the list on success.
   * The hook owns the error state, so the rejection is swallowed locally to
   * avoid an unhandled promise rejection.
   */
  const onGenerate = () => {
    if (!canSubmit) {
      return;
    }
    const body: ExportRequestBody = {
      export_type: effectiveExportType,
      scope_type: scopeType,
      // Global takes no scope_id; the backend coerces blank -> null anyway.
      scope_id: requiresScopeId ? scopeId.trim() : null,
      month,
      currency,
      reason: reason.trim(),
      include_confidence_notes: true,
      include_manual_override_notes: true,
    };
    // On success refetch the list so the new QUEUED job appears.
    // FIX: gate side effects on a non-null result — requestExport() resolves
    // with null when a same-tick duplicate is dropped by the in-flight guard
    // (no POST fired); clearing the reason before the real in-flight POST
    // settles would discard the operator's audit reason if that request fails.
    actions
      .requestExport(body)
      .then((created) => {
        if (created !== null) {
          setReason("");
          reload();
        }
      })
      .catch(() => {
        // The hook captures its own error state (actions.error), which the form
        // renders; swallow the rejection here so it does not surface as an
        // unhandled promise rejection.
      });
  };

  return (
    <section className="view-page" aria-labelledby="exportsTitle">
      {/* Single-column since P1.4 removed the mock guardrails aside: a
          `view-grid wide-side` wrapper would reserve an empty 390px column. */}
      <section className="panel">
        <ExportCenterHeader />

        <RequestExportForm
          exportType={effectiveExportType}
          onExportType={setExportType}
          reportTypeOptions={reportTypeOptions}
          hasCreatableType={hasCreatableType}
          scopeType={scopeType}
          onScopeType={setScopeType}
          scopeId={scopeId}
          onScopeId={setScopeId}
          requiresScopeId={requiresScopeId}
          month={month}
          onMonth={setMonth}
          currency={currency}
          onCurrency={setCurrency}
          reason={reason}
          onReason={setReason}
          canCreateExport={canCreateExport}
          canSubmit={canSubmit}
          submitting={actions.loading}
          onGenerate={onGenerate}
        />

        {actions.error ? <RequestError error={actions.error} /> : null}
        {actions.data ? <RequestSuccess job={actions.data} /> : null}

        <ExportJobsTable
          jobs={jobs}
          loading={loading}
          error={error}
          onRefresh={reload}
          canViewRevenue={canViewRevenue}
          onArtifactPrepared={reload}
        />
      </section>
    </section>
  );
};

export default ExportsView;
