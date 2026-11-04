import * as cheerio from "cheerio";
import type { AnyNode } from "domhandler";

// Offline research only. These are source literals, not verified listing facts.
// No caller may infer publisher identity, rights, computed visibility, or
// permission to persist/promote from the presence of an observation.
const MAX_BYTES = 2_000_000;
const MAX_ROWS = 1_000;
const MAX_TABLES = 32;
const MAX_NODES = 50_000;
const MAX_DEPTH = 128;
const MAX_OBSERVATIONS = 100;
const LABELS = new Map([["NOI", "noi"], ["Year Built", "yearBuilt"], ["Cap Rate", "capRate"]] as const);
const NUMERIC_LITERAL = /^[\d\s$,.%+\-()]{1,64}$/;
const CONTROL = /(?:withheld|confidential|contact\s+(?:broker|agent)|not\s+(?:disclosed|available))/i;

type Field = "noi" | "yearBuilt" | "capRate";
type Observation = { field: Field; label: string; literal: string; tableOrdinal: number; rowOrdinal: number };
// Omission count covers recognized labels in inspected rows only, not page completeness.
type Result = { state: "evaluated" | "partial_observation_limit" | "not_evaluated_limit"; identity: "unverified"; visibility: "unverified"; promotion: "not_authorized"; observations: Observation[]; omittedCount: number | null };

export function offlineBuildoutDetailObservations(html: string): Result {
  const empty = (state: Result["state"], omittedCount: number | null = null): Result => ({ state, identity: "unverified", visibility: "unverified", promotion: "not_authorized", observations: [], omittedCount });
  if (typeof html !== "string" || Buffer.byteLength(html, "utf8") > MAX_BYTES) return empty("not_evaluated_limit");
  const $ = cheerio.load(html);
  // Walk iteratively once before any ancestor or descendant queries. Reject the
  // whole document on a limit so no partial output looks complete.
  const pending: Array<{ node: AnyNode; depth: number }> = [{ node: $.root()[0], depth: 0 }];
  let nodeCount = 0;
  while (pending.length) {
    const { node, depth } = pending.pop()!;
    if (++nodeCount > MAX_NODES || depth > MAX_DEPTH) return empty("not_evaluated_limit");
    if ("children" in node) for (const child of node.children) pending.push({ node: child, depth: depth + 1 });
  }
  const tables = $("table.COMPONENT__table");
  if (tables.length > MAX_TABLES) return empty("not_evaluated_limit");
  const rows = tables.find("tr");
  if (rows.length > MAX_ROWS) return empty("not_evaluated_limit");
  const observations: Observation[] = [];
  let omittedCount = 0;
  let limitExceeded = false;
  tables.each((tableOrdinal, table) => {
    if (limitExceeded) return false;
    $(table).find("tr").each((rowOrdinal, row) => {
      if (limitExceeded) return false;
      if ($(row).closest("table")[0] !== table) return;
      // Require exactly two direct cells: do not pair across rows or tables.
      const cells = $(row).children("td, th");
      if (!cells.length) return;
      const label = $(cells[0]).text().trim().replace(/:$/, "");
      const field = LABELS.get(label as "NOI" | "Year Built" | "Cap Rate");
      if (!field) return;
      if (observations.length + omittedCount >= MAX_OBSERVATIONS) { limitExceeded = true; return false; }
      if (cells.length !== 2) { omittedCount++; return; }
      const literal = $(cells[1]).text().trim().replace(/\s+/g, " ");
      // Inline signals are reject-only; their absence does not prove visibility.
      const concealed = $(row).parents().addBack().add($(row).find("*"))
        .toArray().some((element) => {
          const node = $(element);
          const style = node.attr("style") ?? "";
          return node.attr("hidden") !== undefined ||
            (node.attr("aria-hidden") ?? "").trim().toLowerCase() === "true" ||
            /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b/i.test(style);
        });
      if (concealed || $(cells[1]).find("table").length > 0 || CONTROL.test($(row).text()) || !NUMERIC_LITERAL.test(literal)) { omittedCount++; return; }
      observations.push({ field, label, literal, tableOrdinal, rowOrdinal });
    });
  });
  // Structural limits above fail closed before extraction. At the separate
  // recognized-row limit, retain only already inspected observations; the
  // remainder and its omissions are unknown, so no completeness count is given.
  if (limitExceeded) return { ...empty("partial_observation_limit"), observations };
  return { ...empty("evaluated", omittedCount), observations };
}
