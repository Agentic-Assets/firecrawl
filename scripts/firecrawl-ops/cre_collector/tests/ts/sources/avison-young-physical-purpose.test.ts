import test from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import * as cheerio from "cheerio";
import {
  assertAvisonYoungDetailDoc, avisonYoungBaseListing, enrichAvisonYoungListing,
  fetchAvisonYoungDirectDoc, indexAvisonYoungOfficialFeedUrls, avisonYoungOfficialUrlKey,
} from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";

process.argv = [process.argv[0]!, process.argv[1]!];
const fixture = (name: string) => readFileSync(new URL(`../../fixtures/raw_data/${name}`, import.meta.url));
const originalBytes = fixture("avison-4294-official-17665.html");
const originalHtml = originalBytes.toString("utf8");
const originalRow = JSON.parse(fixture("avison-4294-feed-17665.json").toString("utf8"));
const digest = (value: unknown) => createHash("sha256").update(JSON.stringify(value)).digest("hex");

function specimen() {
  const $ = cheerio.load(originalHtml);
  const node = $('script[type="application/ld+json"]').first();
  return {
    $, node, record: JSON.parse(node.text()), row: structuredClone(originalRow),
    owners: ["17665"], metadataUrl: originalRow.external_url,
  };
}
type Specimen = ReturnType<typeof specimen>;

function check(state = specimen()) {
  state.node.text(JSON.stringify(state.record));
  const url = state.row.external_url;
  const base = avisonYoungBaseListing(state.row, new Map());
  const doc: ScrapedDoc = {
    rawHtml: state.$.html(), markdown: "", links: [],
    metadata: { statusCode: 200, sourceURL: url, url: state.metadataUrl },
  };
  const before = digest({ base, row: state.row, doc });
  const result = assertAvisonYoungDetailDoc(doc, url, base, { matchingExternalUrlRowIds: state.owners });
  assert.equal(digest({ base, row: state.row, doc }), before, "comparison must not rewrite source values");
  return result;
}

function poiScripts(state: Specimen) {
  return state.$("script").filter((_index, node) => /\bpoi\s*:/.test(state.$(node).text()));
}

function replacePoi(state: Specimen, index: number, replacement: string) {
  const script = poiScripts(state).eq(index);
  assert.equal(script.length, 1);
  const before = script.text();
  const after = before.replace(/(\bpoi\s*:\s*)\[\]/, `$1${replacement}`);
  assert.notEqual(after, before, "mutation must change an actual original POI claim");
  script.text(after);
}

test("17665 fixture is the full authentic response, with its exact feed row and two empty POI claims", () => {
  assert.equal(createHash("sha256").update(originalBytes).digest("hex"),
    "8725e0ff1648400494446e91b58af3c2501304d0e52e3373e5a3add89f6ac257");
  assert.equal(createHash("sha256").update(fixture("avison-4294-feed-17665.json")).digest("hex"),
    "0d004c1a1a68996c082bb73543250d52da8d5381b13107e35576488871894023");
  const state = specimen();
  assert.equal(state.$("h1").length, 1);
  assert.equal(state.$("h1").text().trim(), originalRow.address);
  assert.equal(state.$(".hero__sub-title").text().trim(), "Dental Ready Office Space for Lease");
  assert.equal(state.record.propertyListing["@type"], "Place");
  assert.deepEqual(state.record.propertyListing.category, ["office.medical"]);
  assert.equal(state.record.offers.businessFunction, "lease");
  assert.equal(state.record.address.streetAddress, originalRow.address);
  assert.equal(state.record.propertyListing.address.streetAddress, originalRow.address);
  assert.equal(poiScripts(state).length, 2);
  assert.equal(poiScripts(state).toArray().filter(node => /\bpoi\s*:\s*\[\]/.test(state.$(node).text())).length, 2);
});

test("authentic 17665 complete physical H1 and affirmative purpose hero establish the official identity", () => {
  const base = avisonYoungBaseListing(originalRow, new Map());
  const before = digest({ base, originalRow });
  const url = originalRow.external_url;
  assert.doesNotThrow(() => assertAvisonYoungDetailDoc({
    rawHtml: originalHtml, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url, url },
  }, url, base, { matchingExternalUrlRowIds: ["17665"] }));
  assert.equal(digest({ base, originalRow }), before);
});

