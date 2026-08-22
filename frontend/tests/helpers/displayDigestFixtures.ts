import { computeDisplayDigest } from "@/lib/displayDigest";
import type { ChannelImportResult } from "@/lib/api/types";

/** Attach a server-matching display_digest to a plan fixture for tests. */
export const withDisplayDigest = (
  plan: Omit<ChannelImportResult, "display_digest"> & { display_digest?: string },
): ChannelImportResult => ({
  ...plan,
  display_digest: computeDisplayDigest(plan),
});

const hasPlanShape = (body: Record<string, unknown>): boolean =>
  Array.isArray(body.rows) &&
  body.counts !== undefined &&
  typeof body.content_owner_id === "string" &&
  typeof body.cms_status === "string";

export const isImportPlanPayload = (body: unknown): body is ChannelImportResult => {
  if (!body || typeof body !== "object") {
    return false;
  }
  if ("detail" in body) {
    return false;
  }
  return hasPlanShape(body as Record<string, unknown>);
};

const recomputeProvidedDigest = (plan: ChannelImportResult): ChannelImportResult => {
  if (typeof plan.display_digest !== "string" || plan.display_digest === "") {
    // Deliberate: a missing or empty digest is left as-is so tests can
    // exercise the SPA's fail-closed missing-digest path; this helper only
    // repairs an INCORRECT digest that a mock body already provides.
    return plan;
  }
  try {
    return withDisplayDigest(plan);
  } catch {
    return plan;
  }
};

const normalizeDetailWrapper = (body: Record<string, unknown>): unknown => {
  const detail = body.detail;
  if (!isImportPlanPayload(detail)) {
    return body;
  }
  return { ...body, detail: recomputeProvidedDigest(detail) };
};

const normalizeImportPlanBody = (body: object): unknown => {
  if (isImportPlanPayload(body)) {
    return recomputeProvidedDigest(body);
  }
  if ("detail" in body) {
    return normalizeDetailWrapper(body as Record<string, unknown>);
  }
  return body;
};

/**
 * Normalize mock import-plan bodies so display_digest matches disclosed fields.
 * A missing or empty digest is left as-is — the SPA's verify gate fail-closes
 * on it; passing `trustDigest` skips normalization entirely.
 */
export const normalizePlanBody = (body: unknown, trustDigest?: boolean): unknown => {
  if (trustDigest) {
    return body;
  }
  if (!body || typeof body !== "object") {
    return body;
  }
  return normalizeImportPlanBody(body);
};

export const importPlanJsonResponse = (
  body: unknown,
  status = 200,
  options?: { trustDigest?: boolean },
): Response => {
  const payload = normalizePlanBody(body, options?.trustDigest);
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json" },
  });
};
