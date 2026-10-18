process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import { extractAvisonLeaseFacts } from "../../../sources/pure/avison-lease-facts.js";
import { projectAvisonAskingPrice } from "../../../sources/pure/avison-price-visibility.js";

const __dirname = dirname(fileURLToPath(import.meta.url));
const fixture = readFileSync(
  join(__dirname, "../../fixtures/raw_data/avison-young-building-177.html"),
  "utf8"
);
const listingUrl = "https://www.avisonyoung.us/properties/delp-distribution-center";
const sourceUrl = "https://ayusdelpdistributioncenter.sharplaunch.com/";
const observedAt = "2026-10-18T12:00:00.000Z";

test("extracts literal suite and overview facts without filling a missing rent", () => {
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: fixture }],
  });

  assert.deepEqual(facts, {
    schema: "avison-lease-facts-v1",
    external_id: "177",
    listing_url: listingUrl,
    pages: [
      {
        source_url: sourceUrl,
        observed_at: observedAt,
        role: "availability",
        observations: [
          {
            suite_text: "Suite 200",
            status_text: "Available",
            rent_text: null,
            area_text: "28,350 SF",
            lease_type_text: null,
            fields: [
              { label: "Unit/Suite", value: "Unit Suite 200" },
              { label: "Total Size", value: "28,350 SF" },
            ],
            unparsed: [],
          },
        ],
      },
      {
        source_url: sourceUrl,
        observed_at: observedAt,
        role: "overview",
        observations: [
          {
            suite_text: null,
            status_text: null,
            rent_text: null,
            area_text: null,
            lease_type_text: null,
            fields: [],
            unparsed: [
              "28,350 sf available (2,200 sf office)",
              "20' clear height",
              "3 dock doors",
              "50' x 37' column spacing",
              "LED lighting in warehouse and office",
              "Recently renovated office interiors",
            ],
          },
        ],
      },
    ],
  });
});

test("keeps explicit rent text literal and does not synthesize a period or basis", () => {
  const html = `<div class="availability-card-v2"><div class="availability-card-v2-container">
    <div class="availability-card-status-tag">Available</div>
    <div class="availability-card-name"><h3>Suite A</h3></div>
    <div class="availability-card-rent"><h3>$19.50 - $22.00</h3></div>
    <div class="availability-card-info"><div class="availability-card-info-item">
      <span class="availability-card-info-item-title">Lease Type</span>
      <p class="availability-card-info-item-value">Modified Gross</p></div>
    </div></div>
    <div class="building__overview-text"><ul><li>Rental Rate: $2,000 per month</li></ul></div>
  </div>`;
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: html }],
  });

  assert.equal(facts?.pages[0]?.observations[0]?.rent_text, "$19.50 - $22.00");
  assert.equal(facts?.pages[0]?.observations[0]?.lease_type_text, "Modified Gross");
  assert.deepEqual(facts?.pages[0]?.observations[0]?.fields, [
    { label: "Lease Type", value: "Modified Gross" },
  ]);
  assert.equal(facts?.pages[1]?.observations[0]?.rent_text, "$2,000 per month");
  const projected = projectAvisonAskingPrice({
    sourceKey: "avison-young",
    rawSharpLaunch: { hide_price: 1 },
    extraFacts: { avison_lease_facts: facts },
  });
  assert.deepEqual(projected.extraFacts.avison_lease_facts, facts);
});
test("asking projection keeps explicit rent paired and does not orphan asking or unknown financial values", () => {
  const html = `<div class="availability-card-v2"><div class="availability-card-v2-container">
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Rent</span><span class="availability-card-info-item-value">$20</span></div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Sale Price</span><span class="availability-card-info-item-value">$20</span></div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Other Amount</span><span class="availability-card-info-item-value">$20</span></div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Sale Price and Rent</span><span class="availability-card-info-item-value">$20</span></div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Property Type</span><span class="availability-card-info-item-value">Industrial</span></div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Clear Height</span><span class="availability-card-info-item-value">20 feet</span></div>
  </div></div>
  <div class="building__overview-text"><p>The asking price is $20.</p><p>Clear height is 20 feet.</p></div>`;
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: html }],
  });
  const projected = projectAvisonAskingPrice({
    sourceKey: "avison-young",
    askingPrice: 20,
    rawSharpLaunch: { hide_price: 1 },
    extraFacts: { avison_lease_facts: facts },
  });
  const fields = projected.extraFacts.avison_lease_facts.pages[0].observations[0].fields;
  assert.deepEqual(fields, [
    { label: "Rent", value: "$20" },
    { label: "Property Type", value: "Industrial" },
    { label: "Clear Height", value: "20 feet" },
  ]);
  assert.deepEqual(
    projected.extraFacts.avison_lease_facts.pages[1].observations[0].unparsed,
    ["Clear height is 20 feet."],
  );
});


