import assert from "node:assert/strict";
import test from "node:test";
import { franklinStreetBuildoutConfig } from "../../../sources/franklin-street.js";

test("franklinStreetBuildoutConfig selects the sale Buildout token", () => {
  const cfg = franklinStreetBuildoutConfig("sale");
  assert.equal(cfg.company, "Franklin Street");
  assert.equal(cfg.pluginKey, "a234450b432b2b2bebc1ace7e6f692e4489bde70");
  assert.equal(cfg.listingsPage, "https://www.franklinst.com/properties/");
  assert.equal(cfg.opts.cacheSlug, "franklin-street-sale");
  assert.equal(cfg.opts.preferDirectJson, true);
  assert.equal(cfg.opts.requireCompletePages, true);
});

test("franklinStreetBuildoutConfig selects the lease Buildout token", () => {
  const cfg = franklinStreetBuildoutConfig("lease");
  assert.equal(cfg.pluginKey, "2f82fcd26667c4b0126d0084938ffa265f05fa4a");
  assert.equal(cfg.opts.cacheSlug, "franklin-street-lease");
  assert.equal(cfg.opts.directReferer, "https://www.franklinst.com/properties/");
});
