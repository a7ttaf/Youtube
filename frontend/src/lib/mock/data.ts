// Mock data for the UMS Smart Revenue Control Center.
// Static snapshot of the March 2026 close. All money values are display-only.

export type Severity = "green" | "amber" | "red" | "blue" | "violet";
export type WorkflowTone = Severity | "primary";
export type ViewKey =
  | "command"
  | "registry"
  | "groups"
  | "close"
  | "trace"
  | "exports"
  | "connectors"
  | "audit";

export type Role = "finance" | "assistant" | "company";

export const VIEW_COPY: Record<ViewKey, { title: string; subtitle: string }> = {
  command: {
    title: "Revenue Command Center",
    subtitle: "March 2026 close, UMS holding scope, USD reporting currency",
  },
  registry: {
    title: "Channel Registry",
    subtitle: "Ownership, CMS status, company scope, and SQL lineage controls",
  },
  groups: {
    title: "CMS Groups",
    subtitle: "Content-owner group mirror, ownership stamps, and sync",
  },
  close: {
    title: "Month Close Workbench",
    subtitle: "Payment reconciliation, allocation review, overrides, and lock controls",
  },
  trace: {
    title: "SQL Trace Explorer",
    subtitle: "Issue lineage filtered by SQL-backed application permissions",
  },
  exports: {
    title: "Export Center",
    subtitle: "Permission-controlled finance, executive, brand, and audit packages",
  },
  connectors: {
    title: "Connector Operations",
    subtitle: "YouTube, Analytics, AdSense, raw files, and restricted job controls",
  },
  audit: {
    title: "Audit Log",
    subtitle:
      "Sensitive action trace for revenue, exports, overrides, connectors, and lineage reads",
  },
};

export const NAV_GROUPS: Array<{
  label: string;
  items: Array<{ key: ViewKey; label: string; count: string; icon: string }>;
}> = [
  {
    label: "Workspace",
    items: [
      { key: "command", label: "Command Center", count: "Live", icon: "command" },
      { key: "registry", label: "Channel Registry", count: "318", icon: "registry" },
      { key: "groups", label: "CMS Groups", count: "CMS", icon: "groups" },
      { key: "close", label: "Month Close", count: "5", icon: "close" },
      { key: "trace", label: "Trace Explorer", count: "SQL", icon: "trace" },
    ],
  },
  {
    label: "Operations",
    items: [
      { key: "exports", label: "Exports", count: "12", icon: "exports" },
      { key: "connectors", label: "Connectors", count: "2", icon: "connectors" },
      { key: "audit", label: "Audit Log", count: "AA", icon: "audit" },
    ],
  },
];

export const KPIS = [
  { id: "revenue", tone: "is-revenue", label: "Gross revenue", value: "$4.82M", badge: { text: "A Official", tone: "green" as Severity }, note: ["YouTube reports", "318 channels"], finance: true },
  { id: "payment", tone: "is-payment", label: "AdSense payment", value: "$4.39M", badge: { text: "B Matched", tone: "blue" as Severity }, note: ["Payment object", "Mar batch"], finance: true },
  { id: "bank", tone: "is-review", label: "Bank received", value: "$4.36M", badge: { text: "Review", tone: "amber" as Severity }, note: ["FX pending", "$31.4K gap"], finance: true },
  { id: "net", tone: "is-net", label: "Net revenue", value: "$4.11M", badge: { text: "B Recon", tone: "blue" as Severity }, note: ["Post-tax allocation", "Locked rows"], finance: true },
  { id: "issues", tone: "is-risk", label: "Open issues", value: "26", badge: { text: "9 High", tone: "red" as Severity }, note: ["Missing sources", "Outside CMS"], finance: false },
  { id: "close", tone: "is-close", label: "Month state", value: "Close 6/7", badge: { text: "Locked", tone: "green" as Severity }, note: ["Final review", "Locked rows"], finance: false },
] as const;

