import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing } from "../../../sources/avison-young.js";

const row = JSON.parse(readFileSync(new URL("../../fixtures/raw_data/avison-4294-feed-17551.json", import.meta.url), "utf8"));
const original = readFileSync(new URL("../../fixtures/raw_data/avison-4294-official-17551.html", import.meta.url), "utf8");

function verify(html: string, feed = row, owners = ["17551"]) {
  const base = avisonYoungBaseListing(feed, new Map());
  const url = feed.external_url;
  return assertAvisonYoungDetailDoc(
    { rawHtml: html, markdown: "", links: [], metadata: { statusCode: 200, sourceURL: url, url } },
    url, base, { matchingExternalUrlRowIds: owners },
  );
}

test("source suite offering recognizes uniquely bound official identity", () => {
  assert.doesNotThrow(() => verify(original));
});

test("suite offering rejects conflicting URL ownership, remote ID, address and transaction", () => {
  assert.throws(() => verify(original, row, ["17551", "another"]));
  assert.throws(() => verify(original.replace("remote: 17551", "remote: 17552")));
  assert.throws(() => verify(original, { ...row, transaction: ["sale"] }));
  const $ = cheerio.load(original);
  const record = JSON.parse($('script[type="application/ld+json"]').text());
  record.propertyListing.address.postalCode = "00000";
  $('script[type="application/ld+json"]').text(JSON.stringify(record));
  assert.throws(() => verify($.html()));
});

test("suite witness requires the same subject, complete address and matching category", () => {
  for (const mutate of [
    ($: cheerio.CheerioAPI) => $(".hero__sub-title").text("Other Bank | Suite 200"),
    ($: cheerio.CheerioAPI) => $("h1").text("4,397 SF Office Space For Sale"),
    ($: cheerio.CheerioAPI) => $('meta[property="og:description"]').attr("content", "For lease - Prosperity Bank, 721 Hwy 290 W, Dripping Springs, TX 00000"),
    ($: cheerio.CheerioAPI) => {
      const el = $('script[type="application/ld+json"]');
      const record = JSON.parse(el.text());
      record.propertyListing.category = ["retail.store"];
      el.text(JSON.stringify(record));
    },
  ]) {
    const $ = cheerio.load(original);
    mutate($);
    assert.throws(() => verify($.html()));
  }
});
