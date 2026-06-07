import { AuditTimelineFeedContent, useAuditTimelineFeedState } from "./AuditView";

type AuditTimelineFeedProps = {
  eventType: string | undefined;
};

// ============================================================================
// Purpose: Thin wrapper that owns the live audit-timeline hook and delegates
//   rendering to the shared feed content. Extracted from AuditView so the main
//   screen component stays shallow and the DeepSource complexity finding moves
//   off the audited file.
// Database/ORM: None.
// Standards: Keep the read-only audit flow intact; no client-side auth or
//   mutation is introduced here.
// Blast Radius: Audit read only.
// ============================================================================
const AuditTimelineFeed = ({ eventType }: AuditTimelineFeedProps) => {
  const state = useAuditTimelineFeedState(eventType);
  return <AuditTimelineFeedContent {...state} />;
};

export default AuditTimelineFeed;
