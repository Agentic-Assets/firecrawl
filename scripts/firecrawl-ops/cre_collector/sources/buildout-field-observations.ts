// Current collection metadata only, not a history store or canonical update.
// Publisher identity, freshness, rights, and disclosure remain unverified.
const SPACE_LITERAL = /^\s*([0-9]{1,10}) +Spaces?\s*$/;
const SAFE_TEXT = /^[a-z0-9 .,/\-]+$/i;
const SENSITIVE_TEXT = /(?:bearer|secret|token|password|credential|cookie|authorization|api[ _-]?key)/i;
export const MAX_BUILDOUT_FIELD_ATTRIBUTES = 1000;

function countLiteral(value: unknown): number | null {
  if (typeof value !== "string" || value.length > 64) return null;
  const match = SPACE_LITERAL.exec(value);
  if (!match) return null;
  const number = Number(match[1]);
  return number <= 2_147_483_647 ? number : null;
}

export function safeBuildoutSpaceLiteral(value: unknown): value is string {
  return typeof value === "string" && value.length > 0 && value.length <= 64 &&
    SAFE_TEXT.test(value) && !SENSITIVE_TEXT.test(value);
}

type FieldObservation = {
  schema: string; sourceIdentity: string; freshness: string; promotion: string;
  availableSpaceCount: { state: string; literals?: { sourceOrdinal: number; literal: string }[];
    omittedCount?: number; value?: number | null };
  capRate?: { state: string; omittedCount?: number };
  malformedAttributeCount?: number; omittedAttributeCount?: number;
};

export function buildoutFieldObservations(attributes: unknown): FieldObservation | undefined {
  if (!Array.isArray(attributes)) return undefined;
  if (attributes.length > MAX_BUILDOUT_FIELD_ATTRIBUTES) return {
    schema: "buildout-field-observations-v1",
    sourceIdentity: "unverified", freshness: "unverified", promotion: "not_authorized",
    availableSpaceCount: { state: "not_evaluated_attribute_limit" },
    capRate: { state: "not_evaluated_attribute_limit" },
    omittedAttributeCount: attributes.length,
  };
  const literals: { sourceOrdinal: number; literal: string }[] = [];
  let targetCount = 0;
  let omittedCount = 0;
  let malformedCount = 0;
  let capCount = 0;
  for (const [ordinal, pair] of attributes.entries()) {
    if (!Array.isArray(pair) || pair.length !== 2 || typeof pair[0] !== "string" || !pair[0].trim()) {
      malformedCount++;
      if (Array.isArray(pair) && pair[0] === "# of Spaces") { targetCount++; omittedCount++; }
      if (Array.isArray(pair) && pair[0] === "Cap Rate") capCount++;
      continue;
    }
    if (pair[0] === "# of Spaces") {
      targetCount++;
      if (safeBuildoutSpaceLiteral(pair[1])) literals.push({ sourceOrdinal: ordinal, literal: pair[1] });
      else omittedCount++;
    }
    if (pair[0] === "Cap Rate") capCount++;
  }
  if (!targetCount && !capCount) return undefined;
  const parsed = literals.map((item) => countLiteral(item.literal));
  const state = malformedCount || omittedCount || parsed.some((value) => value === null) ? "invalid" :
    targetCount > 1 ? "ambiguous" : targetCount === 1 ? "observed" : "missing";
  return {
    schema: "buildout-field-observations-v1",
    sourceIdentity: "unverified", freshness: "unverified", promotion: "not_authorized",
    malformedAttributeCount: malformedCount,
    availableSpaceCount: { state, ...(literals.length ? { literals } : {}), omittedCount,
      ...(state === "observed" ? { value: parsed[0] } : {}) },
    ...(capCount ? { capRate: { state: "omitted_visibility_unverified", omittedCount: capCount } } : {}),
  };
}

/** Avoid last-wins promotion when a source attribute label is repeated or malformed. */
export function buildoutCanonicalAttributeMap(attributes: unknown): {
  values: Map<string, string>;
  blockedLabels: Set<string>;
  duplicateAttributeCount: number;
  malformedAttributeCount: number;
  omittedAttributeCount: number;
} {
  const values = new Map<string, string>();
  const seen = new Set<string>();
  const blocked = new Set<string>();
  let duplicateAttributeCount = 0;
  let malformedAttributeCount = 0;
  if (!Array.isArray(attributes)) return { values, blockedLabels: blocked, duplicateAttributeCount, malformedAttributeCount, omittedAttributeCount: 0 };
  if (attributes.length > MAX_BUILDOUT_FIELD_ATTRIBUTES) return { values, blockedLabels: blocked, duplicateAttributeCount, malformedAttributeCount, omittedAttributeCount: attributes.length };
  for (const pair of attributes) {
    const label = Array.isArray(pair) && typeof pair[0] === "string" && pair[0].trim() ? pair[0] : undefined;
    const value = Array.isArray(pair) ? pair[1] : undefined;
    const valid = Array.isArray(pair) && pair.length === 2 && label !== undefined &&
      (typeof value === "string" || (typeof value === "number" && Number.isFinite(value)));
    if (!valid) malformedAttributeCount++;
    if (label === undefined) continue;
    if (seen.has(label)) { duplicateAttributeCount++; blocked.add(label); }
    seen.add(label);
    if (!valid) blocked.add(label);
    if (valid && !blocked.has(label)) values.set(label, String(value));
    else values.delete(label);
  }
  return { values, blockedLabels: blocked, duplicateAttributeCount, malformedAttributeCount, omittedAttributeCount: 0 };
}