test("lease namespace keeps existing generic protection for unrecognized financial keys", () => {
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: fixture }],
  })!;
  const projected = projectAvisonAskingPrice({
    sourceKey: "avison-young",
    askingPrice: 30,
    rawSharpLaunch: { hide_price: 1 },
    extraFacts: {
      avison_lease_facts: {
        ...facts,
        salePriceUsd: 30,
        askingAmount: 30,
        pricing: { businessFunction: "Sell", amount: 30 },
      },
    },
  });
  const safe = projected.extraFacts.avison_lease_facts;
  assert.equal("salePriceUsd" in safe, false);
  assert.equal("askingAmount" in safe, false);
  assert.deepEqual(safe.pricing, { businessFunction: "Sell" });
});


test("ambiguous duplicate visible headings stay raw without choosing a rent or suite", () => {
  const html = `<div class="availability-card-v2"><div class="availability-card-v2-container">
    <div class="availability-card-name"><h3>Suite A</h3><h3>Suite B</h3></div>
    <div class="availability-card-status-tag">Available</div>
    <div class="availability-card-rent"><h3>$19.50</h3><h3>$22.00</h3></div>
    <div class="availability-card-info-item"><span class="availability-card-info-item-title">Rent</span>
      <span class="availability-card-info-item-value">$19.50</span><span class="availability-card-info-item-value">$22.00</span></div>
  </div></div>`;
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: html }],
  })!;
  const observation = facts.pages[0]!.observations[0]!;
  assert.equal(observation.suite_text, null);
  assert.equal(observation.rent_text, null);
  assert.deepEqual(observation.fields, []);
  assert.deepEqual(observation.unparsed, [
    "Rent", "$19.50", "$22.00", "Suite: Suite A", "Suite: Suite B", "Rent: $19.50", "Rent: $22.00",
  ]);
});


test("suite and area literals remain protected identity when equal to hidden asking", () => {
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: fixture }],
  })!;
  const page = facts.pages[0]!;
  const observation = page.observations[0]!;
  const withNumericIdentity = {
    ...facts,
    pages: [{
      ...page,
      observations: [{
        ...observation,
        suite_text: "20",
        area_text: "20",
        status_text: "Sale Price $20",
        lease_type_text: "Sale Price $20",
        rent_text: "$20",
      }],
    }],
  };
  const projected = projectAvisonAskingPrice({
    sourceKey: "avison-young",
    askingPrice: 20,
    rawSharpLaunch: { hide_price: 1 },
    extraFacts: { avison_lease_facts: withNumericIdentity },
  });
  const value = projected.extraFacts.avison_lease_facts.pages[0].observations[0];
  assert.equal(value.suite_text, "20");
  assert.equal(value.area_text, "20");
  assert.equal(value.status_text, null);
  assert.equal(value.lease_type_text, null);
  assert.equal(value.rent_text, "$20");
});


test("malformed lease value types survive asking projection for strict consumer refusal", () => {
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: fixture }],
  })!;
  const page = facts.pages[0]!;
  const observation = page.observations[0]!;
  const malformed = {
    ...facts,
    pages: [{
      ...page,
      observations: [{
        ...observation,
        fields: [{ label: "Other Amount", value: 20 }],
      }],
    }],
  };
  const projected = projectAvisonAskingPrice({
    sourceKey: "avison-young",
    askingPrice: 20,
    rawSharpLaunch: { hide_price: 1 },
    extraFacts: { avison_lease_facts: malformed },
  });
  assert.deepEqual(
    projected.extraFacts.avison_lease_facts.pages[0].observations[0].fields,
    [{ label: "Other Amount", value: 20 }],
  );
});


test("duplicate overview rental labels remain separate raw facts without a chosen rent", () => {
  const html = `<div class="building__overview-text"><ul>
    <li>Rental Rate: $2.50/SF/Month</li>
    <li>Rental Rate: $3.00/SF/Month</li>
  </ul></div>`;
  const facts = extractAvisonLeaseFacts({
    externalId: "177",
    listingUrl,
    observedAt,
    pages: [{ sourceUrl, rawHtml: html }],
  })!;
  const observation = facts.pages[0]!.observations[0]!;
  assert.equal(observation.rent_text, null);
  assert.deepEqual(observation.fields, [
    { label: "Rental Rate", value: "$2.50/SF/Month" },
    { label: "Rental Rate", value: "$3.00/SF/Month" },
  ]);
});
