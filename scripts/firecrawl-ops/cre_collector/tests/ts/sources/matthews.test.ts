import assert from "node:assert/strict";
import test from "node:test";
import { parseMatthewsDetail, matthewsTenureFromUrl } from "../../../sources/matthews.js";

test("matthewsTenureFromUrl classifies leasing slugs as lease", () => {
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/leasing-abc"), "lease");
  assert.equal(matthewsTenureFromUrl("https://www.matthews.com/properties/panera-bread"), "sale");
});

test("parseMatthewsDetail extracts core fields from server-rendered HTML", () => {
  const html = `
    <html>
      <head><meta property="og:image" content="https://cms.matthews.com/wp-content/uploads/photo.jpg"></head>
      <body>
        <h1 id="propertyTitle">Panera Bread</h1>
        <div id="propertyAddress">123 Main St, Tulsa, OK 74103</div>
        <div id="propertyPrice">$3,000,000</div>
        <div class="key-info-title">Cap Rate</div><div class="key-info-value">6.40%</div>
        <div class="key-info-title">Property Type</div><div class="key-info-value">Retail</div>
        <a id="agentName" href="/agents/jane">Jane Broker</a>
      </body>
    </html>`;
  const row = parseMatthewsDetail(html, "https://www.matthews.com/properties/panera-bread", "sale");
  assert.equal(row?.id, "panera-bread");
  assert.equal(row?.name, "Panera Bread");
  assert.equal(row?.transactionType, "Sale");
  assert.equal(row?.salePriceUsd, 3000000);
  assert.equal(row?.capRatePct, 6.4);
  assert.equal(row?.assetType, "Retail");
  assert.equal(row?.state, "OK");
});
