import test from "node:test";
import assert from "node:assert/strict";
import * as cheerio from "cheerio";
import { transwesternFieldObservations as observe } from "../../../sources/pure/transwestern-field-observations.js";
import { srcTranswestern } from "../../../sources/transwestern.js";
import { firecrawl } from "../../../lib/scrape.js";

test("retains ordered duplicate source literals without promoting them", () => {
  const result = observe(cheerio.load('<ul><li><b>Elevators:</b> 0</li><li><b>Elevators:</b> 2</li><li><b>Typical Floor Size:</b> 24,990</li></ul>'));
  assert.deepEqual(result.observations.map(({ field, label, literal }) => [field, label, literal]), [
    ["elevators", "Elevators", "0"], ["elevators", "Elevators", "2"],
    ["typical_floor_size", "Typical Floor Size", "24,990"],
  ]);
  assert.equal(result.promotion, "not_authorized");
  assert.equal(result.visibility, "unverified");
  assert.equal(result.association, "unverified");
  assert.equal(result.scope, "entire_detail_document");
});

test("related property observations remain explicitly unassociated", () => {
  const result = observe(cheerio.load('<main><ul><li><b>Elevators</b> 1</li></ul></main><aside><ul><li><b>Elevators</b> 9</li></ul></aside>'));
  assert.deepEqual(result.observations.map(x => x.literal), ["1", "9"]);
  assert.equal(result.association, "unverified");
  assert.equal(result.promotion, "not_authorized");
});

test("counts hidden and unsafe values without retaining them", () => {
  const result = observe(cheerio.load('<ul><li hidden><b>Elevators</b> 999</li><li aria-hidden="true"><b>Elevator</b> 888</li><li style="display:none"><b>Elevators</b> 777</li><li><b>Typical Floor Size</b> confidential</li><li><b>Elevators</b> 1</li></ul>'));
  assert.equal(result.omittedCount, 4);
  assert.deepEqual(result.observations.map((x) => x.literal), ["1"]);
  assert.doesNotMatch(JSON.stringify(result), /999|888|777|confidential/);
});

test("reports unknown omissions at the bound", () => {
  const result = observe(cheerio.load(`<ul>${'<li><b>Elevators</b> 1</li>'.repeat(101)}</ul>`));
  assert.equal(result.state, "partial_limit");
  assert.equal(result.omittedCount, null);
  assert.equal(result.observations.length, 100);
});

test("rejects punctuation-only values and bounds pathological documents", () => {
  const punctuation = observe(cheerio.load('<li><b>Elevators</b> ...</li>'));
  assert.equal(punctuation.omittedCount, 1);
  assert.equal(punctuation.retainedCount, 0);
  const deep = observe(cheerio.load('<div>'.repeat(130) + '<li><b>Elevators</b> 1</li>' + '</div>'.repeat(130)));
  assert.equal(deep.state, "not_evaluated_limit_or_error");
  const many = observe(cheerio.load('<i></i>'.repeat(50_001)));
  assert.equal(many.state, "not_evaluated_limit_or_error");
  const huge = observe(cheerio.load('<li><b>Elevators</b>' + '1'.repeat(2_000_001) + '</li>'));
  assert.equal(huge.state, "not_evaluated_limit_or_error");
  const rows = observe(cheerio.load('<li>unrelated</li>'.repeat(1001)));
  assert.equal(rows.state, "partial_limit");
  assert.equal(rows.omittedCount, null);
  const failed = observe(null as unknown as cheerio.CheerioAPI);
  assert.equal(failed.state, "not_evaluated_limit_or_error");
});

test("only accepted detail emits audit observations; monitor does not", async () => {
  const original = firecrawl.scrape;
  const row = { PageUrl: "sample-property", BuildingName: "Sample Property", PropertyTypeName: "Office",
    FullAddress: "100 Main Street", City: "Dallas", State: "TX", ZipCode: "75201", PropertySize: 10000 };
  (firecrawl as any).scrape = async (url: string) => url.includes("/properties?")
    ? { rawHtml: JSON.stringify([row]) }
    : { rawHtml: '<h1>Sample Property</h1><div class="property-description">Current detail</div><ul><li><b>Elevators:</b> 2</li></ul>', markdown: "Sample Property\nCurrent detail", links: [] };
  try {
    const full = await srcTranswestern("sale", 1, false);
    const audit = (full.listings[0] as any).transwesternFieldObservations;
    assert.equal(audit.observations[0].literal, "2");
    assert.equal(audit.promotion, "not_authorized");
    assert.equal(audit.state, "inspected");
    assert.equal(audit.retainedCount, 1);
    const monitor = await srcTranswestern("sale", 1, true);
    assert.equal((monitor.listings[0] as any).transwesternFieldObservations, undefined);
  } finally { (firecrawl as any).scrape = original; }
});
