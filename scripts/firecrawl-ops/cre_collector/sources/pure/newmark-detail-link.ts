import { newmarkNimCanonicalIdentity } from "./newmark-nim-identity.js";
import type { NewmarkPropertyWitness } from "./newmark-rcm-detail.js";

/** Caller-supplied inventory witness; neither publisher nor freshness is authenticated. */
export type NewmarkDetailWitness = NewmarkPropertyWitness & {
  status: "observed";
  schema: "newmark-nim-link-witness/v1";
  sourceKey: "newmark";
  sourcePath: "raw_data.rawNewmarkNimRecord";
  recordId: string;
  slug: string;
  url: string;
  inventoryObservedAt: string | null;
};

export type NewmarkDetailResolution =
  | { state: "unresolved" }
  | { state: "resolved"; pageUrl: string; claimUrl: string; sourceClock: string | null;
      identity: "unverified"; sourceClockStatus: "unverified" };

const WITNESS_KEYS = ["status", "schema", "sourceKey", "sourcePath", "recordId", "slug", "url", "inventoryObservedAt", "sourceName", "sourceProperty"];
const ASCII_ID = /^[A-Za-z0-9._~-]{1,256}$/;
// Exact witnessed public landing route, with a single opaque pv value. This is
// stricter than the general URL guards: no userinfo, ports, other parameters,
// fragments, whitespace, credential query keys, host aliases, or URL rewriting.
const DETAIL_URL = /^https:\/\/my\.rcm1\.com\/handler\/modern\.aspx\?pv=[A-Za-z0-9_-]{1,512}$/;

function sourceField(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const field = value as Record<string, unknown>;
  if (["missing", "null", "unresolved"].includes(String(field.state))) return Object.keys(field).length === 1;
  return field.state === "value" && Object.keys(field).length === 2 && typeof field.value === "string" &&
    Buffer.byteLength(field.value, "utf8") <= 256 && !/\p{Cc}/u.test(field.value);
}
function propertyWitness(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const property = value as Record<string, unknown>;
  if (Object.keys(property).length !== 2 || !property.fields || typeof property.fields !== "object" || Array.isArray(property.fields)) return false;
  const fields = property.fields as Record<string, unknown>;
  if (property.selection === "unresolved") return Object.keys(fields).length === 0;
  const keys = ["address", "city", "stateAbbreviation", "stateDescription", "zip", "countryCode"];
  return property.selection === "single" && Object.keys(fields).length === keys.length && keys.every(key => Object.hasOwn(fields, key) && sourceField(fields[key]));
}

function utcClock(value: unknown): value is string | null {
  if (value === null) return true;
  if (typeof value !== "string" || value.startsWith("0000-") || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:\.\d{1,6})?Z$/.test(value)) return false;
  const instant = Date.parse(value);
  return Number.isFinite(instant) && new Date(instant).toISOString().slice(0, 19) === value.slice(0, 19);
}

/** Validate an unverified source relationship without fetching or admitting it. */
export function resolveNewmarkDetailLink(claim: { sourceKey?: unknown; externalId?: unknown; url?: unknown; sourcePublishedDetail?: unknown }): NewmarkDetailResolution {
  const unresolved: NewmarkDetailResolution = { state: "unresolved" };
  const input = claim?.sourcePublishedDetail;
  if (claim?.sourceKey !== "newmark" || typeof claim.externalId !== "string" || typeof claim.url !== "string" ||
      !input || typeof input !== "object" || Array.isArray(input)) return unresolved;
  const witness = input as Record<string, unknown>;
  if (Object.keys(witness).length !== WITNESS_KEYS.length || WITNESS_KEYS.some(key => !Object.hasOwn(witness, key)) ||
      witness.status !== "observed" || witness.schema !== "newmark-nim-link-witness/v1" ||
      witness.sourceKey !== "newmark" || witness.sourcePath !== "raw_data.rawNewmarkNimRecord" ||
      typeof witness.recordId !== "string" || !ASCII_ID.test(witness.recordId) ||
      typeof witness.slug !== "string" || !ASCII_ID.test(witness.slug) ||
      typeof witness.url !== "string" || !DETAIL_URL.test(witness.url) || !utcClock(witness.inventoryObservedAt) ||
      !sourceField(witness.sourceName) || !propertyWitness(witness.sourceProperty)) return unresolved;
  try {
    const identity = newmarkNimCanonicalIdentity({ slug: witness.slug, externalWebsiteUrl: witness.url });
    if (identity.id !== claim.externalId || identity.url !== claim.url) return unresolved;
  } catch { return unresolved; }
  return { state: "resolved", pageUrl: witness.url, claimUrl: claim.url, sourceClock: witness.inventoryObservedAt,
    identity: "unverified", sourceClockStatus: "unverified" };
}
