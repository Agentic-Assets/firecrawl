import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { avisonYoungBaseListing, enrichAvisonYoungListing, extractAvisonYoungPhotos } from "../../../sources/avison-young.js";

const dir = new URL("../../fixtures/raw_data/", import.meta.url);
const evidence = JSON.parse(readFileSync(new URL("avison-young-named-association-17440.json", dir), "utf8"));
const html = readFileSync(new URL("avison-young-named-association-17440.html", dir), "utf8");
const roles = JSON.parse(readFileSync(new URL("avison-young-property-photos-17440.json", dir), "utf8"));
const context = { matchingExternalUrlRowIds: ["17440"] };
const doc = (body: string, images: string[] = []) => ({ rawHtml: body, markdown: "Retained listing facts", links: [], images,
  metadata: { sourceURL: evidence.response_url, url: evidence.response_url, statusCode: 200 } });
const noise = [...roles.broker_avatars, roles.tracking_url,
  "https://cdn.sharplaunch.com/website-15/ay_logo.png",
  "https://maps.googleapis.com/maps/api/staticmap?center=Clayton",
  "https://cdn.sharplaunch.com/media/123.jpg"];
// Retained official-0131.body (52767): this filename is a real gallery image,
// not sufficient evidence by itself outside the publisher's gallery role.
const headerNamedGallery = roles.gallery_role_counterexample.url;
const linkedGallery = roles.gallery_role_counterexample.linked_url;

async function enrich(body: string, images: string[] = [], photos?: string[]) {
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  try {
    const base = avisonYoungBaseListing({ ...evidence.feed_row, image_path: roles.feed_image_path }, new Map());
    return await enrichAvisonYoungListing({ ...base, sharpLaunchUrl: undefined,
      ...(photos === undefined ? {} : { photos }) }, false, async () => doc(body, images), context);
  } finally {
    if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
    else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous;
  }
}

test("retained gallery and feed photos win over generic avatar/tracking harvest", async () => {
  const body = html.replace("</body>", [...roles.property_urls.slice(0, 18), ...roles.broker_avatars, roles.tracking_url]
    .map(url => `<img src="${url}">`).join("") + "</body>");
  const result = await enrich(body);
  assert.equal(result.detailError, undefined);
  assert.deepEqual(new Set(result.photos), new Set(roles.property_urls));
  assert.equal(result.photos.length, 19);
  assert.equal(result.photoCount, 19);
  assert.equal(result.detailScrape.photoCount, 19);
  assert.equal(result.street, evidence.feed_row.address);
  assert.equal(result.rawSharpLaunch.image_path, roles.feed_image_path);
  assert.ok(result.documents.some((item: any) => item.url.endsWith("The_Shoppes_at_Glen_Laurel_Retail_Brochure.pdf")));
});

test("doc.images-only property candidates use the same predicate and URL resolution", async () => {
  const only = "https://cdn.sharplaunch.com/v2/client-10/retained/detail-only.webp";
  const relative = "https://cdn.sharplaunch.com/v2/client-10/retained/relative-only.jpg";
  const images = [only, only, "//cdn.sharplaunch.com/v2/client-10/retained/relative-only.jpg", ...noise];
  assert.deepEqual(extractAvisonYoungPhotos([{ doc: doc("<html></html>", [only, "relative-only.jpg", ...noise]),
    url: "https://cdn.sharplaunch.com/v2/client-10/retained/page" }], []), [only, relative]);
  const result = await enrich(html, images);
  assert.deepEqual(new Set(result.photos), new Set([only, relative, roles.property_urls.at(-1)]));
  assert.equal(result.photoCount, 3);
  assert.equal(result.detailScrape.photoCount, 3);
});

test("unqualified fallback cannot bypass the source predicate when no property photo exists", async () => {
  const result = await enrich(html, noise, ["https://images.example.test/unqualified.jpg"]);
  assert.equal(result.photos, undefined);
  assert.equal(result.photoCount, 0);
  assert.equal(result.detailScrape.photoCount, 0);
});

for (const url of noise) test(`non-property image stays out of photo helper: ${new URL(url).hostname}${new URL(url).pathname}`, () => {
  assert.deepEqual(extractAvisonYoungPhotos([{ doc: doc(`<img src="${url}">`, [url]), url: evidence.response_url }], [url]), []);
});

test("retained header-named property asset requires the document gallery role", async () => {
  const gallery = `<section id="gallery"><div class="gallery__carousel-images"><a href="${linkedGallery}" data-fancybox="gallery-carousel"><img src="${headerNamedGallery}" alt="Gallery Image 1"></a></div></section>`;
  const result = await enrich(html.replace("</body>", gallery + "</body>"));
  assert.ok(result.photos.includes(headerNamedGallery));
  assert.ok(result.photos.includes(linkedGallery));
  assert.equal(result.photoCount, result.photos.length);
  assert.deepEqual(extractAvisonYoungPhotos([{ doc: doc(`<img src="${headerNamedGallery}">`, [headerNamedGallery, linkedGallery]), url: evidence.response_url }], [headerNamedGallery, linkedGallery]), []);
});

test("gallery markup cannot bypass CDN, logo, media or avatar exclusions", () => {
  const body = `<section id="gallery"><div class="gallery__carousel-images">${noise.map(url => `<a data-fancybox="gallery-carousel" href="${url}"><img src="${url}"></a>`).join("")}</div></section>`;
  assert.deepEqual(extractAvisonYoungPhotos([{ doc: doc(body), url: evidence.response_url }], []), []);
});
