import test from "node:test";
import assert from "node:assert/strict";
import * as cheerio from "cheerio";
import { transwesternDocumentMediaObservations as observe } from "../../../sources/pure/transwestern-document-media-observations.js";
import { prune } from "../../../lib/util.js";
import { enrichTranswesternListing } from "../../../sources/transwestern.js";
import { firecrawl } from "../../../lib/scrape.js";
import { createHash } from "node:crypto";

const base = "https://transwestern.com/property/example";
const docs = (...urls: string[]) => urls.map(url => ({ url, title: null, docType: "other" as const }));
const media = (...urls: string[]) => urls.map(url => ({ url, mediaType: "video" as const, provider: null, embedUrl: null, title: null }));
const links = (...urls: string[]) => urls.map(url => ({ url, linkType: "other" as const, rel: null }));
const collect = (html: string, documents = docs(), videos = media(), otherLinks = links()) => observe(cheerio.load(html), documents, videos, otherLinks, base, "a".repeat(64), "2026-10-29T00:00:00Z");

test("records distinct property and footer occurrences without changing child arrays", () => {
  const url = "https://transwestern.com/file.pdf";
  const video = "https://www.youtube.com/watch?v=abcdefghijk";
  const html = `<section class="property-dtl"><div class="fix-wrap"><div class="property-dtl-inner"><div class="available-space availablespace-table"><div class="availablespace-main"><table id="tblAttachments"><tr><td><a href="/file.pdf">File</a></td></tr></table></div></div></div></div></section>
    <section class="cmn-banner bnr-inner property-dtl-bnr"><div class="fix-wrap"><div class="banner-caption"><a class="download-flyer-btn" href="/file.pdf">Flyer</a></div></div></section>
    <footer><section class="footer-links"><a href="/file.pdf">Footer</a><a href="${video}">Video</a></section></footer>`;
  const d = docs(url), m = media(video), before = JSON.stringify([d, m]);
  const result = collect(html, d, m);
  assert.equal(JSON.stringify([d, m]), before);
  assert.equal(result.candidateCount, 2);
  assert.deepEqual(result.occurrences.map(x => [x.channel, x.role, x.basis]), [
    ["document", "property", "property_attachment_table"], ["document", "property", "property_banner_download"],
    ["document", "sitewide", "footer_navigation"], ["media", "sitewide", "footer_navigation"],
  ]);
  assert.equal(result.occurrenceCount, 4);
});

test("observes an existing extensionless link only when matched by a download affordance", () => {
  const target = "https://transwestern.com/property/download/123";
  const other = "https://transwestern.com/general";
  const html = `<section class="cmn-banner bnr-inner property-dtl-bnr"><div class="fix-wrap"><div class="banner-caption"><a class="download-flyer-btn" href="/property/download/123">Download</a></div></div></section><a href="${other}">Other</a>`;
  const childLinks = links(target, other), before = JSON.stringify(childLinks);
  const result = collect(html, docs(), media(), childLinks);
  assert.equal(JSON.stringify(childLinks), before);
  assert.equal(result.candidateCount, 1);
  assert.equal(result.eligibleLinkCandidateCount, 1);
  assert.deepEqual(result.occurrences.map(x => [x.url, x.channel, x.role, x.basis]), [[target, "link", "property", "property_banner_download"]]);
  const both = collect(html, docs(target), media(), childLinks);
  assert.deepEqual(both.occurrences.map(x => x.channel), ["document", "link"]);
  assert.equal(collect(html).eligibleLinkCandidateCount, 0);
  const invalid = collect(html, docs(), media(), [null, { url: "not-a-url" }, { url: "https://user:pass@example.com/" }] as any);
  assert.equal(invalid.skippedLinkEntryCount, 3);
  assert.equal(invalid.state, "incomplete");
  assert.equal(invalid.eligibleLinkCandidateCount, 0);
});

test("ambiguous, concealed and unmatched candidates remain unknown without case or video-alias inference", () => {
  const a = "https://transwestern.com/File.pdf", b = "https://transwestern.com/file.pdf";
  const watch = "https://www.youtube.com/watch?v=abcdefghijk";
  const html = `<section class="property-dtl"><table id="tblAttachments"><tr><td><a href="${a}">x</a></td></tr></table></section>
    <section class="property-dtl"></section><footer hidden><section class="footer-links"><a href="${b}">x</a></section></footer>
    <iframe src="https://www.youtube.com/embed/abcdefghijk"></iframe>`;
  const result = collect(html, docs(a, b), media(watch));
  assert.equal(result.candidateCount, 3);
  assert.deepEqual(result.occurrences.map(x => x.role), ["unknown", "unknown", "unknown"]);
  assert.equal(result.occurrences[2].basis, "no_matching_dom_occurrence");
});

test("recognizes the witnessed DataTables wrapper path and refuses concealed or ambiguous tables", () => {
  const make = (extra = "") => `<section class="property-dtl"><div class="fix-wrap"><div class="property-dtl-inner"><div class="available-space availablespace-table"><div class="availablespace-main"><div class="dataTables_wrapper"><div class="dataTables_scroll"><div class="dataTables_scrollBody" ${extra}><table id="tblAttachments"><tr><td><a href="/file.pdf">File</a></td></tr></table></div></div></div></div></div></div></div></section>`;
  const d = docs("https://transwestern.com/file.pdf");
  assert.equal(collect(make(), d).occurrences[0].basis, "property_attachment_table");
  assert.equal(collect(make("hidden"), d).occurrences[0].role, "unknown");
  assert.equal(collect(make() + make(), d).occurrences[0].role, "unknown");
});

test("bounds output and preserves empty and omitted counts through prune", () => {
  const empty = collect("");
  assert.equal((prune(empty) as any).occurrenceCount, 0);
  const many = collect('<footer><section class="footer-links">' + '<a href="/file.pdf">x</a>'.repeat(1001) + '</section></footer>', docs("https://transwestern.com/file.pdf"));
  assert.equal(many.state, "incomplete");
  assert.equal(many.occurrenceCount, 1000);
  assert.equal(many.omittedCount, 1);
  const unsafe = collect("", docs("https://user:pass@example.com/x.pdf", "https://example.com/" + "x".repeat(4096)));
  assert.equal(unsafe.skippedCandidateCount, 2);
  assert.equal(unsafe.state, "incomplete");
});

test("accepted enrichment binds role evidence to the detail while retaining document children", async () => {
  const original = firecrawl.scrape;
  const html = '<h1>Sample Property</h1><section class="property-dtl"><div class="fix-wrap"><div class="property-dtl-inner"><div class="available-space availablespace-table"><div class="availablespace-main"><table id="tblAttachments"><tr><td><a class="download-att-btn" href="/file.pdf">File</a></td></tr></table></div></div></div></div></section>';
  (firecrawl as any).scrape = async () => ({ rawHtml: html, markdown: "Sample Property", links: ["https://transwestern.com/file.pdf"] });
  try {
    const listing = await enrichTranswesternListing({ PageUrl: "sample-property", BuildingName: "Sample Property", FullAddress: "100 Main Street" }, "Sale", "sale", false);
    assert.equal(listing.documents[0].url, "https://transwestern.com/file.pdf");
    const evidence = listing.transwesternDocumentMediaObservations;
    assert.equal(evidence.sourceUrl, listing.url);
    assert.equal(evidence.bodySha256, createHash("sha256").update(html).digest("hex"));
    assert.equal(evidence.observedAt, listing.detailObservedAt);
    assert.equal(evidence.occurrences[0].basis, "property_attachment_table");
  } finally { (firecrawl as any).scrape = original; }
});
