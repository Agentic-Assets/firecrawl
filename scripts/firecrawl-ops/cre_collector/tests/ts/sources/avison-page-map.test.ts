import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { avisonYoungBaseListing, enrichAvisonYoungListing } from "../../../sources/avison-young.js";

const dir = new URL("../../fixtures/raw_data/", import.meta.url);
const evidence = JSON.parse(readFileSync(new URL("avison-young-named-association-17440.json", dir), "utf8"));
const html = readFileSync(new URL("avison-young-named-association-17440.html", dir), "utf8");
const url = evidence.response_url;
const map = (lat: string, lng: string) => `<div id="map-container"><div id="map_canvas" data-latitude="${lat}" data-longitude="${lng}" data-zoom="16"></div></div>`;
async function enrich(body: string, lat = 34.55637, lng = -82.001803) {
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  try {
    const base = avisonYoungBaseListing({ ...evidence.feed_row, location: { lat, lng } }, new Map());
    return await enrichAvisonYoungListing({ ...base, sharpLaunchUrl: undefined }, false,
      async () => ({ rawHtml: body, markdown: "", links: [], metadata: { sourceURL: url, url, statusCode: 200 } }),
      { matchingExternalUrlRowIds: ["17440"] });
  } finally { if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT; else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous; }
}
const withMap = (markup: string) => html.replace("</body>", markup + "</body>");
test("identity-bound official map is retained separately from conflicting feed coordinates", async () => {
  const result = await enrich(withMap(map("34.55119049627", "-82.006313292438")));
  assert.equal(result.detailError, undefined);
  assert.equal(result.latitude, 34.55637); assert.equal(result.longitude, -82.001803);
  assert.deepEqual(result.avisonPageMapObservation, { sourceUrl: url, latitude: 34.55119049627, longitude: -82.006313292438, method: "official_property_map" });
});
test("map does not populate absent feed coordinates", async () => {
  const result = await enrich(withMap(map("34.551", "-82.006")), null as any, null as any);
  assert.equal(result.latitude, undefined); assert.equal(result.longitude, undefined);
  assert.equal(result.avisonPageMapObservation.latitude, 34.551);
});
test("matching map is still an independent observation", async () => {
  const result = await enrich(withMap(map("34.55637", "-82.001803")));
  assert.deepEqual(result.avisonPageMapObservation, { sourceUrl: url, latitude: 34.55637, longitude: -82.001803, method: "official_property_map" });
});
for (const markup of ["", map("", "-82"), map("34", ""), map("Infinity", "-82"), map("NaN", "-82"), map("0x22", "-82"), map("91", "-82"), map("34", "181"), map("34", "-82") + map("34", "-82"), map("34", "-82") + `<div id="map_canvas" data-latitude="34" data-longitude="-82"></div>`]) {
  test(`absent or ambiguous map does not suppress listing: ${markup || "absent"}`, async () => {
    const result = await enrich(withMap(markup)); assert.equal(result.detailError, undefined); assert.equal(result.avisonPageMapObservation, undefined);
    assert.equal(result.latitude, 34.55637); assert.equal(result.longitude, -82.001803);
  });
}
test("unvalidated page cannot supply map evidence", async () => {
  const result = await enrich(withMap(map("34", "-82")).replaceAll("The Shoppes at Glen Laurel", "Unrelated Subject"));
  assert.equal(result.avisonPageMapObservation, undefined); assert.ok(result.detailError);
});
