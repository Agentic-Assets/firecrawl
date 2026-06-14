// Isolate argv before cbre.ts loads config (strict parseArgs).
process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import {
  cbreAspect,
  cbreListingSlug,
  cbreListingUrl,
  cbreBrochureUrl,
  cbrePhotoUrl,
  cbreTransactionType,
} from "../../../sources/cbre.js";

test("cbreAspect maps transaction to API aspect", () => {
  assert.equal(cbreAspect("sale"), "isSale");
  assert.equal(cbreAspect("lease"), "isLetting");
});

test("cbreListingSlug lowercases and hyphenates address parts", () => {
  assert.equal(
    cbreListingSlug({
      name: "Midtown Tower",
      street: "123 Main St",
      city: "Dallas",
      state: "TX",
      zip: "75201",
    }),
    "midtown-tower-123-main-st-dallas-tx-75201"
  );
  assert.equal(
    cbreListingSlug({ name: "Retail Pad", street: null, city: "Austin", state: "TX", zip: null }),
    "retail-pad-austin-tx"
  );
  assert.equal(cbreListingSlug({ name: null, street: null, city: null, state: null, zip: null }), "");
});

test("cbreListingUrl builds CBRE detail path", () => {
  assert.equal(
    cbreListingUrl("US-SMPL-160329", "midtown-tower-dallas-tx"),
    "https://www.cbre.com/properties/properties-for-lease/commercial-space/details/US-SMPL-160329/midtown-tower-dallas-tx"
  );
});

test("cbreBrochureUrl resolves absolute and relative URIs", () => {
  assert.equal(cbreBrochureUrl("https://cdn.cbre.com/brochure.pdf"), "https://cdn.cbre.com/brochure.pdf");
  assert.equal(cbreBrochureUrl("/resources/fileassets/US-SMPL/brochure.pdf"), "https://www.cbre.com/resources/fileassets/US-SMPL/brochure.pdf");
  assert.equal(cbreBrochureUrl(null), "https://www.cbre.com");
});

test("cbrePhotoUrl resolves absolute and relative resource URIs", () => {
  assert.equal(cbrePhotoUrl("https://images.cbre.com/photo.jpg"), "https://images.cbre.com/photo.jpg");
  assert.equal(cbrePhotoUrl("/resources/photos/abc.jpg"), "https://www.cbre.com/resources/photos/abc.jpg");
  assert.equal(cbrePhotoUrl(null), null);
  assert.equal(cbrePhotoUrl(""), null);
});

test("cbreTransactionType maps aspect flags", () => {
  assert.equal(cbreTransactionType(["isSale"]), "Sale");
  assert.equal(cbreTransactionType(["isLetting"]), "Lease");
  assert.equal(cbreTransactionType(["isSale", "isLetting"]), "Sale/Lease");
  assert.equal(cbreTransactionType([]), "Sale");
});
