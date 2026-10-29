import type * as cheerio from "cheerio";

type Field = "elevators" | "typical_floor_size";
type Observation = { field: Field; label: string; literal: string; eligibility: "eligible" | "ineligible" };
type Result = { schema: string; scope: string; visibility: string; state: string;
  observations: Observation[]; elevatorCount?: number; typicalFloorSizeText?: string };

// A public Details tab is property-specific, but this static inspection does not
// claim computed visibility. Preserve literal evidence even when it cannot lift.
export function transwesternBuildingFacts($: cheerio.CheerioAPI) {
  const observations: Observation[] = [];
  const result = (state: string, extra: Pick<Result, "elevatorCount" | "typicalFloorSizeText"> = {}): Result => ({
    schema: "transwestern-building-facts-v1", scope: "property_details_tab",
    visibility: "not_evaluated", state, observations, ...extra,
  });
  try {
    const conceal = (node: any) => {
      const el = $(node);
      return el.attr("hidden") !== undefined || (el.attr("aria-hidden") ?? "").trim().toLowerCase() === "true" ||
        /(?:^|;)\s*(?:display\s*:\s*none|visibility\s*:\s*(?:hidden|collapse))\b/i.test(el.attr("style") ?? "");
    };
    const sections = $("section.property-dtl");
    if (sections.length !== 1) return result("ambiguous_property_section");
    const tabs = sections.children(".fix-wrap").children(".property-dtl-inner")
      .children(".property-dscptn").children(".prpty-dscptn-left").children("#prpty-dscptn-tab");
    if (tabs.length !== 1 || sections.find("#prpty-dscptn-tab").length !== 1) return result("ambiguous_tab_container");
    const labels = tabs.children("ul.resp-tabs-list");
    const containers = tabs.children("div.resp-tabs-container");
    if (labels.length !== 1 || containers.length !== 1) return result("ambiguous_tab_structure");
    const names = labels.children("li");
    const panels = containers.children("div");
    const detailIndices = names.toArray().flatMap((node, i) =>
      $(node).text().trim().toLowerCase() === "details" ? [i] : []);
    if (names.length !== panels.length || detailIndices.length !== 1) return result("ambiguous_details_panel");
    const panel = panels.eq(detailIndices[0]);
    // The selected tab panel itself may be hidden until activation. Other
    // explicitly concealed ancestors are not evidence of a public assertion.
    if ([...tabs.parents().toArray(), tabs[0], containers[0]].some(conceal)) return result("concealed_property_ancestor");
    const lists = panel.children("ul");
    if (lists.length !== 1) return result("ambiguous_details_list");
    if (lists.children("li").length > 200) return result("not_evaluated_limit");
    let malformed = false;
    lists.children("li").each((_, row) => {
      const headings = $(row).find("b,strong");
      const label = headings.first().text().trim().replace(/:$/, "").trim();
      const field: Field | null = /^elevators?$/i.test(label) ? "elevators" :
        /^typical\s*floor\s*size$/i.test(label) ? "typical_floor_size" : null;
      if (!field) return;
      const literal = $(row).text().replace(headings.first().text(), "").replace(/^\s*:\s*/, "").trim();
      const eligible = headings.length === 1 && $(row).children("b,strong").length === 1 &&
        literal.length <= 2048 && !$(row).find("li, ul, ol").length &&
        ![lists[0], row, ...$(row).find("*").toArray()].some(conceal);
      if (!eligible) malformed = true;
      observations.push({ field, label, literal, eligibility: eligible ? "eligible" : "ineligible" });
    });
    // A nested label may not be silently ignored as if it were absent.
    if (lists.find("li li, li ul, li ol").length) return result("ambiguous_details_structure");
    const elevators = observations.filter(x => x.field === "elevators");
    const floors = observations.filter(x => x.field === "typical_floor_size");
    const extra: Pick<Result, "elevatorCount" | "typicalFloorSizeText"> = {};
    if (elevators.length === 1 && elevators[0].eligibility === "eligible" && /^(?:0|[1-9]\d*)$/.test(elevators[0].literal)) {
      const value = Number(elevators[0].literal);
      if (Number.isSafeInteger(value)) extra.elevatorCount = value;
    }
    if (floors.length === 1 && floors[0].eligibility === "eligible" && /\d/.test(floors[0].literal)) extra.typicalFloorSizeText = floors[0].literal;
    return result(malformed ? "malformed_or_concealed_row" :
      elevators.length > 1 || floors.length > 1 ? "ambiguous_fields" : "inspected", extra);
  } catch {
    return result("not_evaluated_error");
  }
}
