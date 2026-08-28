// Test-only re-export barrel. Production components must import @/fixtures/* or API types.
export * from "@/fixtures/snapshotPanels";
export type { Severity, WorkflowTone, ViewKey, Role } from "@/types/domain";
export { VIEW_COPY, NAV_GROUPS, WORKFLOW_STEPS } from "@/config/navigation";
