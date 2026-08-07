import { type ReactNode, useRef, useState } from "react";

import { ApiError } from "@/lib/api/client";
import type {
  ChannelGroupApiEntry,
  ConnectorCredential,
  ConnectorCredentialListResponse,
} from "@/lib/api/types";
import { useConnectorCredentials } from "@/lib/api/useConnectors";
import { useGroups } from "@/lib/api/useGroups";
import {
  useClearOwnerStampAction,
  useGroupArchiveAction,
} from "@/lib/api/useGroupSync";
import { GroupsSyncFlow, isValidAuditReason } from "./GroupsSyncFlow";

// ============================================================================
// Purpose: The REAL-data Channel Groups screen (import/sync arc, PR-A) — the
//   steady-state table wired to GET /groups via useGroups: one row per group
//   with its CMS group id ("manual" when NULL), owner stamp (a distinct
//   "unstamped" marker when NULL = adoptable), member count, and active status.
//   Two audited row actions are live, each an inline confirm panel with a
//   required reason: Clear stamp -> DELETE /groups/{id}/content-owner?reason=…
//   (#170 owner-recovery; shown only when a stamp exists) and Archive/Restore
//   -> PATCH /groups/{id} {active, reason} (the one manual edit the synced-group
//   lockdown permits). The panel header also carries the credential-fed
//   content-owner picker + a Sync (dry-run) button that opens the sync stepper
//   (GroupsSyncFlow), which replaces the table while active.
// Database/ORM: None (frontend) — consumes GET /groups (VIEW_ANALYTICS per
//   member) + the two MANAGE_GROUPS-gated, audited write routes above.
// Standards: No client-side authorization is invented. canManageGroups gates the
//   row-action affordances (fail-closed; buttons hidden, not disabled); the
//   backend checks stay the authority and 4xx bodies surface inline verbatim
//   (house rule: canned backend messages are operator-facing). Mutations use the
//   synchronous in-flight ref latch (one request per click burst -> one audit
//   event) and reload on success. Reason is required + trimmed and client-blocked
//   on a NUL, mirroring the backend audit_logs 422 so a knowably-invalid reason
//   never round-trips.
// Blast Radius: Channel-group ownership/active-state (two audited write paths).
//   No finance number computation; no mutation outside the wired routes.
// Connections:
//   - File: frontend/src/lib/api/useGroups.ts -> GET /groups list.
//   - File: frontend/src/lib/api/useGroupSync.ts -> clear-stamp + archive actions.
//   - File: frontend/src/lib/api/useConnectors.ts -> credential list (owner picker).
//   - File: frontend/src/components/srcc/views/GroupsSyncFlow.tsx -> sync stepper.
//   - File: backend/ums_smart_revenue/api/groups.py -> the group routes.
//   - File: Docs/superpowers/specs/2026-08-07-import-sync-ui-design.md -> spec.
// ============================================================================

// Only one panel is open at a time; opening another row's action replaces it.
type PanelTarget = { groupId: string; kind: "clear" | "archive" };

/** Map a mutation failure to operator-facing copy — backend detail verbatim. */
const describeMutationError = (err: unknown): string => {
  if (err instanceof ApiError) {
    const body = err.body as { detail?: unknown } | null;
    return String(body?.detail ?? err.message);
  }
  return err instanceof Error ? err.message : String(err);
};

// ---- Root component ---------------------------------------------------------

/**
 * The real-data channel groups view. useGroups() is called ONCE here and threaded
 * down. Row actions set the single open-panel target ({groupId, kind}); opening
 * another closes the previous. Successful mutations reload the group list.
 */
export function GroupsView({ canManageGroups }: { canManageGroups: boolean }) { // skipcq: JS-0067
  const groupState = useGroups();
  const [panel, setPanel] = useState<PanelTarget | null>(null);
  // The picker selection (a content owner's account id) + whether the sync
  // stepper is open. Both live here: the stepper needs the selected owner, and
  // opening it replaces the table. Only a manager renders the header controls, so
  // syncOpen can only ever become true for a manager.
  const [selectedOwnerId, setSelectedOwnerId] = useState("");
  const [syncOpen, setSyncOpen] = useState(false);

  return (
    <section className="view-page" aria-labelledby="groupsTitle">
      <section className="panel">
        {canManageGroups ? (
          <GroupsSyncHeader
            selectedOwnerId={selectedOwnerId}
            onSelectOwner={setSelectedOwnerId}
            onStartSync={() => {
              setPanel(null);
              setSyncOpen(true);
            }}
            syncDisabled={syncOpen}
          />
        ) : (
          <GroupsPanelHeader />
        )}
        {syncOpen ? (
          <GroupsSyncFlow
            contentOwnerId={selectedOwnerId}
            onCancel={() => setSyncOpen(false)}
            onDone={() => {
              setSyncOpen(false);
              groupState.reload();
            }}
          />
        ) : (
          <GroupsTable
            canManageGroups={canManageGroups}
            groupState={groupState}
            panel={panel}
            onOpenPanel={setPanel}
            onClosePanel={() => setPanel(null)}
            onMutated={groupState.reload}
          />
        )}
      </section>
    </section>
  );
}

