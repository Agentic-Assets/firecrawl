type ProvenanceStatus = "missing" | "invalid" | "valid";
type SourceAttribute = [string, string | number | boolean | null];

export interface BuildoutInventoryObservation {
  schema: "buildout-inventory-attributes-v1";
  attributes: SourceAttribute[];
  omittedCount: number;
  omissions: { malformed: number; sensitive: number; limit: number };
  provenance: { observedAt: ProvenanceStatus; generationId: ProvenanceStatus };
  observedAt?: string;
  generationId?: string;
}

// Validate clocks without normalization or substituting the collection time.
function validBuildoutObservationTime(value: unknown): value is string {
  if (typeof value !== "string") return false;
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.\d{1,6})?(Z|[+-]\d{2}:\d{2})$/.exec(value);
  if (!match) return false;
  const [, year, month, day, hour, minute, second, zone] = match;
  const y = Number(year), m = Number(month), d = Number(day);
  const leap = y % 4 === 0 && (y % 100 !== 0 || y % 400 === 0);
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (y < 1 || m < 1 || m > 12 || d < 1 || d > days[m - 1] ||
      Number(hour) > 23 || Number(minute) > 59 || Number(second) > 59) return false;
  if (zone !== "Z") {
    const hours = Number(zone.slice(1, 3)), minutes = Number(zone.slice(4));
    if (hours > 14 || minutes > 59 || (hours === 14 && minutes !== 0)) return false;
  }
  return true;
}

const sensitiveLabel = /(?:^|[^a-z0-9])(?:api[ _-]?key|access[ _-]?token|password|private[ _-]?key|session[ _-]?(?:id|token)|cookie|credential|authorization|secret)(?=$|[^a-z0-9])/i;
const sensitiveQueryKey = /^(?:token|access[_-]?token|api[_-]?key|signature|sig|password|secret|session|access[_-]?key(?:[_-]?id)?|awsaccesskeyid|googleaccessid|x-(?:amz|goog)-(?:signature|credential|security[_-]?token))$/i;

// Decode query keys before checking them, including URLs embedded in prose.
// Malformed URLs and query escapes cannot be verified safe and are omitted.
function containsSensitiveValue(value: string): boolean {
  if (/\bauthorization\s*:\s*(?:basic|bearer)\s+\S+|\b(?:set-)?cookie\s*:/i.test(value)) return true;
  // Bare Bearer prose is ambiguous. This best-effort filter cannot guarantee
  // that arbitrary credentials are detected; apply an egress review separately.
  for (const match of value.matchAll(/\bbasic\s+([a-z0-9+/]{4,}={0,2})(?=$|[\s,;])/gi)) {
    const token = match[1];
    const decoded = Buffer.from(token, "base64");
    if (decoded.toString("base64").replace(/=+$/, "") === token.replace(/=+$/, "") && decoded.includes(0x3a)) return true;
  }
  for (const match of value.matchAll(/https?:\/\/[^\s<>"']+/gi)) {
    try {
      const url = new URL(match[0]);
      if (url.username || url.password) return true;
      for (const part of [url.search.slice(1), url.hash.slice(1).replace(/\?/g, "&")]) {
        if (/%(?![0-9a-f]{2})/i.test(part)) return true;
        for (const component of part.split("&")) decodeURIComponent(component.replace(/\+/g, " "));
        for (const [key] of new URLSearchParams(part)) {
          let decoded = key;
          for (let i = 0; i < 4; i++) {
            if (sensitiveQueryKey.test(decoded)) return true;
            if (!/%[0-9a-f]{2}/i.test(decoded)) break;
            decoded = decodeURIComponent(decoded);
          }
          // Deeper encoded names cannot be safely classified.
          if (sensitiveQueryKey.test(decoded) || /%[0-9a-f]{2}/i.test(decoded)) return true;
        }
      }
    } catch { return true; }
  }
  return false;
}

// Keep literal public card attributes separate from interpreted listing fields.
// This is deliberately not a copy of the provider object: credentials, nested
// data and operational metadata do not belong in this observation.
export function buildoutInventoryObservation(
  attributes: unknown[], freshness?: { observedAt?: unknown; generationId?: unknown },
): BuildoutInventoryObservation;
export function buildoutInventoryObservation(
  attributes: unknown, freshness?: { observedAt?: unknown; generationId?: unknown },
): BuildoutInventoryObservation | undefined;
export function buildoutInventoryObservation(
  attributes: unknown, freshness?: { observedAt?: unknown; generationId?: unknown },
): BuildoutInventoryObservation | undefined {
  if (!Array.isArray(attributes)) return undefined;
  const retained: SourceAttribute[] = [];
  const omissions = { malformed: 0, sensitive: 0, limit: 0 };
  let retainedBytes = 0;
  for (const pair of attributes) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== "string" || !pair[0].trim() ||
        !(pair[1] === null || typeof pair[1] === "boolean" ||
          (typeof pair[1] === "number" && Number.isFinite(pair[1])) || typeof pair[1] === "string")) {
      omissions.malformed++;
      continue;
    }
    const value = pair[1];
    if (sensitiveLabel.test(pair[0]) || containsSensitiveValue(pair[0]) ||
        (typeof value === "string" && containsSensitiveValue(value))) {
      omissions.sensitive++;
      continue;
    }
    // Reserve space for envelope metadata and Python JSON separators.
    const pairBytes = Buffer.byteLength(JSON.stringify(pair), "utf8") + 4;
    if (retained.length >= 100 || pair[0].length > 256 ||
        (typeof value === "string" && value.length > 2048) || retainedBytes + pairBytes > 240_000) {
      omissions.limit++;
      continue;
    }
    if (/[\x00-\x1f\x7f]/.test(pair[0]) ||
        (typeof value === "string" && /[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]/.test(value))) {
      omissions.malformed++;
      continue;
    }
    retained.push([pair[0], value]);
    retainedBytes += pairBytes;
  }
  // Generation IDs are opaque identifiers, limited to this safe ASCII grammar.
  const observedAt = freshness?.observedAt;
  const generationId = freshness?.generationId;
  const validTime = validBuildoutObservationTime(observedAt);
  const validGeneration = typeof generationId === "string" &&
    /^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$/.test(generationId) &&
    !sensitiveLabel.test(generationId) && !containsSensitiveValue(generationId);
  const status = (value: unknown, valid: boolean): ProvenanceStatus =>
    value === undefined || value === null ? "missing" : valid ? "valid" : "invalid";
  return {
    schema: "buildout-inventory-attributes-v1",
    attributes: retained,
    omittedCount: omissions.malformed + omissions.sensitive + omissions.limit,
    omissions,
    provenance: { observedAt: status(observedAt, validTime), generationId: status(generationId, validGeneration) },
    ...(validTime ? { observedAt } : {}),
    ...(validGeneration ? { generationId } : {}),
  };
}

// prune removes false, null and empty strings. Restore only this bounded source
// observation, leaving the shared pruning contract unchanged.
export function preserveBuildoutInventoryObservation(listing: any, observation: any): void {
  if (listing && observation) listing.buildoutInventoryObservation = observation;
}
