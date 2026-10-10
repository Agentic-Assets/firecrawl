// Isolate argv before jll.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fetchJllSearchPage } from "../../../sources/jll.js";

// Public JLL client operation observed 2026-10-10. The endpoint now rejects
// reduced operations even when they request valid schema fields.
const provider = JSON.parse(readFileSync(new URL(
  "../../fixtures/jll-search-results-operation.json", import.meta.url,
), "utf8")) as { query: string };

const normalize = (query: string) => query.replace(/\s+/g, " ").trim();

function providerResponse(body: { query: string; operationName: string }): Response {
  if (body.operationName !== "SearchResults" || normalize(body.query) !== normalize(provider.query)) {
    return Response.json({ error: 'Operation "SearchResults" does not match the allowed query.' }, { status: 400 });
  }
  return Response.json({
    data: {
      properties: {
        count: 1,
        items: [{
          id: "123", title: "Example Office", pageUrl: "/listings/example-office",
          address: "100 Main Street", city: "Miami", state: "FL", postcode: "33131",
          propertyTypes: ["office"], tenureTypes: ["sale"], hidePrice: false,
          salePrice: { amount: 2500000, currency: "USD", unit: "total" },
          surfaceAreas: [{ value: 10000, unit: "feet" }], images: [],
        }],
      },
    },
  });
}

test("JLL stops after the provider rejects an unapproved operation", async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async () => {
    requests++;
    return providerResponse({
      operationName: "SearchResults",
      query: "query SearchResults($market: String!, $language: String!) { properties(market: $market, language: $language) { count items { id pageUrl } } }",
    });
  };
  try {
    await assert.rejects(fetchJllSearchPage("sale", "office", 1), {
      name: "JllGraphqlRequestError", message: "JLL GraphQL HTTP 400", retryable: false,
    });
    assert.equal(requests, 1);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("JLL sends the public allowed operation and preserves listing mapping", async () => {
  const originalFetch = globalThis.fetch;
  let requests = 0;
  globalThis.fetch = async (_input, init) => {
    requests++;
    const body = JSON.parse(String(init?.body));
    assert.deepEqual(body.variables, {
      market: "us", language: "en", propertyTypes: ["office"], tenureTypes: ["sale"],
      skip: 0, take: 50,
      orderBy: { field: "dateModified", direction: "desc", imagePriority: true },
    });
    return providerResponse(body);
  };
  try {
    const result = await fetchJllSearchPage("sale", "office", 1);
    assert.equal(requests, 1);
    assert.equal(result.total, 1);
    assert.equal(result.listings.length, 1);
    const listing = result.listings[0];
    assert.equal(listing.id, "123");
    assert.equal(listing.name, "Example Office");
    assert.equal(listing.url, "https://property.jll.com/listings/example-office");
    assert.equal(listing.salePriceUsd, 2500000);
    assert.equal(listing.buildingSizeSqft, 10000);
    assert.equal(listing.state, "FL");
    assert.equal(listing.transactionType, "Sale");
  } finally {
    globalThis.fetch = originalFetch;
  }
});
