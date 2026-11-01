process.argv = [process.argv[0]!, process.argv[1]!];

import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { avisonYoungPricePerAcre, avisonYoungSaleType } from "../../../sources/pure/avison-listing-structure.js";
import { avisonYoungBaseListing } from "../../../sources/avison-young.js";
import { projectAvisonAskingPrice } from "../../../sources/pure/avison-price-visibility.js";

const land = {
  id: 20228, status: "active", transaction: ["sale"], type: ["land.residential"], sale_currency: "USD",
  sale_price: 1_125_540, sale_unit_price: 18_500, total_surface_unit: "sqft",
  description: "<p>60.84 +/- Acres available for sale in North Hall</p>",
};

test("Avison land unit price becomes price per acre only when it reproduces the acreage", () => {
  assert.equal(avisonYoungPricePerAcre(land), 18_500);
  const listing = avisonYoungBaseListing(land, new Map());
  assert.equal(listing.pricePerAcre, 18_500);
  assert.equal(listing.salePricePerSf, undefined);
  assert.equal(listing.saleUnitPrice, 18_500);

  assert.equal(avisonYoungPricePerAcre({ ...land, description: "<p>60.84 &plusmn; Acres</p>" }), 18_500);
  assert.equal(avisonYoungPricePerAcre({ ...land, total_surface: 60.84, total_surface_unit: "ac", description: null }), 18_500);
  assert.equal(avisonYoungPricePerAcre({ ...land, description: "<p>For Sale</p>" }), null);
  assert.equal(avisonYoungPricePerAcre({ ...land, type: ["office.medical"] }), null);
  assert.equal(avisonYoungPricePerAcre({ ...land, sale_unit_price: land.sale_price }), null);
  assert.equal(avisonYoungPricePerAcre({ ...land, sale_currency: "CAD" }), null);
});

test("Avison withheld price yields no price per acre", () => {
  assert.equal(avisonYoungPricePerAcre({ ...land, hide_price: 1 }), null);
  assert.equal(avisonYoungPricePerAcre({ ...land, hide_price: 0 }), 18_500);
  // A per-acre price from a visible pass is an asking amount once any pass withholds.
  const out = projectAvisonAskingPrice({
    sourceKey: "avison-young", rawSharpLaunch: { ...land, hide_price: 1 }, pricePerAcre: 18_500,
  });
  assert.equal(out.pricePerAcre, undefined);
});

test("Avison detail Sale type is captured verbatim", () => {
  const rawHtml = readFileSync(
    new URL("../../fixtures/raw_data/avison-young-official-display-role-50399.html", import.meta.url),
    "utf8"
  );
  assert.equal(avisonYoungSaleType([{ doc: { rawHtml } as any }]), "Investment");
  assert.equal(avisonYoungSaleType([{ doc: { rawHtml: "<ul><li><span>Zoning</span><span>RM 18</span></li></ul>" } as any }]), null);
});
