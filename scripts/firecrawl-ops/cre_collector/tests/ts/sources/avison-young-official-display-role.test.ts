import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import {
  assertAvisonYoungDetailDoc,
  avisonYoungBaseListing,
  avisonYoungOfficialUrlKey,
  indexAvisonYoungOfficialFeedUrls,
} from "../../../sources/avison-young.js";

const fixtureDir = new URL("../../fixtures/raw_data/", import.meta.url);
const displayRole17752 = JSON.parse(readFileSync(new URL("avison-young-official-display-role-17752.json", fixtureDir), "utf8"));
const displayRole17752Html = readFileSync(new URL("avison-young-official-display-role-17752.html", fixtureDir), "utf8");
const evidence = JSON.parse(readFileSync(new URL("avison-young-official-display-role-18194.json", fixtureDir), "utf8"));
const html = readFileSync(new URL("avison-young-official-display-role-18194.html", fixtureDir), "utf8");
const base = avisonYoungBaseListing(evidence.feed_row, new Map());
const officialUrlIndex = indexAvisonYoungOfficialFeedUrls([evidence.feed_row]);
const responseUrlKey = avisonYoungOfficialUrlKey(evidence.response_url);
assert.ok(responseUrlKey);
const matchingFeedContext = {
  matchingExternalUrlRowIds: officialUrlIndex.get(responseUrlKey) ?? [],
};

function makeDoc(rawHtml: string) {
  return {
    rawHtml,
    markdown: "",
    links: [],
    metadata: {
      statusCode: 200,
      sourceURL: evidence.response_url,
      url: evidence.response_url,
    },
  };
}

function assertRejected(rawHtml = html, context: typeof matchingFeedContext | null = matchingFeedContext) {
  assert.throws(
    () => assertAvisonYoungDetailDoc(makeDoc(rawHtml), evidence.response_url, base, context ?? undefined),
    /identity does not match/
  );
}

test("registered official exact subject and locality-offering display is bound by the full feed URL set", () => {
  assert.ok(assertAvisonYoungDetailDoc(makeDoc(html), evidence.response_url, base, matchingFeedContext));
});

test("exact feed subject with a complete locality hero does not require offering copy", () => {
  const localityOnly = html.replace(
    "Chicago, Illinois - Available for Sale or Lease",
    "Chicago, Illinois"
  );
  assert.ok(assertAvisonYoungDetailDoc(makeDoc(localityOnly), evidence.response_url, base, matchingFeedContext));
});

test("official URL index uses all feed rows, even inactive duplicates, with trailing-slash identity", () => {
  const duplicate = { ...evidence.feed_row, id: 18195, status: "inactive", external_url: `${evidence.response_url}/` };
  const allRows = indexAvisonYoungOfficialFeedUrls([evidence.feed_row, duplicate]);
  assert.deepEqual(allRows.get(responseUrlKey), ["18194", "18195"]);
  assertRejected(html, { matchingExternalUrlRowIds: allRows.get(responseUrlKey) ?? [] });
  const sameIdentityCopy = { ...evidence.feed_row, status: "inactive", external_url: `${evidence.response_url}/` };
  const sameIdentityRows = indexAvisonYoungOfficialFeedUrls([evidence.feed_row, sameIdentityCopy]);
  assert.deepEqual(sameIdentityRows.get(responseUrlKey), ["18194"]);
  assert.ok(assertAvisonYoungDetailDoc(makeDoc(html), evidence.response_url, base,
    { matchingExternalUrlRowIds: sameIdentityRows.get(responseUrlKey) ?? [] }));
});

test("official display witness refuses missing, mismatched, or duplicate feed URL ownership", () => {
  assertRejected(html, null);
  assertRejected(html, { matchingExternalUrlRowIds: [] });
  assertRejected(html, { matchingExternalUrlRowIds: ["18195"] });
  assertRejected(html, { matchingExternalUrlRowIds: ["18194", "18194"] });
  assertRejected(html, { matchingExternalUrlRowIds: ["18194", "18195"] });
});

test("official display witness requires a present matching ZIP on every structured address", () => {
  const missingZip = html.replaceAll('"postalCode":"60632"', '"postalCode":""');
  const wrongZip = html.replaceAll('"postalCode":"60632"', '"postalCode":"60633"');
  const oneWrongAddress = html.replace('"postalCode":"60632"', '"postalCode":"60633"');
  const wrongStreet = html.replaceAll("4235 West Ann Lurie Place", "4235 West Ann Lurie Place Unit 2");
  assertRejected(missingZip);
  assertRejected(wrongZip);
  assertRejected(oneWrongAddress);
  assertRejected(wrongStreet);
});

