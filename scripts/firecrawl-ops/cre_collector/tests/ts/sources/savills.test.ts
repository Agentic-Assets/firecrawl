import test from "node:test";
import assert from "node:assert/strict";
import {
  inferStateFromZip,
  parseSavillsUsLocation,
  savillsSqft,
  savillsImageUrls,
  savillsDocumentUrls,
} from "../../../sources/savills.js";

test("inferStateFromZip maps ZIP prefixes to states", () => {
  assert.equal(inferStateFromZip("75201"), "TX");
  assert.equal(inferStateFromZip("10001"), "NY");
  assert.equal(inferStateFromZip("90210"), "CA");
  assert.equal(inferStateFromZip("not-a-zip"), null);
  assert.equal(inferStateFromZip(null), null);
});

test("parseSavillsUsLocation parses city, state, and postal code", () => {
  assert.deepEqual(parseSavillsUsLocation("123 Main St, Dallas, TX 75201"), {
    city: "123 Main St",
    state: "TX",
    postalCode: "75201",
  });
  assert.deepEqual(parseSavillsUsLocation("Seattle, Washington 98101"), {
    city: "Seattle",
    state: "WA",
    postalCode: "98101",
  });
  assert.equal(parseSavillsUsLocation(""), null);
});

test("savillsSqft extracts square footage from text", () => {
  assert.equal(savillsSqft("Office (12,500 sq ft)"), 12500);
  assert.equal(savillsSqft("15,000 sqft warehouse"), 15000);
  assert.equal(savillsSqft("price on application"), null);
});

test("savillsImageUrls dedupes gallery image sizes", () => {
  const row = {
    ImagesGallery: [
      { ImageUrl_L: "https://cdn.savills.com/a-large.jpg", ImageUrl_S: "https://cdn.savills.com/a-small.jpg" },
    ],
    PropertyCardImagesGallery: [{ ImageUrl: "https://cdn.savills.com/b.jpg" }],
  };
  const urls = savillsImageUrls(row);
  assert.equal(urls.length, 3);
  assert.ok(urls.every((u) => u.startsWith("https://")));
});

test("savillsDocumentUrls classifies the floor-plan PDF distinctly from brochures", () => {
  const docs = savillsDocumentUrls({
    FloorplanPDFUrl: "https://cdn.savills.com/floorplan.pdf",
    BrochureGallery: [
      { Caption: "Marketing Brochure", ImageUrl: "https://cdn.savills.com/brochure.pdf" },
    ],
  });
  const floorplan = docs.find((d) => d.url.endsWith("floorplan.pdf"));
  const brochure = docs.find((d) => d.url.endsWith("brochure.pdf"));
  assert.equal(floorplan?.docType, "floor_plan");
  assert.equal(brochure?.docType, "brochure");
  // Non-PDF and missing urls are excluded.
  assert.equal(savillsDocumentUrls({ FloorplanPDFUrl: null, BrochureGallery: [] }).length, 0);
});