export const CHANNELS = [
  {
    id: "ums-drama",
    avatar: "DR",
    name: "UMS Drama",
    code: "UC-DRAMA-01",
    company: "United Studios",
    cms: { text: "Inside CMS", tone: "green" as Severity },
    gross: "$684,200",
    tax: "$41,900",
    deductions: "$32,400",
    net: "$609,900",
    confidence: { text: "B Recon", tone: "blue" as Severity },
    issue: { text: "FX note", tone: "amber" as Severity },
  },
  {
    id: "ums-news-live",
    avatar: "NL",
    name: "UMS News Live",
    code: "UC-NEWS-22",
    company: "News Network",
    cms: { text: "Inside CMS", tone: "green" as Severity },
    gross: "$512,880",
    tax: "$29,600",
    deductions: "$21,140",
    net: "$462,140",
    confidence: { text: "A Official", tone: "green" as Severity },
    issue: null,
  },
  {
    id: "sports-extra",
    avatar: "SX",
    name: "Sports Extra",
    code: "UC-SPORT-7",
    company: "TV Sector",
    cms: { text: "Outside CMS", tone: "amber" as Severity },
    gross: "$244,700",
    tax: "$18,300",
    deductions: "$19,820",
    net: "$206,580",
    confidence: { text: "C Alloc", tone: "amber" as Severity },
    issue: { text: "Source", tone: "red" as Severity },
  },
  {
    id: "kids-arabic",
    avatar: "KA",
    name: "Kids Arabic",
    code: "UC-KIDS-18",
    company: "Family Media",
    cms: { text: "Inside CMS", tone: "green" as Severity },
    gross: "$199,420",
    tax: "$12,010",
    deductions: "$8,980",
    net: "$178,430",
    confidence: { text: "B Recon", tone: "blue" as Severity },
    issue: null,
  },
];

export const WORKFLOW_STEPS: Array<{ state: string; tone: WorkflowTone; label: string }> = [
  { state: "is-done", tone: "green", label: "Reports" },
  { state: "is-done", tone: "green", label: "Normalize" },
  { state: "is-done", tone: "green", label: "Payments" },
  { state: "is-current", tone: "primary", label: "Allocate" },
  { state: "", tone: "amber", label: "Lock" },
  { state: "", tone: "amber", label: "Export" },
];

export const REGISTRY_ROWS = [
  { avatar: "DR", name: "UMS Drama", code: "UC-DRAMA-01", company: "United Studios", sector: "TV", cms: { text: "Inside CMS", tone: "green" as Severity }, source: "YouTube Reporting API", node: "channel:ums-drama", state: { text: "Approved", tone: "green" as Severity }, action: "Review" },
  { avatar: "SX", name: "Sports Extra", code: "UC-SPORT-7", company: "TV Sector", sector: "TV", cms: { text: "Outside CMS", tone: "amber" as Severity }, source: "Uploaded owner statement", node: "channel:sports-extra", state: { text: "Evidence due", tone: "amber" as Severity }, action: "Assign" },
  { avatar: "NL", name: "UMS News Live", code: "UC-NEWS-22", company: "News Network", sector: "News", cms: { text: "Inside CMS", tone: "green" as Severity }, source: "YouTube Analytics API", node: "channel:news-live", state: { text: "Approved", tone: "green" as Severity }, action: "Review" },
  { avatar: "MS", name: "Music Stage", code: "UC-MUSIC-31", company: "Catalog Media", sector: "Entertainment", cms: { text: "Unmapped", tone: "red" as Severity }, source: "Not linked", node: "pending", state: { text: "Export block", tone: "red" as Severity }, action: "Map" },
];

export const CLOSE_SUMMARY = [
  { label: "Close state", value: "Step 6/7", note: "Allocation review before lock" },
  { label: "Payment gap", value: "$31.4K", note: "Bank fee and FX confirmation pending", finance: true },
  { label: "Manual overrides", value: "3", note: "All require reviewer and reason" },
  { label: "Export blockers", value: "2", note: "Cannot finalize until accepted" },
];

export const CLOSE_DETAILS = [
  { label: "Gross revenue source", value: "YouTube official reports, checksummed raw files" },
  { label: "Payment source", value: "AdSense payment object matched to March close" },
  { label: "Allocation method", value: "Post-tax proportional gap allocation" },
  { label: "Lock policy", value: "Unlock requires reason, dual approval, and audit log" },
];

