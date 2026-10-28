import type * as cheerio from "cheerio";
import type { AnyNode } from "domhandler";

// Audit-only observations from the entire accepted detail document. The page
// may contain unrelated properties; neither association nor visibility is proved.
// Inline concealment signals reject values; their absence does not establish visibility.
export function transwesternFieldObservations($: cheerio.CheerioAPI) {
  const unavailable = () => ({ schema: "transwestern-field-observations-v1", scope: "entire_detail_document",
    association: "unverified", visibility: "unverified", promotion: "not_authorized",
    state: "not_evaluated_limit_or_error", retainedCount: 0,
    observations: [] as Array<{ field: string; label: string; literal: string; rowOrdinal: number }>,
    omittedCount: null });
  try {
  const pending: Array<{ node: AnyNode; depth: number }> = [{ node: $.root()[0], depth: 0 }];
  let visited = 0;
  let textBytes = 0;
  while (pending.length) {
    const { node, depth } = pending.pop()!;
    if (++visited > 50_000 || depth > 128) return unavailable();
    if ("data" in node && typeof node.data === "string") {
      textBytes += Buffer.byteLength(node.data, "utf8");
      if (textBytes > 2_000_000) return unavailable();
    }
    if ("children" in node) {
      if (pending.length + node.children.length > 50_000) return unavailable();
      for (const child of node.children) pending.push({ node: child, depth: depth + 1 });
    }
  }
  const observations: Array<{ field: string; label: string; literal: string; rowOrdinal: number }> = [];
  let omitted = 0;
  let truncated = false;
  $("li").each((rowOrdinal, row) => {
    if (rowOrdinal >= 1_000) { truncated = true; return false; }
    const heading = $(row).find("b,strong").first();
    const label = heading.text().trim().replace(/:$/, "").trim();
    const field = /^elevators?$/i.test(label) ? "elevators" :
      /^typical\s*floor\s*size$/i.test(label) ? "typical_floor_size" : null;
    if (!field) return;
    if (observations.length + omitted >= 100) { truncated = true; return false; }
    const text = $(row).text();
    const literal = text.replace(heading.text(), "").replace(/^\s*:\s*/, "").trim();
    const concealed = $(row).parents().addBack().add($(row).find("*"))
      .toArray().some((node) => {
        const el = $(node);
        return el.attr("hidden") !== undefined ||
          (el.attr("aria-hidden") ?? "").trim().toLowerCase() === "true" ||
          /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b/i.test(el.attr("style") ?? "");
      });
    // Restrict persisted literals to ordinary numeric source text. A rejected
    // occurrence remains counted, so it cannot masquerade as a unique fact.
    if (concealed || !/^(?=.*[0-9])[0-9,. ]{1,128}$/.test(literal)) { omitted++; return; }
    observations.push({ field, label, literal, rowOrdinal });
  });
  return { schema: "transwestern-field-observations-v1", scope: "entire_detail_document",
    association: "unverified", visibility: "unverified",
    promotion: "not_authorized", state: truncated ? "partial_limit" : "inspected",
    observations, retainedCount: observations.length, omittedCount: truncated ? null : omitted };
  } catch {
    return unavailable();
  }
}