test("official display witness rejects lossy punctuation in structured locality claims", () => {
  const localityPunctuation = html.replaceAll('"addressLocality":"Chicago"', '"addressLocality":"Chicago#"');
  const statePunctuation = html.replaceAll('"addressRegion":"IL"', '"addressRegion":"Illinois+"');
  const unicodeSeparator = html.replaceAll('"addressLocality":"Chicago"', '"addressLocality":"Chicago"');
  assertRejected(localityPunctuation);
  assertRejected(statePunctuation);
  assertRejected(unicodeSeparator);
});

test("official display witness rejects partial physical claims and conflicting visible addresses", () => {
  assertRejected(html.replace("4235 W Ann Lurie Place", "4235 W Ann Lurie Place #2"));
  assertRejected(html.replace("4235 W Ann Lurie Place", "Unrelated Property"));
  assertRejected(html.replace("Chicago, Illinois - Available for Sale or Lease", "Chicago, Wisconsin - Available for Sale or Lease"));
  assertRejected(html.replace("Chicago, Illinois - Available for Sale or Lease", "Chicago#, Illinois - Available for Sale or Lease"));
  assertRejected(html.replace("Chicago, Illinois - Available for Sale or Lease", "Chicago, Illinois+ - Available for Sale or Lease"));
  assertRejected(html.replace("Chicago, Illinois - Available for Sale or Lease", "Chicago Unit 2, Illinois - Available for Sale or Lease"));
  assertRejected(html.replace(
    "48,456 sf, well-maintained food/cold storage distribution facility.",
    "Other Warehouse, 999 Wrong Road, Kenosha, WI 53140"
  ));
  assertRejected(html.replace(
    "48,456 sf, well-maintained food/cold storage distribution facility.",
    "Other Warehouse, 4235 West Ann Lurie Place & 999 Other Road, Kenosha, WI 53140"
  ));
});

function assertFixtureAccepted(id: string) {
  const fixtureEvidence = JSON.parse(readFileSync(new URL(`avison-young-official-display-role-${id}.json`, fixtureDir), "utf8"));
  const fixtureHtml = readFileSync(new URL(`avison-young-official-display-role-${id}.html`, fixtureDir), "utf8");
  const fixtureBase = avisonYoungBaseListing(fixtureEvidence.feed_row, new Map());
  const urlKey = avisonYoungOfficialUrlKey(fixtureEvidence.response_url);
  assert.ok(urlKey);
  const rowsByUrl = indexAvisonYoungOfficialFeedUrls([fixtureEvidence.feed_row]);
  const context = { matchingExternalUrlRowIds: rowsByUrl.get(urlKey) ?? [] };
  const fixtureDoc = {
    rawHtml: fixtureHtml,
    markdown: "",
    links: [],
    metadata: { statusCode: 200, sourceURL: fixtureEvidence.response_url, url: fixtureEvidence.response_url },
  };
  return { fixtureEvidence, fixtureHtml, fixtureBase, fixtureDoc, context };
}

test("official display supports captured street-offering, acreage-plus-address, and exact-street/locality roles", () => {
  for (const id of ["18395", "50399", "17752"]) {
    const fixture = assertFixtureAccepted(id);
    try {
      assert.ok(assertAvisonYoungDetailDoc(fixture.fixtureDoc, fixture.fixtureEvidence.response_url,
        fixture.fixtureBase, fixture.context), `fixture ${id}`);
    } catch (error) {
      throw new Error(`fixture ${id}: ${String(error)}`);
    }
  }
});

test("identical subject H1 duplication is allowed, but differing duplicate headings are refused", () => {
  assert.equal((html.match(/<h1\b/g) ?? []).length, 2);
  assert.ok(assertAvisonYoungDetailDoc(makeDoc(html), evidence.response_url, base, matchingFeedContext));
  const mismatchedSecondHeading = html.replace("4235 W Ann Lurie Place</h1>", "Other Property</h1>");
  assertRejected(mismatchedSecondHeading);
});