for (const hero of ["Office Space for Lease", "Office Suites for Lease", "Medical Ready Office Space for Lease", "Dental Ready Office Space for Lease"]) {
  test(`complete physical witness supports the finite purpose ${hero}`, () => {
    const state = specimen();
    state.$(".hero__sub-title").text(hero);
    assert.doesNotThrow(() => check(state));
  });
}

for (const hero of [
  "Premium Office Space for Lease", "Luxury Office Suites for Lease", "Dental Ready Retail Space for Lease",
  "Medical Ready Industrial Space for Lease", "Office Space for Sale", "Office Space for Sale or Lease",
  "Office Space for Lease or Sale", "Office Space", "Office Space Available", "Office Space for Rent",
  "Office Space for Lease in Aliso Viejo", "Aliso Viejo Office Space for Lease", "2127 SF Office Space for Lease",
  "Office Space at 24541 Pacific Park Drive for Lease", "No Office Space for Lease",
  "Office Space Not Available for Lease", "Previously Office Space for Lease", "Withdrawn Office Space for Lease",
  "Leased Office Space for Lease", "Office Space for Lease - No Longer Available",
]) {
  test(`physical witness never makes an unsupported or conflicting purpose advisory: ${hero}`, () => {
    const state = specimen();
    state.$(".hero__sub-title").text(hero);
    assert.throws(() => check(state), /identity/);
  });
}

const mutations: Array<[string, (state: Specimen) => void]> = [
  ["foreign-h1", s => { s.$("h1").text("24542 Pacific Park Drive, Aliso Viejo, CA 92656"); }],
  ["missing-h1", s => { s.$("h1").remove(); }],
  ["duplicate-h1", s => { s.$("h1").first().after(s.$("h1").first().clone()); }],
  ["missing-hero", s => { s.$(".hero__sub-title").remove(); }],
  ["duplicate-hero", s => { s.$(".hero__sub-title").first().after(s.$(".hero__sub-title").first().clone()); }],
  ["foreign-title", s => { s.$("title").text("24542 Pacific Park Drive, Aliso Viejo, CA 92656"); }],
  ["foreign-og-title", s => { s.$('meta[property="og:title"]').attr("content", "Different property"); }],
  ["foreign-canonical", s => { s.$('link[rel="canonical"]').attr("href", "https://www.avisonyoung.us/properties/other"); }],
  ["missing-canonical", s => { s.$('link[rel="canonical"]').remove(); }],
  ["foreign-og-url", s => { s.$('meta[property="og:url"]').attr("content", "https://www.avisonyoung.us/properties/other"); }],
  ["foreign-observed-url", s => { s.metadataUrl = "https://www.avisonyoung.us/properties/other"; }],
  ["missing-remote", s => { s.$("script").filter((_i, n) => /\/\*\s*remote\s*:/.test(s.$(n).text())).remove(); }],
  ["wrong-remote", s => { const n = s.$("script").filter((_i, n) => /\/\*\s*remote\s*:/.test(s.$(n).text())).first(); n.text(n.text().replace(/remote\s*:\s*17665/, "remote: 17666")); }],
  ["duplicate-remote", s => { const n = s.$("script").filter((_i, n) => /\/\*\s*remote\s*:/.test(s.$(n).text())).first(); n.after(n.clone()); }],
  ["missing-listing", s => { s.node.remove(); }],
  ["duplicate-listing", s => { s.node.after(s.node.clone()); }],
  ["foreign-listing-name", s => { s.record.name = "Different property"; }],
  ["foreign-listing-url", s => { s.record.url = "https://www.avisonyoung.us/properties/other"; }],
  ["missing-owner", s => { s.owners = []; }],
  ["other-owner", s => { s.owners = ["17666"]; }],
  ["multiple-owners", s => { s.owners = ["17665", "17666"]; }],
  ["missing-one-address", s => { delete s.record.address; }],
  ["missing-both-addresses", s => { delete s.record.address; delete s.record.propertyListing.address; }],
  ["array-instead-of-address", s => { s.record.address = [s.record.address, structuredClone(s.record.address)]; }],
  ["foreign-typed-street", s => { s.record.address.streetAddress = "24542 Pacific Park Drive"; }],
  ["foreign-typed-city", s => { s.record.propertyListing.address.addressLocality = "Irvine"; }],
  ["foreign-typed-state", s => { s.record.address.addressRegion = "NV"; }],
  ["wrong-typed-zip", s => { s.record.propertyListing.address.postalCode = "92657"; }],
  ["missing-typed-zip", s => { delete s.record.address.postalCode; }],
  ["missing-feed-zip", s => { delete s.row.zip; }],
  ["malformed-feed-zip", s => { s.row.zip = "9265"; }],
  ["wrong-typed-geo", s => { s.record.propertyListing.geo.latitude += 0.01; }],
  ["missing-typed-geo", s => { delete s.record.propertyListing.geo; }],
  ["string-typed-geo", s => { s.record.propertyListing.geo.latitude = String(s.record.propertyListing.geo.latitude); }],
  ["nonfinite-feed-geo", s => { s.row.location.lat = null; }],
  ["wrong-place-type", s => { s.record.propertyListing["@type"] = "Accommodation"; }],
  ["conflicting-category", s => { s.record.propertyListing.category = ["retail.storefront"]; }],
  ["multiple-categories", s => { s.record.propertyListing.category.push("retail.storefront"); }],
  ["medical-modifier-on-generic-office", s => { s.row.type = ["office.professional"]; s.record.propertyListing.category = ["office.professional"]; }],
  ["medical-modifier-on-retail", s => { s.row.type = ["retail.storefront"]; s.record.propertyListing.category = ["retail.storefront"]; }],
  ["opposing-feed-tenure", s => { s.row.transaction = ["sale"]; }],
  ["dual-feed-tenure", s => { s.row.transaction = ["sale", "lease"]; }],
  ["opposing-structured-tenure", s => { s.record.offers.businessFunction = "sale"; }],
  ["missing-structured-tenure", s => { delete s.record.offers.businessFunction; }],
  ["lost-building-overview", s => { s.$(".building__overview-text").remove(); }],
  ["foreign-description", s => {
    const node = s.$('meta[property="og:description"]');
    assert.equal(node.length, 1);
    const before = node.attr("content");
    node.attr("content", "For lease - 24542 Pacific Park Drive, Aliso Viejo, CA 92656");
    assert.notEqual(node.attr("content"), before);
  }],
  ["malformed-second-poi", s => { replacePoi(s, 1, "nullish"); }],
  ["conflicting-second-poi", s => { replacePoi(s, 1, JSON.stringify([{ name: s.row.name, address: "24542 Pacific Park Drive", lat: s.row.location.lat, lng: s.row.location.lng }])); }],
  ["wrong-poi-geo", s => { replacePoi(s, 0, JSON.stringify([{ name: s.row.name, address: s.row.address, lat: s.row.location.lat + 0.01, lng: s.row.location.lng }])); }],
];
for (const [name, mutate] of mutations) {
  test(`official physical-purpose evidence refuses ${name}`, () => {
    const state = specimen();
    const fingerprint = () => digest({ row: state.row, record: state.record, html: state.$.html(), owners: state.owners, metadataUrl: state.metadataUrl });
    const before = fingerprint();
    mutate(state);
    assert.notEqual(fingerprint(), before, "negative mutation must change an actual witness");
    assert.throws(() => check(state), /identity/);
  });
}