/** Read-only groups header (no sync controls): title + subtitle only. Rendered
 * for a viewer who cannot manage groups, so the sync affordance stays hidden. */
function GroupsPanelHeader() { // skipcq: JS-0067
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="groupsTitle">Channel Groups</strong>
        <span>CMS-mirrored groups, their content-owner stamp, and membership</span>
      </div>
    </div>
  );
}

// The credential connector_key the group sync reads a content owner from. Mirrors
// backend YOUTUBE_ANALYTICS_CONNECTOR ("youtube-analytics", connectors/keys.py):
// the sync's content owner comes from a youtube-analytics credential's account_id.
const YOUTUBE_ANALYTICS_KEY = "youtube-analytics";

/**
 * The pickable content owners: ACTIVE youtube-analytics credentials only. Status
 * is compared case-insensitively against the "ACTIVE" literal the credential rows
 * carry (matching ConnectorsView's credentialStatusTone idiom); account_id is the
 * content owner id the sync targets.
 */
function activeAnalyticsOwners(
  list: ConnectorCredentialListResponse | null,
): ConnectorCredential[] {
  if (!list) return [];
  return list.items.filter(
    (credential) =>
      credential.connector_key === YOUTUBE_ANALYTICS_KEY &&
      credential.status.toUpperCase() === "ACTIVE",
  );
}

/** Short muted note under the picker for its non-ready states (or null). */
function pickerNote(
  state: ReturnType<typeof useConnectorCredentials>,
  owners: ConnectorCredential[],
): string | null {
  if (state.error) return "Couldn't load connector credentials.";
  if (!state.data) return "Loading credentials…";
  if (owners.length === 0) {
    return "Register a youtube-analytics credential in Connectors first.";
  }
  return null;
}

type GroupsSyncHeaderProps = {
  selectedOwnerId: string;
  onSelectOwner: (ownerId: string) => void;
  onStartSync: () => void;
  syncDisabled: boolean;
};

/**
 * The manager-only panel header: title + subtitle, plus the content-owner picker
 * (fed by the ACTIVE youtube-analytics credentials) and the Sync (dry-run) button
 * that opens the stepper. Owns the credential fetch, so a read-only viewer (who
 * gets GroupsPanelHeader instead) never fires it. The picker degrades gracefully
 * while credentials load, on error, and when none exist (disabled + a short muted
 * note pointing at Connectors). Sync is disabled without a selection or while the
 * stepper is already open; per the fail-closed house rule it renders only for a
 * manager (this component), never as a disabled control for a read-only viewer.
 */
function GroupsSyncHeader({ // skipcq: JS-0067
  selectedOwnerId,
  onSelectOwner,
  onStartSync,
  syncDisabled,
}: GroupsSyncHeaderProps) {
  const credentialState = useConnectorCredentials();
  const owners = activeAnalyticsOwners(credentialState.data);
  const note = pickerNote(credentialState, owners);
  return (
    <div className="panel-header">
      <div className="panel-title">
        <strong id="groupsTitle">Channel Groups</strong>
        <span>CMS-mirrored groups, their content-owner stamp, and membership</span>
      </div>
      <div className="control-row">
        <select
          className="control"
          aria-label="Content owner"
          value={selectedOwnerId}
          disabled={owners.length === 0}
          onChange={(event) => onSelectOwner(event.target.value)}
        >
          <option value="">Select a content owner…</option>
          {owners.map((credential) => (
            <option key={credential.id} value={credential.account_id}>
              {credential.account_id}
            </option>
          ))}
        </select>
        <button
          className="primary-button"
          type="button"
          disabled={!selectedOwnerId || syncDisabled}
          onClick={onStartSync}
        >
          Sync (dry-run)
        </button>
      </div>
      {note ? <span className="muted">{note}</span> : null}
    </div>
  );
}

type GroupAsyncState = ReturnType<typeof useGroups>;

type GroupTableProps = {
  canManageGroups: boolean;
  groupState: GroupAsyncState;
  panel: PanelTarget | null;
  onOpenPanel: (target: PanelTarget) => void;
  onClosePanel: () => void;
  onMutated: () => void;
};