export const CLOSE_CHECKPOINTS = [
  { name: "Raw report intake", owner: "System Integration User", evidence: "raw_report_file_id", action: "View raw files", state: { text: "Done", tone: "green" as Severity }, next: "Open Log" },
  { name: "Payment reconciliation", owner: "Finance Admin", evidence: "payment_reconciliation_id", action: "View bank reconciliation", state: { text: "Review", tone: "amber" as Severity }, next: "Explain Gap" },
  { name: "Manual override approval", owner: "Corporate Admin", evidence: "override_id", action: "Create manual override", state: { text: "3 pending", tone: "amber" as Severity }, next: "Review" },
  { name: "Month lock", owner: "Finance Admin", evidence: "finance_month_id", action: "Lock or unlock month", state: { text: "Blocked", tone: "red" as Severity }, next: "Resolve" },
];

export const TRACE_SUMMARY = [
  { label: "Trace mode", value: "SQL only", note: "SQL remains the financial source of truth" },
  { label: "Visible entities", value: "146", note: "Filtered by current role and scope" },
  { label: "Hidden finance rows", value: "38", note: "Assistant and company scopes cannot open them" },
  { label: "Trace paths", value: "9", note: "Revenue issue paths for March close" },
];

export const TRACE_NODES_LARGE = [
  { id: "holding", x: 48,  y: 82,  text: "UMS Holding" },
  { id: "tv-sector", x: 248, y: 94,  text: "TV Sector" },
  { id: "united-studios", x: 388, y: 170, text: "United Studios" },
  { id: "ums-drama", x: 548, y: 126, text: "UMS Drama" },
  { id: "payment", x: 526, y: 264, text: "Payment", finance: true },
  { id: "gap-allocation", x: 300, y: 330, text: "Gap Allocation", finance: true },
  { id: "sports-extra", x: 156, y: 246, text: "Sports Extra" },
  { id: "export-row", x: 646, y: 294, text: "Export Row", finance: true },
];

export const TRACE_LINES_LARGE = [
  { id: "holding-to-tv-sector", left: 110, top: 100, width: 140, rotate: 8 },
  { id: "tv-sector-to-united-studios", left: 260, top: 116, width: 138, rotate: 29 },
  { id: "united-studios-to-payment", left: 393, top: 194, width: 132, rotate: -18 },
  { id: "tv-sector-to-gap-allocation", left: 232, top: 198, width: 122, rotate: 58 },
  { id: "gap-allocation-to-payment", left: 378, top: 326, width: 146, rotate: -24 },
  { id: "payment-to-export-row", left: 520, top: 258, width: 116, rotate: 19 },
];

export const TRACE_EVENTS = [
  { tone: "amber" as Severity, title: "Sports Extra source issue", sub: "Channel to company mapping lacks March evidence", badge: { text: "Trace", tone: "amber" as Severity } },
  { tone: "green" as Severity, title: "UMS Drama payment path", sub: "Channel, payment, allocation, and export row linked", badge: { text: "Clean", tone: "green" as Severity } },
  { tone: "red" as Severity, title: "Hidden company branch", sub: "Suppressed by company scope filter", badge: { text: "Masked", tone: "red" as Severity } },
];

export const EXPORTS_SUMMARY = [
  { label: "Export packages", value: "12", note: "Finance, executive, brand, and channel formats" },
  { label: "Ready now", value: "7", note: "Non-finance reports and approved finance workbooks" },
  { label: "Blocked", value: "2", note: "Raw appendix and unresolved source evidence" },
  { label: "Last export", value: "01:42", note: "Audit event stored with checksum" },
];

