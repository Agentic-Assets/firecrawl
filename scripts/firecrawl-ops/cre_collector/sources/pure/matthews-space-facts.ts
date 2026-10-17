import type { CheerioAPI } from "cheerio";
import { clean } from "../../lib/util.js";

// Literal source facts, not a property rent/area aggregate. Combined and
// individual suites can overlap; keep their original card associations.
export function matthewsSpaceFacts($: CheerioAPI) {
  const title = $("#propertyTitle");
  if (title.length !== 1) return undefined;
  const section = title.closest("section");
  const groups = section.find("h3").filter((_, heading) =>
    /^\d+\s+Spaces?\s+Available$/i.test(clean($(heading).text()) ?? "")
  );
  if (!groups.length) return undefined;
  if (groups.length !== 1) {
    return { matthewsUnparsedLeasingSpaces: groups.map((_, h) => clean($(h).parent().text())).get() };
  }

  const spaces: Array<Array<{ label: string; value: string }>> = [];
  const unparsed: string[] = [];
  groups.parent().children("div").each((_, card) => {
    const facts: Array<{ label: string; value: string }> = [];
    const seen = new Set<string>();
    let ambiguous = false;
    $(card).find(".key-info-title").each((_, element) => {
      const row = $(element).parent().parent();
      const label = clean($(element).text());
      const value = clean(row.find(".key-info-value").text());
      const key = label?.replace(/:$/, "").toLowerCase();
      if (!key || !value || seen.has(key)
        || row.find(".key-info-title").length !== 1
        || row.find(".key-info-value").length !== 1) {
        ambiguous = true;
        return;
      }
      seen.add(key);
      facts.push({ label: label!, value });
    });
    if ($(card).find(".key-info-value").length !== facts.length) ambiguous = true;
    if (!ambiguous && seen.has("space name")
      && (seen.has("rental rate") || seen.has("space available"))) {
      spaces.push(facts);
    } else {
      const text = clean($(card).text());
      if (text) unparsed.push(text);
    }
  });
  return { matthewsLeasingSpaces: spaces, matthewsUnparsedLeasingSpaces: unparsed };
}
