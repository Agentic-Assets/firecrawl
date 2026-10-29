import type * as cheerio from "cheerio";

export function transwesternConcealed($: cheerio.CheerioAPI, node: any): boolean {
  const el = $(node);
  return el.attr("hidden") !== undefined || (el.attr("aria-hidden") ?? "").trim().toLowerCase() === "true" ||
    /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b/i.test(el.attr("style") ?? "");
}

// Positional panels belong to the unique witnessed property hierarchy. The
// panel itself may be hidden until its tab is selected; ancestors may not.
export function transwesternPropertyPanel($: cheerio.CheerioAPI, name: "details" | "description") {
  const sections = $("section.property-dtl");
  if (sections.length !== 1) return { state: "ambiguous_property_section" };
  const tabs = sections.children(".fix-wrap").children(".property-dtl-inner")
    .children(".property-dscptn").children(".prpty-dscptn-left").children("#prpty-dscptn-tab");
  if (tabs.length !== 1 || sections.find("#prpty-dscptn-tab").length !== 1) return { state: "ambiguous_tab_container" };
  const labels = tabs.children("ul.resp-tabs-list");
  const containers = tabs.children("div.resp-tabs-container");
  if (labels.length !== 1 || containers.length !== 1) return { state: "ambiguous_tab_structure" };
  const names = labels.children("li");
  const panels = containers.children("div");
  const indices = names.toArray().flatMap((node, i) => $(node).text().trim().toLowerCase() === name ? [i] : []);
  if (names.length !== panels.length || indices.length !== 1) return { state: `ambiguous_${name}_panel` };
  if ([...tabs.parents().toArray(), tabs[0], containers[0]].some(node => transwesternConcealed($, node))) {
    return { state: "concealed_property_ancestor" };
  }
  return { state: "inspected", panel: panels.eq(indices[0]) };
}
