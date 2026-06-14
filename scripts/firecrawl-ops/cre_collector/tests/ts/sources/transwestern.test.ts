import test from "node:test";
import assert from "node:assert/strict";
import * as cheerio from "cheerio";
import {
  canonicalTranswesternUrl,
  transwesternDetailUrl,
  transwesternTransactionType,
  transwesternSizeText,
  transwesternPriceText,
  parseTranswesternFacts,
  parseTranswesternAvailability,
} from "../../../sources/transwestern.js";

test("canonicalTranswesternUrl resolves relative paths and rejects junk", () => {
  assert.equal(
    canonicalTranswesternUrl("/property/dallas-office"),
    "https://transwestern.com/property/dallas-office"
  );
  assert.equal(canonicalTranswesternUrl("javascript:void(0)"), null);
  assert.equal(canonicalTranswesternUrl("-"), null);
  assert.equal(canonicalTranswesternUrl(null), null);
});

test("transwesternDetailUrl builds property slug URLs", () => {
  assert.equal(
    transwesternDetailUrl("dallas-midtown-tower"),
    "https://transwestern.com/property/dallas-midtown-tower"
  );
  assert.equal(transwesternDetailUrl("-"), null);
  assert.equal(transwesternDetailUrl(null), null);
});

test("transwesternTransactionType maps bucket labels", () => {
  assert.equal(transwesternTransactionType("Sale"), "Sale");
  assert.equal(transwesternTransactionType("Lease"), "Lease");
  assert.equal(transwesternTransactionType("Sublease"), "Sublease");
  assert.equal(transwesternTransactionType("Sale or Lease"), "Sale/Lease");
});

test("transwesternSizeText formats square footage", () => {
  assert.equal(transwesternSizeText({ PropertySize: 12500 }), "12,500 SF");
  assert.equal(transwesternSizeText({ PropertySize: 0 }), null);
  assert.equal(transwesternSizeText({}), null);
});

test("transwesternPriceText formats sale price or lease fallback", () => {
  assert.equal(transwesternPriceText({ Price: 2500000 }, "sale"), "$2,500,000");
  assert.equal(transwesternPriceText({ Price: 0 }, "sale"), "Contact broker for pricing");
  assert.equal(transwesternPriceText({ Price: 0 }, "lease"), null);
});

test("parseTranswesternFacts extracts label/value pairs", () => {
  const html = `
    <ul class="property-facts">
      <li><b>Year Built:</b> 1987</li>
      <li><strong>Class:</strong> A</li>
    </ul>
  `;
  const $ = cheerio.load(html);
  assert.deepEqual(parseTranswesternFacts($), {
    "Year Built": "1987",
    Class: "A",
  });
});

test("parseTranswesternAvailability parses suite rows", () => {
  const html = `
    <table id="tblAvailability">
      <tr><th>Suite</th><th>Size</th><th>Rate</th><th>Type</th></tr>
      <tr><td>1200</td><td>4,500 SF</td><td>$32/SF</td><td>Office</td></tr>
      <tr><td>1400</td><td>2,100 SF</td><td>$28/SF</td><td>Office</td></tr>
    </table>
  `;
  const $ = cheerio.load(html);
  const rows = parseTranswesternAvailability($);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].suite, "1200");
  assert.equal(rows[0].size, "4,500 SF");
  assert.equal(rows[0].rate, "$32/SF");
  assert.equal(rows[0].type, "Office");
});
