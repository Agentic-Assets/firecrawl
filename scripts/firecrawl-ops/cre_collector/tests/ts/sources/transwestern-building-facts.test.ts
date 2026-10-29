import test from "node:test";
import assert from "node:assert/strict";
import * as cheerio from "cheerio";
import { createHash } from "node:crypto";
import { transwesternBuildingFacts as observe } from "../../../sources/pure/transwestern-building-facts.js";
import { enrichTranswesternListing } from "../../../sources/transwestern.js";
import { firecrawl } from "../../../lib/scrape.js";

const page = (rows: string, tabs = "<li>Description</li><li>Details</li>", panels = `<div>description</div><div><ul>${rows}</ul></div>`) =>
  cheerio.load(`<section class="property-dtl"><div class="fix-wrap"><div class="property-dtl-inner"><div class="property-dscptn"><div class="prpty-dscptn-left"><div id="prpty-dscptn-tab"><ul class="resp-tabs-list">${tabs}</ul><div class="resp-tabs-container">${panels}</div></div></div></div></div></div></section>`);

test("lifts zero and retains source floor units in the matching Details panel", () => {
  const result = observe(page('<li><b>Elevators</b> : 0</li><li><b>Typical Floor Size</b> : 34,567 SF</li>'));
  assert.equal(result.state, "inspected");
  assert.equal(result.elevatorCount, 0);
  assert.equal(result.typicalFloorSizeText, "34,567 SF");
  assert.equal(result.visibility, "not_evaluated");
});

test("keeps conflicting and unparseable literals, without lifting them", () => {
  const result = observe(page('<li><b>Elevators</b> : 1</li><li><b>Elevators</b> : 2</li><li><b>Typical Floor Size</b> : 5,374</li>'));
  assert.equal(result.state, "ambiguous_fields");
  assert.equal(result.elevatorCount, undefined);
  assert.equal(result.typicalFloorSizeText, "5,374");
  assert.deepEqual(result.observations.map(x => x.literal), ["1", "2", "5,374"]);
  const unparseable = observe(page('<li><b>Elevators</b> : two passenger</li>'));
  assert.equal(unparseable.elevatorCount, undefined);
  assert.equal(unparseable.observations[0].literal, "two passenger");
});

test("refuses unpaired, duplicate, nested and concealed source structures", () => {
  assert.equal(observe(page('', '<li>Details</li><li>Details</li>')).state, "ambiguous_details_panel");
  assert.equal(observe(page('', '<li>Details</li>', '<div></div><div></div>')).state, "ambiguous_details_panel");
  assert.equal(observe(page('<li><b>Elevators</b> : 1<ul><li>other</li></ul></li>')).state, "ambiguous_details_structure");
  assert.equal(observe(page('<li hidden><b>Elevators</b> : 1</li>')).elevatorCount, undefined);
  assert.equal(observe(cheerio.load('<section class="property-dtl"></section><section class="property-dtl"></section>')).state, "ambiguous_property_section");
});

test("holds an ineligible field without losing an eligible sibling", () => {
  const result = observe(page('<li hidden><b>Elevators</b> : 2</li><li><b>Typical Floor Size</b> : 5,374 SF</li>'));
  assert.equal(result.state, "malformed_or_concealed_row");
  assert.equal(result.elevatorCount, undefined);
  assert.equal(result.typicalFloorSizeText, "5,374 SF");
  assert.deepEqual(result.observations.map(x => x.eligibility), ["ineligible", "eligible"]);
});

test("unknown nested details structure holds both candidates while retaining observations", () => {
  const result = observe(page('<li><b>Elevators</b> : 2</li><li><b>Typical Floor Size</b> : 5,374</li><li>Other<ul><li>nested</li></ul></li>'));
  assert.equal(result.state, "ambiguous_details_structure");
  assert.equal(result.elevatorCount, undefined);
  assert.equal(result.typicalFloorSizeText, undefined);
  assert.deepEqual(result.observations.map(x => x.literal), ["2", "5,374"]);
});

test("does not treat the intentional hidden Details tab as a hidden field", () => {
  const result = observe(page('', '<li>Details</li>', '<div style="display:none"><ul><li><b>Elevators</b> : 1</li></ul></div>'));
  assert.equal(result.elevatorCount, 1);
});

test("rejects a collateral tab and explicitly concealed property ancestors", () => {
  const collateral = cheerio.load('<section class="property-dtl"><aside><div id="prpty-dscptn-tab"><ul class="resp-tabs-list"><li>Details</li></ul><div class="resp-tabs-container"><div><ul><li><b>Elevators</b> : 99</li></ul></div></div></div></aside></section>');
  assert.equal(observe(collateral).state, "ambiguous_tab_container");
  for (const selector of ["section.property-dtl", ".fix-wrap", ".property-dscptn", "#prpty-dscptn-tab", ".resp-tabs-container"]) {
    const $ = page('<li><b>Elevators</b> : 2</li>');
    $(selector).attr("aria-hidden", "true");
    const facts = observe($);
    assert.equal(facts.state, "concealed_property_ancestor");
    assert.equal(facts.elevatorCount, undefined);
  }
});

test("accepted detail binds facts to actual body, URL and detail clock", async () => {
  const original = firecrawl.scrape;
  const html = '<h1>Sample Property</h1><section class="property-dtl"><div class="fix-wrap"><div class="property-dtl-inner"><div class="property-dscptn"><div class="prpty-dscptn-left"><div id="prpty-dscptn-tab"><ul class="resp-tabs-list"><li>Details</li></ul><div class="resp-tabs-container"><div><ul><li><b>Elevators</b> : 0</li></ul></div></div></div></div></div></div></div></section>';
  (firecrawl as any).scrape = async () => ({ rawHtml: html, markdown: "Sample Property", links: [] });
  const row = { PageUrl: "sample-property", BuildingName: "Sample Property", FullAddress: "100 Main Street" };
  try {
    const listing = await enrichTranswesternListing(row, "Sale", "sale", false);
    const facts = listing.transwesternBuildingFacts;
    assert.equal(facts.sourceUrl, listing.url);
    assert.equal(facts.bodySha256, createHash("sha256").update(html).digest("hex"));
    assert.equal(facts.observedAt, listing.detailObservedAt);
    assert.equal(facts.elevatorCount, 0);
    const wrong = await enrichTranswesternListing({ ...row, BuildingName: "Different" , FullAddress: "other" }, "Sale", "sale", false);
    assert.equal(wrong.transwesternBuildingFacts, undefined);
  } finally { (firecrawl as any).scrape = original; }
});
