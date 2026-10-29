import type * as cheerio from "cheerio";
import { transwesternConcealed, transwesternPropertyPanel } from "./transwestern-property-panel.js";

type Field = "elevators" | "typical_floor_size";
type Observation = { field: Field; label: string; literal: string; eligibility: "eligible" | "ineligible" };
type Result = { schema: string; scope: string; visibility: string; state: string;
  observations: Observation[]; observationCount: number; elevatorCount?: number; typicalFloorSizeText?: string };

// A public Details tab is property-specific, but this static inspection does not
// claim computed visibility. Preserve literal evidence even when it cannot lift.
export function transwesternBuildingFacts($: cheerio.CheerioAPI) {
  const observations: Observation[] = [];
  const result = (state: string, extra: Pick<Result, "elevatorCount" | "typicalFloorSizeText"> = {}): Result => ({
    schema: "transwestern-building-facts-v1", scope: "property_details_tab",
    visibility: "not_evaluated", state, observations, observationCount: observations.length, ...extra,
  });
  try {
    const association = transwesternPropertyPanel($, "details");
    if (!association.panel) return result(association.state);
    const panel = association.panel;
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
        ![lists[0], row, ...$(row).find("*").toArray()].some(node => transwesternConcealed($, node));
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