/** Column header row. Extracted to keep nesting shallow. */
function GroupsTableHead() { // skipcq: JS-0067
  return (
    <thead>
      <tr>
        <th>Name</th><th>CMS id</th><th>Owner</th>
        <th>Members</th><th>Status</th><th>Actions</th>
      </tr>
    </thead>
  );
}

/** Single-row message cell inside the groups table shell (loading, error, empty). */
function GroupsTableMessageRow({ title, sub }: { title: string; sub: string }) { // skipcq: JS-0067
  return (
    <tr>
      <td colSpan={6}>
        <span className="item-title">{title}</span>
        <span className="item-sub">{sub}</span>
      </td>
    </tr>
  );
}

/** Error message row: 403 -> no-permission copy; else status + verbatim detail. */
function groupsErrorRow(error: unknown) { // skipcq: JS-0067
  const isApi = error instanceof ApiError;
  const status = isApi ? error.status : null;
  if (status === 403) {
    return (
      <GroupsTableMessageRow
        title="No permission"
        sub="Your role cannot view channel groups."
      />
    );
  }
  return (
    <GroupsTableMessageRow
      title={`Failed to load groups${status ? ` (${status})` : ""}`}
      sub={describeMutationError(error)}
    />
  );
}

/**
 * Channel groups data table. Handles loading, error (403 -> no-permission row),
 * empty, and loaded states, mirroring RegistryTable. Renders the bespoke group
 * rows (OutcomeTable is for the sync diff, not this steady-state table).
 */
function GroupsTable({ // skipcq: JS-0067
  canManageGroups,
  groupState,
  panel,
  onOpenPanel,
  onClosePanel,
  onMutated,
}: GroupTableProps) {
  const { data: groups, error } = groupState;

  if (error) {
    return <GroupsTableShell role="alert">{groupsErrorRow(error)}</GroupsTableShell>;
  }

  if (!groups) {
    return (
      <GroupsTableShell busy>
        <GroupsTableMessageRow
          title="Loading groups…"
          sub="Reading the channel-group registry"
        />
      </GroupsTableShell>
    );
  }

  if (groups.length === 0) {
    return (
      <GroupsTableShell>
        <GroupsTableMessageRow
          title="No groups yet"
          sub="Import a roster, then sync your content owner's groups."
        />
      </GroupsTableShell>
    );
  }

  return (
    <GroupsTableShell>
      {groups.map((group) => (
        <GroupRow
          key={group.id}
          group={group}
          canManageGroups={canManageGroups}
          panelOpen={panel?.groupId === group.id ? panel.kind : null}
          onOpenPanel={onOpenPanel}
          onClosePanel={onClosePanel}
          onMutated={onMutated}
        />
      ))}
    </GroupsTableShell>
  );
}

/** The table-wrap + labelled table + header shell shared by every table state. */
function GroupsTableShell({ // skipcq: JS-0067
  role,
  busy,
  children,
}: {
  role?: string;
  busy?: boolean;
  children: ReactNode;
}) {
  return (
    <div className="table-wrap" role={role} aria-busy={busy ? "true" : undefined}>
      <table aria-label="Channel groups">
        <GroupsTableHead />
        <tbody>{children}</tbody>
      </table>
    </div>
  );
}

type GroupRowProps = {
  group: ChannelGroupApiEntry;
  canManageGroups: boolean;
  panelOpen: PanelTarget["kind"] | null;
  onOpenPanel: (target: PanelTarget) => void;
  onClosePanel: () => void;
  onMutated: () => void;
};

/**
 * A single channel-group row. Derives every display cell from the API shape and,
 * when its action panel is open, renders the inline confirm panel as a full-width
 * row directly beneath it (component-local; no portal/modal).
 */
function GroupRow({ // skipcq: JS-0067
  group,
  canManageGroups,
  panelOpen,
  onOpenPanel,
  onClosePanel,
  onMutated,
}: GroupRowProps) {
  const memberCount = group.channel_ids.length;
  return (
    <>
      <tr>
        <td>{group.name}</td>
        <td>
          {group.cms_group_id === null ? (
            <span className="muted">manual</span>
          ) : (
            <span className="code-chip">{group.cms_group_id}</span>
          )}
        </td>
        <td>
          {group.content_owner_id === null ? (
            // Distinct marker: a NULL stamp is adoptable by the next sync.
            <span className="badge amber" data-testid="owner-unstamped">
              unstamped
            </span>
          ) : (
            <span className="code-chip">{group.content_owner_id}</span>
          )}
        </td>
        <td>{memberCount}</td>
        <td>
          {group.active ? "active" : <span className="muted">archived</span>}
        </td>
        <td>
          {canManageGroups ? (
            <GroupRowActions
              group={group}
              onOpenPanel={onOpenPanel}
            />
          ) : null}
        </td>
      </tr>
      {canManageGroups && panelOpen ? (
        <tr>
          <td colSpan={6}>
            <GroupActionPanel
              group={group}
              kind={panelOpen}
              onClose={onClosePanel}
              onMutated={onMutated}
            />
          </td>
        </tr>
      ) : null}
    </>
  );
}

