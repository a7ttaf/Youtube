import type { ViewKey } from "@/types/domain";

// ============================================================================
// Purpose: Keep route labels and surface grouping presentation-only and keyed
//          to the typed ViewKey union; navigation never carries finance counts.
// Database/ORM: None; authoritative values are loaded by each routed view.
// Standards: One canonical key list feeds route validation, sidebar buttons,
//            and page copy without static status, currency, or KPI fixtures.
// Blast Radius: Client navigation and labels only; no API or finance state.
// Connections:
//   - File: frontend/src/router/AppRouter.tsx -> validates route segments.
//   - File: frontend/src/components/srcc/AppShell.tsx -> renders navigation.
//   - File: frontend/src/components/srcc/views/* -> owns API-backed values.
// ============================================================================
export const VIEW_COPY: Record<ViewKey, { title: string; subtitle: string }> = {
  command: {
    title: "Revenue Command Center",
    subtitle: "Net revenue, payment reconciliation, and open issues for the selected month",
  },
  registry: {
    title: "Channel Registry",
    subtitle: "Channel ownership, CMS status, company scope, and roster import",
  },
  groups: {
    title: "CMS Groups",
    subtitle: "Content-owner group mirror, ownership stamps, and sync",
  },
  close: {
    title: "Month Close Workbench",
    subtitle: "Close readiness, lock and unlock controls, and the audited reason trail",
  },
  trace: {
    title: "SQL Trace Explorer",
    subtitle: "Per-channel number explanation, filtered by your read permissions",
  },
  exports: {
    title: "Export Center",
    subtitle: "Permission-gated export requests and the artifacts they generate",
  },
  connectors: {
    title: "Connector Operations",
    subtitle: "Credentials, run history, and permission-gated job controls",
  },
  audit: {
    title: "Audit Log",
    subtitle:
      "Sensitive action trace for revenue, exports, overrides, connectors, and lineage reads",
  },
};

export const NAV_GROUPS: ReadonlyArray<{
  label: string;
  items: ReadonlyArray<{ key: ViewKey; label: string; icon: string }>;
}> = [
  {
    label: "Workspace",
    items: [
      { key: "command", label: "Command Center", icon: "command" },
      { key: "registry", label: "Channel Registry", icon: "registry" },
      { key: "groups", label: "CMS Groups", icon: "groups" },
      { key: "close", label: "Month Close", icon: "close" },
      { key: "trace", label: "Trace Explorer", icon: "trace" },
    ],
  },
  {
    label: "Operations",
    items: [
      { key: "exports", label: "Exports", icon: "exports" },
      { key: "connectors", label: "Connectors", icon: "connectors" },
      { key: "audit", label: "Audit Log", icon: "audit" },
    ],
  },
];

export const VIEW_KEYS: ReadonlyArray<ViewKey> = NAV_GROUPS.flatMap((group) =>
  group.items.map((item) => item.key),
);

/** Return true only for a view key declared in the canonical navigation map. */
export const isViewKey = (value: string): value is ViewKey => {
  return (VIEW_KEYS as string[]).includes(value);
};
