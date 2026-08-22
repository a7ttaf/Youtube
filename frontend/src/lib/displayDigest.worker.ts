import { computeDisplayDigestFromFields, type DisplayDigestPlanFields } from "@/lib/displayDigestCore";

type DigestWorkerRequest = {
  id: number;
  plan: DisplayDigestPlanFields;
};

type DigestWorkerResponse =
  | { id: number; digest: string }
  | { id: number; error: string };

// ============================================================================
// Purpose: Off-main-thread display_digest compute for import plan verification.
// Database/ORM: None (Web Worker) — delegates to displayDigestCore recipe.
// Standards: Structured postMessage responses; errors returned as strings so
//   the orchestrator can reject pending waiters and fall back to sync digest.
// Blast Radius: Import preview/apply trust boundary (review #184, C1).
// Connections:
//   - File: frontend/src/lib/displayDigest.ts -> Worker spawn and message routing.
//   - File: frontend/src/lib/displayDigestCore.ts -> Canonical JSON + SHA-256.
//   - File: backend/ums_smart_revenue/api/channels.py -> Server-side digest recipe.
// ============================================================================
self.onmessage = (event: MessageEvent<DigestWorkerRequest>) => {
  try {
    const digest = computeDisplayDigestFromFields(event.data.plan);
    const response: DigestWorkerResponse = { id: event.data.id, digest };
    self.postMessage(response);
  } catch (error) {
    const response: DigestWorkerResponse = {
      id: event.data.id,
      error: error instanceof Error ? error.message : String(error),
    };
    self.postMessage(response);
  }
};
