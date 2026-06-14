// Isolate argv before jll-investor.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import {
  JLL_INVESTOR_HOST,
  jllInvestorNextData,
  jllInvestorUrlFromAlias,
  jllInvestorSitemapUrls,
  jllInvestorSitemapCandidateLimit,
  jllInvestorStatus,
  jllInvestorSearchListing,
  jllInvestorDocumentUrls,
  jllInvestorImageUrls,
  jllInvestorContacts,
} from "../../../sources/jll-investor.js";

test("jllInvestorNextData parses __NEXT_DATA__ JSON from HTML", () => {
  const html = `
    <html><body>
      <script id="__NEXT_DATA__" type="application/json">
        {"props":{"pageProps":{"initialState":{"pdp":{"listing":{"id":"SF-001"}}}}}}
      </script>
    </body></html>
  `;
  const data = jllInvestorNextData(html);
  assert.equal(data?.props?.pageProps?.initialState?.pdp?.listing?.id, "SF-001");
  assert.equal(jllInvestorNextData("<html></html>"), null);
});

test("jllInvestorUrlFromAlias resolves slug, path, and absolute URLs", () => {
  assert.equal(
    jllInvestorUrlFromAlias("multifamily/dallas-portfolio"),
    `${JLL_INVESTOR_HOST}/us/en/listings/multifamily/dallas-portfolio`
  );
  assert.equal(
    jllInvestorUrlFromAlias("/us/en/listings/office/chicago-tower"),
    `${JLL_INVESTOR_HOST}/us/en/listings/office/chicago-tower`
  );
  assert.equal(
    jllInvestorUrlFromAlias("https://invest.jll.com/us/en/listings/retail/austin"),
    "https://invest.jll.com/us/en/listings/retail/austin"
  );
  assert.equal(jllInvestorUrlFromAlias(null), null);
});

test("jllInvestorSitemapUrls extracts locale sitemap links from index HTML", () => {
  const html = `
    <sitemapindex>
      <sitemap><loc>https://invest.jll.com/us/sitemap-us.xml</loc></sitemap>
      <sitemap><loc>https://invest.jll.com/gb/sitemap-gb.xml</loc></sitemap>
      <sitemap><loc>https://invest.jll.com/us/sitemap-us.xml</loc></sitemap>
    </sitemapindex>
  `;
  assert.deepEqual(jllInvestorSitemapUrls(html), [
    "https://invest.jll.com/us/sitemap-us.xml",
    "https://invest.jll.com/gb/sitemap-gb.xml",
  ]);
});

test("jllInvestorSitemapCandidateLimit applies max heuristics when scan limit is unset", () => {
  assert.equal(jllInvestorSitemapCandidateLimit(10, 500), 80);
  assert.equal(jllInvestorSitemapCandidateLimit(0, 40), 26);
  assert.equal(jllInvestorSitemapCandidateLimit(Number.POSITIVE_INFINITY, 200), 200);
});

test("jllInvestorStatus prefers under-contract flag then stage name", () => {
  assert.equal(jllInvestorStatus({ isUnderContract: true, stageName: "Active" }), "Under Contract");
  assert.equal(jllInvestorStatus({ stageName: "Closed" }), "Closed");
  assert.equal(jllInvestorStatus({}), "Active");
});

test("jllInvestorSearchListing maps search API row to listing shape", () => {
  const listing = jllInvestorSearchListing({
    id: "a0B3x000001",
    alias: "multifamily/dallas-portfolio",
    name: "Dallas Portfolio",
    assetType: "Multifamily",
    displayAddress: "100 Main St",
    city: "Dallas",
    state: "TX",
    country: "United States",
    latitude: 32.78,
    longitude: -96.8,
    numberOfUnits: "240 units",
    image: "https://cdn.example/hero.jpg",
    isUnderContract: false,
    stageName: "Active",
  });
  assert.equal(listing.id, "a0B3x000001");
  assert.equal(listing.transactionType, "Sale (investment)");
  assert.equal(listing.country, "US");
  assert.equal(listing.url, `${JLL_INVESTOR_HOST}/us/en/listings/multifamily/dallas-portfolio`);
  assert.deepEqual(listing.photos, ["https://cdn.example/hero.jpg"]);
  assert.equal(listing.status, "Active");
});

test("jllInvestorDocumentUrls collects nested https document links", () => {
  const urls = jllInvestorDocumentUrls({
    documents: {
      teaser: { url: "https://cdn.example/teaser.pdf" },
      nested: [{ url: "https://cdn.example/flyer.pdf" }, "mailto:broker@jll.com"],
      deep: { child: { url: "https://cdn.example/deep.pdf" } },
    },
  });
  assert.deepEqual(urls, [
    "https://cdn.example/teaser.pdf",
    "https://cdn.example/flyer.pdf",
    "https://cdn.example/deep.pdf",
  ]);
});

test("jllInvestorImageUrls merges primary, multimedia, and fallback images", () => {
  const urls = jllInvestorImageUrls(
    {
      image: "https://cdn.example/primary.jpg",
      multimedia: { images: ["https://cdn.example/gallery-1.jpg", "https://cdn.example/primary.jpg"] },
    },
    ["https://cdn.example/fallback.jpg"]
  );
  assert.deepEqual(urls, [
    "https://cdn.example/primary.jpg",
    "https://cdn.example/gallery-1.jpg",
    "https://cdn.example/fallback.jpg",
  ]);
});

test("jllInvestorContacts maps brokers and dedupes by email", () => {
  const contacts = jllInvestorContacts({
    brokers: [
      { name: "Alex Broker", email: "alex@jll.com", title: "EVP", phone: "555-1000" },
      { name: "Alex Broker", email: "alex@jll.com", title: "EVP" },
      { name: "Sam Broker", email: "sam@jll.com", linkedInURL: "https://linkedin.com/in/sam" },
    ],
  });
  assert.equal(contacts.length, 2);
  assert.equal(contacts[0]?.company, "JLL");
  assert.equal(contacts[0]?.title, "EVP");
  assert.equal(contacts[1]?.linkedInUrl, "https://linkedin.com/in/sam");
});

test("jllInvestorContacts returns empty array when brokers missing", () => {
  assert.deepEqual(jllInvestorContacts({}), []);
  assert.deepEqual(jllInvestorContacts({ brokers: null }), []);
});
