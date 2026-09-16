import type { Tx } from "../../types.js";
import { clean } from "../../lib/util.js";

export type FoundryStatusDecision = { disposition: "active" | "terminal" | "held"; status: string | null; tenures: Tx[]; reason: string; };

const FOUNDRY_TERMINAL_STATUSES = new Set(["closed", "discontinued", "leased", "off market", "sold", "unavailable", "withdrawn"]);

export function normalizedFoundryStatus(value: string | null): string | null {
  return clean(value)?.toLowerCase().replace(/[_-]+/g, " ").replace(/\s*[/|]\s*/g, " or ").replace(/\s+/g, " ").trim() ?? null;
}

/** Explicit provider taxonomy, default-deny for unreviewed statuses. */
export function classifyFoundryStatus(value: string | null): FoundryStatusDecision {
  const status = normalizedFoundryStatus(value);
  if (!status) return { disposition: "held", status: null, tenures: [], reason: "missing explicit Foundry property status" };
  if (FOUNDRY_TERMINAL_STATUSES.has(status)) return { disposition: "terminal", status, tenures: [], reason: `terminal Foundry property status: ${status}` };
  if (status === "for sale") return { disposition: "active", status, tenures: ["sale"], reason: "explicit for-sale status" };
  if (status === "for lease" || status === "sublease") return { disposition: "active", status, tenures: ["lease"], reason: `explicit ${status} status` };
  if (
    status === "for sale or lease"
    || status === "for lease or sale"
    || status === "sale and lease"
    || status === "sale or lease"
    || status === "lease or sale"
  ) {
    return { disposition: "active", status, tenures: ["sale", "lease"], reason: "explicit dual-tenure status" };
  }
  if (status === "available" || status === "coming soon" || status === "proposed" || status === "under contract") return { disposition: "active", status, tenures: [], reason: `active status ${status} requires a separate explicit transaction token` };
  return { disposition: "held", status, tenures: [], reason: `unknown Foundry property status: ${status}` };
}

/** Extract explicit sale/lease tenure tokens from provider-owned property notes. */
export function explicitFoundryTenures(notes: readonly string[]): Tx[] {
  const tenures = new Set<Tx>();
  for (const note of notes) {
    const decision = classifyFoundryStatus(note);
    if (decision.disposition !== "active") continue;
    for (const tenure of decision.tenures) {
      tenures.add(tenure);
    }
  }
  return [...tenures];
}