test("matched street-shaped subjects cannot omit or contradict the feed street", () => {
  const wrongBase = { ...base, name: "Jones Dairy Road", headline: "Jones Dairy Road", street: "416 Jones Dairy Road" };
  const wrongStreetHtml = html
    .replaceAll("4235 W Ann Lurie Place", "Jones Dairy Road")
    .replaceAll("4235 West Ann Lurie Place", "416 Jones Dairy Road");
  assert.throws(() => assertAvisonYoungDetailDoc(makeDoc(wrongStreetHtml), evidence.response_url,
    wrongBase, matchingFeedContext), /identity does not match/);
  const missingNumberHtml = html.replaceAll("4235 W Ann Lurie Place", "4235 W Ann Lurie Place")
    .replaceAll("4235 West Ann Lurie Place", "Ann Lurie Place");
  const missingNumberBase = { ...base, name: "Ann Lurie Place", headline: "Ann Lurie Place", street: "4235 West Ann Lurie Place" };
  assert.throws(() => assertAvisonYoungDetailDoc(makeDoc(missingNumberHtml), evidence.response_url,
    missingNumberBase, matchingFeedContext), /identity does not match/);
  const streetFixture = assertFixtureAccepted("17752");
  assert.throws(() => assertAvisonYoungDetailDoc(
    { ...streetFixture.fixtureDoc, rawHtml: streetFixture.fixtureHtml.replace("800 Corporate Way</h1>", "801 Corporate Way</h1>") },
    streetFixture.fixtureEvidence.response_url, streetFixture.fixtureBase, streetFixture.context
  ), /identity does not match/);
  assert.throws(() => assertAvisonYoungDetailDoc(
    { ...streetFixture.fixtureDoc, rawHtml: streetFixture.fixtureHtml.replace('<h1 class="hero__title', '<h1 class="extra">Other Address</h1><h1 class="hero__title') },
    streetFixture.fixtureEvidence.response_url, streetFixture.fixtureBase, streetFixture.context
  ), /identity does not match/);
});

test("missing new-family structure leaves the established fallback unchanged", () => {
  const $ = cheerio.load(html);
  const ld = $("script[type='application/ld+json']").toArray()
    .map((element) => $(element).html() ?? "")
    .find((value) => value.includes('"@type":"RealEstateListing"'));
  assert.ok(ld);
  const thin = `<!doctype html><html><head>
    <title>${evidence.feed_row.name}</title>
    <meta property="og:title" content="${evidence.feed_row.name}">
    <meta property="og:url" content="${evidence.response_url}">
    <script type="application/ld+json">${ld}</script>
    </head><body>
    <h1>${evidence.feed_row.name}</h1><h1>${evidence.feed_row.name}</h1>
    <div class="hero__sub-title">Chicago, Illinois - Available for Sale or Lease</div>
    </body></html>`;
  const thinDoc = makeDoc(thin);
  const withoutNewFamily = assertAvisonYoungDetailDoc(thinDoc, evidence.response_url, base);
  const withIncompleteNewFamily = assertAvisonYoungDetailDoc(thinDoc, evidence.response_url, base, matchingFeedContext);
  assert.deepEqual(withIncompleteNewFamily, withoutNewFamily);
});


test("address-bearing offer subjects retain and validate supplied locality claims", () => {
  const fixture = assertFixtureAccepted("17752");
  assert.ok(assertAvisonYoungDetailDoc(fixture.fixtureDoc, fixture.fixtureEvidence.response_url,
    fixture.fixtureBase, fixture.context));
  for (const [from, to] of [
    ["800 Corporate Way, Westlake", "800 Corporate Way, Other City"],
    ["800 Corporate Way, Westlake", "800 Corporate Way, Westlake Unit 2"],
    ["800 Corporate Way, Westlake", "800 Corporate Way, Westlake, OH 44146"],
  ]) {
    const changed = fixture.fixtureHtml.replaceAll(from, to);
    assert.throws(() => assertAvisonYoungDetailDoc({
      ...fixture.fixtureDoc, rawHtml: changed,
    }, fixture.fixtureEvidence.response_url, fixture.fixtureBase, fixture.context), /identity/);
  }
});

test("official display witness refuses a wrong street embedded in the owned offer title", () => {
  const feedRow = {
    ...displayRole17752.feed_row,
    name: "For Lease | 801 Corporate Way, Westlake",
    meta_title: "For Lease | 801 Corporate Way, Westlake",
  };
  const base = avisonYoungBaseListing(feedRow, new Map());
  const requestUrl = displayRole17752.response_url;
  const html = displayRole17752Html.replaceAll(
    "For Lease | 800 Corporate Way, Westlake",
    "For Lease | 801 Corporate Way, Westlake",
  );
  assert.throws(() => assertAvisonYoungDetailDoc({
    rawHtml: html, markdown: "", links: [],
    metadata: { statusCode: 200, sourceURL: requestUrl, url: requestUrl },
  }, requestUrl, base, { matchingExternalUrlRowIds: ["17752"] }), /identity/);
});