for (const location of ["listing", "place"] as const) {
  for (const value of ["For lease - 24542 Pacific Park Drive, Aliso Viejo, CA 92656", { text: "malformed typed description" }]) {
    test(`official physical-purpose typed description refuses ${location} ${typeof value === "string" ? "foreign address" : "nonstring value"}`, () => {
      const state = specimen();
      const target = location === "listing" ? state.record : state.record.propertyListing;
      const before = digest(target);
      target.description = value;
      assert.notEqual(digest(target), before);
      assert.throws(() => check(state), /identity/);
    });
  }
}

test("a populated POI may repeat the independently complete physical witness", () => {
  const state = specimen();
  replacePoi(state, 0, JSON.stringify([{ name: state.row.name, address: state.row.address, lat: state.row.location.lat, lng: state.row.location.lng }]));
  assert.doesNotThrow(() => check(state));
});

test("complete-feed ownership includes inactive and other-tenure collisions", () => {
  const other = { ...structuredClone(originalRow), id: 17666, status: "inactive", transaction: ["sale"] };
  const rows = [structuredClone(originalRow), other];
  const owners = indexAvisonYoungOfficialFeedUrls(rows).get(avisonYoungOfficialUrlKey(originalRow.external_url)!)!;
  assert.deepEqual(owners, ["17665", "17666"]);
  const state = specimen();
  state.owners = owners;
  assert.throws(() => check(state), /identity/);
});

