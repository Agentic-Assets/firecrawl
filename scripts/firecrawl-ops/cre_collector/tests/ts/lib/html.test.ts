import test from "node:test";
import assert from "node:assert/strict";
import {
  decodeHtmlEntities,
  titleFromFilename,
  jsonLdObjects,
  firstJsonLd,
  stripHtmlText,
  extractSitemapUrlEntries,
  dedupeStrings,
} from "../../../lib/html.js";

test("decodeHtmlEntities decodes common entities", () => {
  assert.equal(decodeHtmlEntities("Tom &amp; Jerry"), "Tom & Jerry");
  assert.equal(decodeHtmlEntities("&quot;quoted&quot;"), '"quoted"');
  assert.equal(decodeHtmlEntities("&#39;ok&#39;"), "'ok'");
  assert.equal(decodeHtmlEntities("\\u0026amp;"), "&");
  assert.equal(decodeHtmlEntities("&lt;tag&gt;"), "<tag>");
});

test("titleFromFilename derives a readable title from URL path", () => {
  assert.equal(
    titleFromFilename("https://example.com/docs/annual-report-2024.pdf"),
    "annual report 2024"
  );
  assert.equal(titleFromFilename("not-a-valid-url"), "Document");
});

test("jsonLdObjects extracts objects from script tags", () => {
  const html = `
    <html><body>
      <script type="application/ld+json">
        {"@type":"Product","name":"Widget"}
      </script>
      <script type="application/ld+json">
        {"@graph":[{"@type":"Organization","name":"Acme"}]}
      </script>
      <script type="application/ld+json">not json</script>
    </body></html>
  `;
  const objects = jsonLdObjects(html);
  assert.equal(objects.length, 3);
  assert.equal(objects[0].name, "Widget");
  assert.equal(objects[2].name, "Acme");
});

test("firstJsonLd finds object by @type", () => {
  const html = `
    <script type="application/ld+json">
      {"@type":["Place","Product"],"sku":"A1"}
    </script>
    <script type="application/ld+json">
      {"@type":"Organization","name":"Broker"}
    </script>
  `;
  const product = firstJsonLd(html, "Product");
  const org = firstJsonLd(html, "organization");
  assert.equal(product?.sku, "A1");
  assert.equal(org?.name, "Broker");
  assert.equal(firstJsonLd(html, "Missing"), null);
});

test("stripHtmlText removes tags and normalizes whitespace", () => {
  assert.equal(stripHtmlText("<p>Hello <b>world</b></p>"), "Hello world");
  assert.equal(stripHtmlText(null), null);
  assert.equal(stripHtmlText(123), null);
});

test("extractSitemapUrlEntries parses url blocks", () => {
  const xml = `<?xml version="1.0"?>
    <urlset>
      <url>
        <loc>https://example.com/a</loc>
        <lastmod>2024-01-01</lastmod>
      </url>
      <url>
        <loc>https://example.com/b&amp;c</loc>
      </url>
      <url><loc>  </loc></url>
    </urlset>`;
  assert.deepEqual(extractSitemapUrlEntries(xml), [
    { loc: "https://example.com/a", lastmod: "2024-01-01" },
    { loc: "https://example.com/b&c", lastmod: null },
    { loc: "", lastmod: null },
  ]);
});

test("dedupeStrings keeps first cleaned unique values", () => {
  assert.deepEqual(
    dedupeStrings([" Alpha ", "Alpha", null, "", "Beta", " Beta ", undefined]),
    ["Alpha", "Beta"]
  );
});

test("dedupeStrings is case-sensitive", () => {
  assert.deepEqual(dedupeStrings(["alpha", "Alpha"]), ["alpha", "Alpha"]);
});
