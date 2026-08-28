import type { ViewKey, WorkflowTone } from "@/types/domain";

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

export const WORKFLOW_STEPS: Array<{ state: string; tone: WorkflowTone; label: string }> = [
  { state: "is-done", tone: "green", label: "Reports" },
  { state: "is-done", tone: "green", label: "Normalize" },
  { state: "is-done", tone: "green", label: "Payments" },
  { state: "is-current", tone: "primary", label: "Allocate" },
  { state: "", tone: "amber", label: "Lock" },
  { state: "", tone: "amber", label: "Export" },
];

export const VIEW_KEYS: ViewKey[] = [
  "command",
  "registry",
  "groups",
  "close",
  "trace",
  "exports",
  "connectors",
  "audit",
];

export function isViewKey(value: string): value is ViewKey {
  return (VIEW_KEYS as string[]).includes(value);
}