/** The row's action buttons: Clear stamp (only when stamped) + Archive/Restore. */
function GroupRowActions({ // skipcq: JS-0067
  group,
  onOpenPanel,
}: {
  group: ChannelGroupApiEntry;
  onOpenPanel: (target: PanelTarget) => void;
}) {
  return (
    <span className="row-actions">
      {group.content_owner_id !== null ? (
        <button
          className="mini-button"
          type="button"
          onClick={() => onOpenPanel({ groupId: group.id, kind: "clear" })}
        >
          Clear stamp
        </button>
      ) : null}
      <button
        className="mini-button"
        type="button"
        onClick={() => onOpenPanel({ groupId: group.id, kind: "archive" })}
      >
        {group.active ? "Archive" : "Restore"}
      </button>
    </span>
  );
}

/** Static copy per panel kind: title, explanatory line, error-banner heading. */
function panelCopy(
  kind: PanelTarget["kind"],
  active: boolean,
): { title: string; explanation: string; failTitle: string } {
  if (kind === "clear") {
    return {
      title: "Clear owner stamp",
      explanation:
        "Erases the owner stamp; the correct owner's next sync re-adopts the group.",
      failTitle: "Clear stamp failed",
    };
  }
  if (active) {
    return {
      title: "Archive group",
      explanation:
        "Archives the group. Members and history are preserved; sync stops maintaining it until you restore it.",
      failTitle: "Archive failed",
    };
  }
  return {
    title: "Restore group",
    explanation:
      "Restores the group to active; the next sync will maintain its membership again.",
    failTitle: "Restore failed",
  };
}

/**
 * Inline confirm panel for a single row action (clear stamp OR archive/restore).
 * Component-local reason + busy + error state. Confirm is disabled while the
 * reason is blank or carries a NUL (the backend 422-rejects NUL in audit
 * reasons). A same-tick double-click is dropped by the in-flight ref latch so a
 * click burst records one audit event. On success it reloads the list and closes;
 * an ApiError surfaces inline (backend detail verbatim) with the panel kept open.
 */
function GroupActionPanel({ // skipcq: JS-0067
  group,
  kind,
  onClose,
  onMutated,
}: {
  group: ChannelGroupApiEntry;
  kind: PanelTarget["kind"];
  onClose: () => void;
  onMutated: () => void;
}) {
  const [reason, setReason] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inFlightRef = useRef(false);
  const clearStamp = useClearOwnerStampAction();
  const archive = useGroupArchiveAction();

  const { title, explanation, failTitle } = panelCopy(kind, group.active);
  const trimmedReason = reason.trim();
  // Mirror the backend audit_logs contract: reason required + no NUL (422).
  const reasonValid = isValidAuditReason(reason);
  const canSubmit = reasonValid && !busy;

  /** Run the clear/archive mutation; latched against concurrent double-clicks. */
  const submit = async () => {
    if (!canSubmit) return;
    // Synchronous latch: both clicks of a same-tick double-click run off the
    // same render, so state alone cannot drop the second request.
    if (inFlightRef.current) return;
    inFlightRef.current = true;
    setBusy(true);
    setError(null);
    try {
      if (kind === "clear") {
        await clearStamp({ groupId: group.id, reason: trimmedReason });
      } else {
        await archive({
          groupId: group.id,
          active: !group.active,
          reason: trimmedReason,
        });
      }
      onMutated();
      onClose();
    } catch (caught) {
      setError(describeMutationError(caught));
      // Panel state survives so the operator can correct and retry.
    } finally {
      setBusy(false);
      inFlightRef.current = false;
    }
  };

  return (
    <div className="confirm-panel" role="group" aria-label={title}>
      <div className="panel-title">
        <strong>{title}</strong>
        <span>{explanation}</span>
      </div>
      <div className="field-row">
        <label htmlFor="groupActionReason">Reason (required, audited)</label>
        <input
          id="groupActionReason"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Why this change"
        />
      </div>
      {error ? (
        <div className="form-error" role="alert">
          <strong>{failTitle}</strong>
          <span>{error}</span>
        </div>
      ) : null}
      <div className="action-row">
        <button className="ghost-button" type="button" onClick={onClose}>
          Cancel
        </button>
        <button
          className="primary-button"
          type="button"
          disabled={!canSubmit}
          onClick={submit}
        >
          {busy ? "Working…" : "Confirm"}
        </button>
      </div>
    </div>
  );
}