for (const address of ["24541-24543 Pacific Park Drive", "24541 Pacific Park Drive Suite 2", "Hwy 68 & i-75"]) {
  test(`purpose text cannot authorize a different range, unit or intersection scope: ${address}`, () => {
    const state = specimen();
    const full = `${address}, Aliso Viejo, CA 92656`;
    state.row.address = full;
    state.row.name = full;
    state.record.name = full;
    state.record.address.streetAddress = full;
    state.record.propertyListing.address.streetAddress = full;
    state.$("title, h1").text(full);
    state.$('meta[property="og:title"]').attr("content", full);
    state.$('meta[name="description"], meta[property="og:description"], meta[name="twitter:description"]').attr("content", `For lease - ${full}, Aliso Viejo, CA 92656`);
    assert.throws(() => check(state), /identity/);
  });
}

test("real fresh property enrichment and canonical staging conserve the full composite source identity", async () => {
  const keys = ["CRE_REQUIRE_FRESH_DETAILS", "CRE_REQUIRE_FRESH_PROPERTY_DETAILS", "CRE_REFRESH_GENERATION", "CRE_REFRESH_STARTED_AT", "AVISON_YOUNG_DETAIL_TRANSPORT"];
  const previous = new Map(keys.map(key => [key, process.env[key]]));
  const base = avisonYoungBaseListing(structuredClone(originalRow), new Map());
  const before = digest(base);
  const urls: string[] = [];
  try {
    delete process.env.CRE_REQUIRE_FRESH_DETAILS;
    process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS = "1";
    process.env.CRE_REFRESH_GENERATION = "synthetic-physical-purpose-generation";
    process.env.CRE_REFRESH_STARTED_AT = "2026-10-20T00:00:00.000Z";
    process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
    const listing = await enrichAvisonYoungListing(base, false, async url => {
      assert.ok([originalRow.url, originalRow.external_url].includes(url));
      urls.push(url);
      return fetchAvisonYoungDirectDoc(url, 30_000, async () => ["93.184.216.34"],
        async () => ({ status: 200, location: null, body: originalHtml }));
    }, { matchingExternalUrlRowIds: ["17665"] });
    assert.deepEqual(urls, [originalRow.url, originalRow.external_url]);
    assert.equal(listing.detailError, undefined);
    assert.equal(listing.detailUnavailable, undefined);
    // The deliberately rejected native attempt keeps the established property-
    // fallback child safeguard; official association does not authorize replacement.
    assert.equal(listing.preserveChildCollections, true);
    assert.equal(listing.street, originalRow.address);
    assert.equal(listing.name, originalRow.name);
    assert.deepEqual(listing.rawSharpLaunch, base.rawSharpLaunch);
    assert.equal(digest(base), before);
    assert.equal(listing.detailJsonLd.address.streetAddress, originalRow.address);
    assert.equal(listing.detailJsonLd.propertyListing.address.streetAddress, originalRow.address);
    assert.equal(listing.freshnessProvenance.detailScope, "detail_page");
    assert.equal(listing.freshnessProvenance.generationId, "synthetic-physical-purpose-generation");
    assert.equal(listing.freshnessProvenance.cacheDisposition, "live");
    assert.match(listing.markdown, /Pacific Park/);
    const staged = spawnSync("python3", ["-c", "import json,sys; from cre_ingest import to_row; print(json.dumps(to_row(json.load(sys.stdin),{},'2026-10-20T00:01:00Z'),default=list))"], {
      cwd: fileURLToPath(new URL("../../../", import.meta.url)),
      input: JSON.stringify({ ...listing, sourceKey: "avison-young", transactionMode: "lease" }),
      encoding: "utf8", timeout: 10_000, maxBuffer: 256 * 1024, env: { ...process.env, PYTHONPATH: "." },
    });
    assert.equal(staged.error, undefined);
    assert.equal(staged.status, 0, staged.stderr);
    const row = JSON.parse(staged.stdout);
    assert.ok(row);
    assert.equal(row.external_id, "17665");
    assert.equal(row.transaction_type, "lease");
    assert.equal(row.address, originalRow.address);
    assert.equal(row.source_url, originalRow.external_url);
    assert.deepEqual(row.raw_data.rawSharpLaunch, listing.rawSharpLaunch);
    assert.equal(row.raw_data.preserveChildCollections, true);
    assert.equal(row.raw_data.detailJsonLd.address.streetAddress, originalRow.address);
    assert.equal(row.raw_data.detailJsonLd.propertyListing.address.streetAddress, originalRow.address);
    assert.equal(row.raw_data.freshnessProvenance.generationId, "synthetic-physical-purpose-generation");
  } finally {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
  }
});
