/**
 * Success and terminal response gates for the internal C10 browser route:
 * what may be signed, and when a signed success may be serialized.
 */
import type { C10BrowserPageResponse } from "./c10_browser_execution";
import type { C10SidecarCard } from "./c10_browser_internal";

export function c10TerminalEvidence(
  evidence: Record<string, unknown> | null,
  executionFailed: boolean,
  cleanupConfirmed: boolean,
): Record<string, unknown> | null {
  // The route must never serialize a signed success while a page/context/lease
  // might remain live. A caller receives only a generic quarantine failure.
  return !executionFailed && cleanupConfirmed && evidence ? evidence : null;
}

function expectedC10ResponseContentType(card: C10SidecarCard): string {
  return card.stage === "enumeration" ? "application/json" : "text/html";
}

function normalizedContentType(value: string | null): string | null {
  if (!value) return null;
  return value.split(";", 1)[0]?.trim().toLowerCase() || null;
}

function normalizedC10MemberRoute(value: unknown, card: C10SidecarCard): string | null {
  if (typeof value !== "string") return null;
  try {
    const route = new URL(value, `https://${card.allowedHost}`);
    if (
      route.protocol !== "https:" ||
      route.host !== card.allowedHost ||
      route.search ||
      route.hash
    ) return null;
    return route.toString().replace(/\/$/, "");
  } catch {
    return null;
  }
}

/**
 * An absent or empty GraphQL `errors` array means no errors; any other value
 * (non-empty, null, or a non-array) is a failure.  Python `_verify_evidence`
 * and the JLL selection rule share this contract via golden vectors.
 */
export function hasNoC10GraphqlErrors(payload: object): boolean {
  if (!Object.prototype.hasOwnProperty.call(payload, "errors")) return true;
  const errors = (payload as { errors?: unknown }).errors;
  return Array.isArray(errors) && errors.length === 0;
}

/** Reject GraphQL transport successes that do not prove the sealed cohort is current. */
export function hasC10EnumerationMembership(
  card: C10SidecarCard,
  bodyBase64: string,
): boolean {
  if (card.stage !== "enumeration" || !Array.isArray(card.expectedMemberRoutes)) {
    return false;
  }
  try {
    const payload: unknown = JSON.parse(Buffer.from(bodyBase64, "base64").toString("utf8"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload) || !hasNoC10GraphqlErrors(payload)) {
      return false;
    }
    const data = (payload as { data?: unknown }).data;
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    const properties = (data as { properties?: unknown }).properties;
    if (!properties || typeof properties !== "object" || Array.isArray(properties)) return false;
    const items = (properties as { items?: unknown }).items;
    if (!Array.isArray(items) || items.length === 0) return false;
    const observed = new Set(
      items.map((item) => (
        item && typeof item === "object"
          ? normalizedC10MemberRoute((item as { pageUrl?: unknown }).pageUrl, card)
          : null
      )),
    );
    return card.expectedMemberRoutes.every((route) => observed.has(route));
  } catch {
    return false;
  }
}

/**
 * Admission-lane enumeration success: a well-formed native GraphQL envelope
 * with at least sixteen candidates.  Membership is recomputed and enforced by
 * the coordinator from this signed body before any member capability exists.
 */
export function hasC10AdmissionEnumerationCandidates(bodyBase64: string): boolean {
  try {
    const payload: unknown = JSON.parse(Buffer.from(bodyBase64, "base64").toString("utf8"));
    if (!payload || typeof payload !== "object" || Array.isArray(payload) || !hasNoC10GraphqlErrors(payload)) {
      return false;
    }
    const data = (payload as { data?: unknown }).data;
    if (!data || typeof data !== "object" || Array.isArray(data)) return false;
    const properties = (data as { properties?: unknown }).properties;
    if (!properties || typeof properties !== "object" || Array.isArray(properties)) return false;
    const items = (properties as { items?: unknown }).items;
    return Array.isArray(items) && items.length >= 16;
  } catch {
    return false;
  }
}

/**
 * Success evidence is intentionally stricter than transport completion. A
 * result has to be the reviewed route, status, response representation, and
 * challenge-free before the listener signs it.
 */
export function isC10SuccessfulBrowserResponse(
  card: C10SidecarCard,
  response: C10BrowserPageResponse,
  challengeDetected: boolean,
): boolean {
  return (
    response.status >= 200 &&
    response.status < 300 &&
    response.finalUrl === card.url &&
    normalizedContentType(response.contentType) ===
      expectedC10ResponseContentType(card) &&
    !challengeDetected &&
    (card.stage !== "enumeration" ||
      (card.expectedMemberRoutes === null
        ? hasC10AdmissionEnumerationCandidates(response.bodyBase64)
        : hasC10EnumerationMembership(card, response.bodyBase64)))
  );
}
