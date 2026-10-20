import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing, enrichAvisonYoungListing } from "../../../sources/avison-young.js";

process.argv = [process.argv[0]!, process.argv[1]!];
const fixtures = new URL("../../fixtures/raw_data/", import.meta.url);
const feed = JSON.parse(readFileSync(new URL("avison-4294-feed-17626.json", fixtures), "utf8"));
const html = readFileSync(new URL("avison-4294-official-17626.html", fixtures), "utf8");
const street = feed.address;
const composite = `${street}, ${feed.city}, ${feed.state} ${feed.zip}`;
const context = { matchingExternalUrlRowIds: [String(feed.id)] };

// Only the address representation varies; this is the already accepted
// sublease role, not a new marketing-heading or whole-source acceptance rule.
function represented(address = composite) {
  const row = { ...feed, address };
  const $ = cheerio.load(html);
  const node = $('script[type="application/ld+json"]');
  const record = JSON.parse(node.text());
  record.address.streetAddress = address;
  record.propertyListing.address.streetAddress = address;
  node.text(JSON.stringify(record));
  return { row, $, record, base: avisonYoungBaseListing(row, new Map()) };
}

function check(value = represented(), body = value.$.html()) {
  return assertAvisonYoungDetailDoc({ rawHtml: body, markdown: "", links: [], metadata: {
    statusCode: 200, sourceURL: feed.external_url, url: feed.external_url,
  } }, feed.external_url, value.base, context);
}

test("existing official sublease role compares an exact composite address without changing its source values", () => {
  const value = represented();
  const before = structuredClone(value.base);
  const record = check(value);
  assert.equal(record.address.streetAddress, composite);
  assert.equal(record.propertyListing.address.streetAddress, composite);
  assert.deepEqual(value.base, before);
  assert.equal(value.base.rawSharpLaunch.address, composite);
});

test("composite comparison reuses known full-state and whitespace normalization", () => {
  assert.ok(check(represented(`${street},  Troy  , Michigan 48084`)));
});

test("the established simple-street role remains unchanged", () => {
  assert.ok(check(represented(street)));
});

test("a projected street does not supply a new marketing-hero acceptance role", () => {
  const value = represented();
  value.$("h1").text(composite);
  value.$(".hero__sub-title").text("Dental Ready Office Space for Lease");
  assert.throws(() => check(value), /identity/);
});

for (const [label, address] of [
  ["wrong city", `${street}, Detroit, MI 48084`],
  ["wrong state", `${street}, Troy, TX 48084`],
  ["wrong ZIP", `${street}, Troy, MI 48083`],
  ["truncated ZIP", `${street}, Troy, MI 4808`],
  ["missing ZIP", `${street}, Troy, MI`],
  ["extra locality", `${street}, Troy, Troy, MI 48084`],
  ["unit comma", `${street}, Suite 2, Troy, MI 48084`],
  ["unit prefix", `${street} Unit 2, Troy, MI 48084`],
  ["range prefix", "2600-2602 W Big Beaver Road, Troy, MI 48084"],
  ["intersection prefix", "W Big Beaver Road & Main Street, Troy, MI 48084"],
]) {
  test(`composite comparison preserves refusal for ${label}`, () => {
    assert.throws(() => check(represented(address)), /identity/);
  });
}

test("matching suffix cannot hide a different physical street in either typed address", () => {
  for (const nested of [false, true]) {
    const value = represented();
    const address = nested ? value.record.propertyListing.address : value.record.address;
    address.streetAddress = "2601 W Big Beaver Road, Troy, MI 48084";
    value.$('script[type="application/ld+json"]').text(JSON.stringify(value.record));
    assert.throws(() => check(value), /identity/);
  }
});

test("composite address requires separately bound known locality and five-digit ZIP", () => {
  for (const changes of [{ city: "" }, { state: "" }, { postalCode: "" }, { postalCode: "4808" }]) {
    const value = represented();
    Object.assign(value.base, changes);
    assert.throws(() => check(value), /identity/);
  }
});

test("composite comparison still validates every populated existing-role POI", () => {
  const value = represented();
  const point = { name: feed.name, address: street, lat: value.base.latitude, lng: value.base.longitude };
  const script = value.$("script").filter((_index, node) => value.$(node).text().includes("poi: []"));
  assert.equal(script.length, 1);
  const original = script.text();
  script.text(original.replace("poi: []", `poi: ${JSON.stringify([point])}`));
  assert.ok(check(value));
  script.text(original.replace("poi: []", `poi: ${JSON.stringify([{ ...point, address: "2601 W Big Beaver Road" }])}`));
  assert.throws(() => check(value), /identity/);
});

test("real enrichment and canonical staging retain the original composite feed and typed values", async () => {
  const value = represented();
  const before = structuredClone(value.base);
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  const oldFetch = globalThis.fetch;
  let calls = 0;
  process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  globalThis.fetch = async () => { throw new Error("composite fixture forbids network"); };
  try {
    const listing = await enrichAvisonYoungListing({ ...value.base, sharpLaunchUrl: undefined }, false, async url => {
      assert.equal(url, feed.external_url);
      calls++;
      return { rawHtml: value.$.html(), markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url, url } };
    }, context);
    assert.equal(calls, 1);
    assert.equal(listing.detailError, undefined);
    assert.deepEqual(value.base, before);
    assert.equal(listing.street, composite);
    assert.equal(listing.rawSharpLaunch.address, composite);
    assert.equal(listing.detailJsonLd.address.streetAddress, composite);
    const staged = spawnSync("python3", ["-c", "import json,sys; from cre_ingest import to_row; row=to_row(json.load(sys.stdin),{},'2026-10-20T00:01:00Z'); assert row is not None; print(json.dumps(row,default=list))"], {
      cwd: fileURLToPath(new URL("../../../", import.meta.url)), encoding: "utf8", timeout: 10_000,
      input: JSON.stringify({ ...listing, sourceKey: "avison-young", transactionMode: "lease" }),
      env: { ...process.env, PYTHONPATH: "." }, maxBuffer: 128 * 1024,
    });
    assert.equal(staged.error, undefined);
    assert.equal(staged.status, 0, staged.stderr);
    const row = JSON.parse(staged.stdout);
    assert.equal(row.address, composite);
    assert.equal(row.raw_data.rawSharpLaunch.address, composite);
    assert.equal(row.raw_data.detailJsonLd.address.streetAddress, composite);
  } finally {
    globalThis.fetch = oldFetch;
    if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous;
  }
});
