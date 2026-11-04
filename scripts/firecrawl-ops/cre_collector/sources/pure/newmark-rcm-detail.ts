import * as cheerio from "cheerio";
import { visit } from "jsonc-parser";
import { preservesJsonValue } from "../../lib/json-value.js";

type LiteralField = { state: "missing" | "null" | "unresolved" } | { state: "value"; value: string };
export type NewmarkPropertyWitness = {
  sourceName: LiteralField;
  sourceProperty: { selection: "unresolved"; fields: Record<string, never> } |
    { selection: "single"; fields: Record<string, LiteralField> };
};

/** Use the maintained JSON visitor to reject duplicate keys and unsupported syntax. */
function unambiguousJson(literal: string): boolean {
  let valid = true, nodes = 0;
  const stack: Array<Set<string> | null> = [];
  const count = () => { if (++nodes > 100_000 || stack.length > 128) throw new Error("JSON bound"); };
  try {
    visit(literal, {
      onObjectBegin: () => { stack.push(new Set()); count(); },
      onObjectProperty: key => { count(); const keys = stack.at(-1); if (!keys || Buffer.byteLength(key, "utf8") > 8192 || keys.has(key)) valid = false; else keys.add(key); },
      onObjectEnd: () => { stack.pop(); },
      onArrayBegin: () => { stack.push(null); count(); },
      onArrayEnd: () => { stack.pop(); },
      onLiteralValue: () => count(),
      onError: () => { valid = false; },
    }, { disallowComments: true, allowTrailingComma: false });
    return valid && stack.length === 0;
  } catch { return false; }
}

/** Original embedded JSON is retained as text, including its numeric literals. */
export function extractNewmarkRcmDetail(html: unknown): {
  status: "observed" | "unavailable" | "ambiguous" | "invalid_json" | "input_limit";
  literal: string | null; parsed: unknown; parsedSerializable: boolean; numericFidelity: "literal_only";
} {
  const empty = (status: "unavailable" | "ambiguous" | "input_limit") =>
    ({ status, literal: null, parsed: null, parsedSerializable: false, numericFidelity: "literal_only" as const });
  if (typeof html !== "string") return empty("unavailable");
  if (Buffer.byteLength(html, "utf8") > 2_000_000) return empty("input_limit");
  const $ = cheerio.load(html);
  const candidates = $("script").toArray().map(element => $(element).html() ?? "")
    .filter(text => /\bvar\s+data\s*=/.test(text));
  if (!candidates.length) return empty("unavailable");
  if (candidates.length !== 1) return empty("ambiguous");
  const text = candidates[0]!;
  if ([...text.matchAll(/\bvar\s+data\s*=/g)].length !== 1) return empty("ambiguous");
  const match = text.match(/\bvar\s+data\s*=\s*(\{[\s\S]*\})\s*;?\s*$/);
  if (!match) return empty("ambiguous");
  const literal = match[1]!;
  try {
    if (!unambiguousJson(literal)) return { status: "invalid_json", literal, parsed: null, parsedSerializable: false, numericFidelity: "literal_only" };
    const parsed: unknown = JSON.parse(literal);
    return { status: "observed", literal, parsed, parsedSerializable: preservesJsonValue(parsed), numericFidelity: "literal_only" };
  } catch { return { status: "invalid_json", literal, parsed: null, parsedSerializable: false, numericFidelity: "literal_only" }; }
}

function literalValue(input: unknown): string | null {
  if (!input || typeof input !== "object" || Array.isArray(input)) return null;
  const field = input as Partial<LiteralField> & { value?: unknown };
  return field.state === "value" && typeof field.value === "string" && field.value.length <= 256 ? field.value : null;
}
function comparable(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim().replace(/\s+/g, " ").toLocaleLowerCase("en-US") : null;
}

/** Conservative source-to-page comparison, not publisher authentication. */
export function matchNewmarkRcmProperty(witness: unknown, parsed: unknown): "matched" | "unresolved" {
  if (!witness || typeof witness !== "object" || !parsed || typeof parsed !== "object" || Array.isArray(parsed)) return "unresolved";
  const source = witness as NewmarkPropertyWitness;
  if (source.sourceProperty?.selection !== "single") return "unresolved";
  const fields = source.sourceProperty.fields;
  if (!fields || typeof fields !== "object" || Array.isArray(fields)) return "unresolved";
  const data = parsed as { name?: unknown; addresses?: Record<string, unknown> };
  const addresses = data.addresses;
  if (!addresses || typeof addresses !== "object" || Array.isArray(addresses)) return "unresolved";
  const pairs: Array<[unknown, unknown, boolean]> = [
    [literalValue(source.sourceName), data.name, true],
    [literalValue(fields.address), addresses.street, true],
    [literalValue(fields.city), addresses.city, true],
    [literalValue(fields.stateAbbreviation), addresses.state, false],
    [literalValue(fields.zip), addresses.zip, false],
  ];
  if (!literalValue(fields.stateAbbreviation) && !literalValue(fields.zip)) return "unresolved";
  for (const [left, right, required] of pairs) {
    if (left === null && !required) continue;
    const a = comparable(left), b = comparable(right);
    if (!a || !b || a !== b) return "unresolved";
  }
  return "matched";
}