export const EXPORTS_ROWS = [
  { name: "Finance Workbook", scope: "global", audience: "Finance Admins", money: { text: "Sensitive", tone: "red" as Severity }, requires: "Locked month, revenue export permission", status: { text: "Notes due", tone: "amber" as Severity }, action: "Prepare" },
  { name: "Executive PDF", scope: "global", audience: "Corporate leadership", money: { text: "Summary", tone: "blue" as Severity }, requires: "Finance viewer or higher", status: { text: "Ready", tone: "green" as Severity }, action: "Export" },
  { name: "Brand Slide Deck", scope: "scoped", audience: "Brand partners", money: { text: "Scoped", tone: "amber" as Severity }, requires: "Company or channel export scope", status: { text: "Ready", tone: "green" as Severity }, action: "Export" },
  { name: "Raw Appendix", scope: "raw", audience: "Audit", money: { text: "Raw files", tone: "red" as Severity }, requires: "Raw report file permission", status: { text: "Blocked", tone: "red" as Severity }, action: "Request" },
];

export const EXPORT_META = [
  { label: "Scope hash", chip: "exp_8b3c41" },
  { label: "Month", value: "Mar 2026 locked" },
  { label: "Actor", value: "finance.admin@ums" },
  { label: "Retention", value: "Audit log plus generated file checksum" },
];

export const CONNECTORS_SUMMARY = [
  { label: "Connector jobs", value: "4", note: "YouTube, Analytics, Data API, AdSense" },
  { label: "Latest run", value: "00:18", note: "Reporting API completed with checksum" },
  { label: "Warnings", value: "2", note: "Quota and delayed AdSense settlement" },
  { label: "Admin access", value: "Restricted", note: "OAuth and job runs require connector permission" },
];

export const CONNECTOR_HEALTH = [
  { name: "YouTube Reporting API", note: "Latest file set normalized for March", badge: { text: "Healthy", tone: "green" as Severity } },
  { name: "YouTube Analytics API", note: "Performance metrics available without revenue for analysts", badge: { text: "Scoped", tone: "blue" as Severity } },
  { name: "AdSense Payments", note: "Settlement object linked, bank gap pending", badge: { text: "Review", tone: "amber" as Severity } },
];

export const CONNECTOR_JOBS = [
  { job: "Monthly revenue report fetch", credential: "youtube-reporting-prod", lastRun: "2026-04-02 00:18", rawFile: "raw_yt_mar_2026", action: "Run connector job", state: { text: "Complete", tone: "green" as Severity } },
  { job: "Analytics performance sync", credential: "youtube-analytics-prod", lastRun: "2026-04-02 01:10", rawFile: "raw_perf_mar_2026", action: "View raw report file", state: { text: "Complete", tone: "green" as Severity } },
  { job: "AdSense payment sync", credential: "adsense-payment-prod", lastRun: "2026-04-01 23:40", rawFile: "raw_adsense_mar_2026", action: "Manage connector OAuth", state: { text: "Delayed", tone: "amber" as Severity } },
  { job: "Channel metadata refresh", credential: "youtube-data-prod", lastRun: "2026-04-02 02:00", rawFile: "raw_meta_mar_2026", action: "Change channel mapping", state: { text: "Queued", tone: "blue" as Severity } },
];

export const CREDENTIAL_CONTROLS = [
  { tone: "green" as Severity, title: "OAuth token vault", sub: "Rotations logged and scoped to connector service accounts", badge: { text: "On", tone: "green" as Severity } },
  { tone: "amber" as Severity, title: "Quota pressure", sub: "Analytics sync should back off outside close window", badge: { text: "Watch", tone: "amber" as Severity } },
  { tone: "red" as Severity, title: "No password storage", sub: "OAuth grants only; revoked accounts disable jobs", badge: { text: "Enforced", tone: "red" as Severity } },
];

// AUDIT_EVENTS and AUDIT_SUMMARY removed: the Audit timeline is wired to
// GET /audit/events and the summary tiles to GET /audit/summary (real,
// tenant-scoped aggregate counts) via views/AuditView. The mock no longer
// carries any audit data.

// ISSUES, CLOSE_STEPS, EXPORT_READINESS, REGISTRY_SUMMARY, REGISTRY_CONTROLS,
// RECON_NOTES and EXPORTS_GUARDRAILS removed (P1.4): every panel they fed
// rendered fabricated counts, statuses and badges. The Command / Registry /
// Close / Exports views now derive their summaries from the API data they
// already load, and the panels with no real source were deleted rather than
// refilled — a screen shows a real number, an honest empty state, or nothing.
