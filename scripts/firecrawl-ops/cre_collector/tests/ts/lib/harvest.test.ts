// Pure unit tests for lib/harvest.ts harvestDetail(). No network, no argv side
// effects (harvest.ts deliberately does not import lib/config.ts). node:test
// style, matching tests/ts/lib/enrich.test.ts.

import test from "node:test";
import assert from "node:assert/strict";
import { harvestDetail, type HarvestCtx } from "../../../lib/harvest.js";
import type { AttrBlock, DocItem, LinkItem, MediaItem, ScrapedDoc } from "../../../types.js";

// Minimal ScrapedDoc builder; every field optional so a test fills only what it
// exercises (mirrors the enrich.test.ts detailDoc helper).
function doc(partial: Partial<ScrapedDoc>): ScrapedDoc {
  return { rawHtml: "", markdown: "", links: [], ...partial };
}

// ---------------------------------------------------------------------------
// (1) Lee Associates Vimeo case: a Buildout-style detail page where the video
// rides a div[component=video][url] attribute, an offering-memorandum pdf is in
// the links, and the gallery carries real CDN photos plus map-tile/svg noise.
// One vimeo MediaItem (normalized player.vimeo embed), one OM DocItem, exactly
// the 5 CDN photos kept.
// ---------------------------------------------------------------------------

test("Lee Associates Vimeo: div[component=video][url] -> one vimeo media w/ normalized embed; OM pdf -> doc; 5 photos kept, noise dropped", () => {
  const attributes: AttrBlock[] = [
    { selector: "div[component=video]", attribute: "url", values: ["https://vimeo.com/824804225"] },
    { selector: "iframe", attribute: "src", values: [] },
    {
      selector: "a",
      attribute: "href",
      values: [
        "https://cdn.buildout.com/docs/123/offering-memorandum.pdf",
        "https://www.leeassociates.com/properties/some-listing",
      ],
    },
    { selector: "video source", attribute: "src", values: [] },
    { selector: "[data-video-url]", attribute: "data-video-url", values: [] },
  ];
  const images = [
    "https://images.buildout.com/p/1.jpg",
    "https://images.buildout.com/p/2.jpg",
    "https://images.buildout.com/p/3.jpg",
    "https://images.buildout.com/p/4.jpg",
    "https://images.buildout.com/p/5.jpg",
    "https://maps.googleapis.com/maps/api/staticmap?center=x", // noise: map tile
    "https://cdn.example.com/icon.svg", // noise: svg
    "data:image/png;base64,AAAA", // noise: data uri
  ];
  const out = harvestDetail(doc({ attributes, images }));

  assert.equal(out.media.length, 1, "exactly one media item");
  const m = out.media[0]!;
  assert.equal(m.mediaType, "video");
  assert.equal(m.provider, "vimeo");
  assert.equal(m.url, "https://vimeo.com/824804225");
  // div[component=video] is an embed-style selector -> embedUrl normalized to the
  // canonical player.vimeo form.
  assert.equal(m.embedUrl, "https://player.vimeo.com/video/824804225");

  assert.equal(out.documents.length, 1, "exactly one document");
  assert.equal(out.documents[0]!.docType, "om");
  assert.equal(out.documents[0]!.url, "https://cdn.buildout.com/docs/123/offering-memorandum.pdf");

  // The non-doc, non-media leeassociates listing anchor lands as a link.
  assert.equal(out.links.length, 1);
  assert.equal(out.links[0]!.url, "https://www.leeassociates.com/properties/some-listing");

  // All 5 real CDN photos kept; the 3 noise urls dropped; never truncated.
  assert.equal(out.images.length, 5);
  assert.ok(out.images.every((u) => u.includes("images.buildout.com")));
});

// ---------------------------------------------------------------------------
// (1b) Same video, two urls: the real Lee/Buildout fixture exposes the SAME
// vimeo video as both the vimeo.com/<id>/<hash> share anchor (in links) AND the
// player.vimeo.com/video/<id>?h=<hash> iframe src (in rawHtml, no attributes
// format). They must fold to ONE MediaItem: landing url kept, privacy hash
// preserved in the embed url (unlisted vimeo videos will not play without it).
// ---------------------------------------------------------------------------

test("same vimeo video via share anchor + player iframe folds to one item; privacy hash preserved", () => {
  const out = harvestDetail(
    doc({
      links: ["https://vimeo.com/911365406/78b4c4fc54?share=copy"],
      // No `attributes` -> the rawHtml media regex fallback runs and finds the
      // player iframe; without identity-dedup this produced TWO vimeo items.
      rawHtml: '<iframe src="https://player.vimeo.com/video/911365406?h=78b4c4fc54"></iframe>',
    })
  );
  assert.equal(out.media.length, 1, "two urls of one video -> exactly one MediaItem");
  const m = out.media[0]!;
  assert.equal(m.provider, "vimeo");
  assert.equal(m.mediaType, "video");
  // Landing (share) url preferred over the player/embed form for the human url.
  assert.equal(m.url, "https://vimeo.com/911365406/78b4c4fc54?share=copy");
  // Privacy hash carried into the canonical embed url.
  assert.equal(m.embedUrl, "https://player.vimeo.com/video/911365406?h=78b4c4fc54");
});

// ---------------------------------------------------------------------------
// (1c) Same fold via the structured attributes path (production case): the
// div[component=video][url] share url and the iframe[src] player url for one
// video collapse to one item with the hash preserved.
// ---------------------------------------------------------------------------

test("attributes path: div[component=video] share + iframe player for one video fold to one item w/ hash", () => {
  const attributes: AttrBlock[] = [
    {
      selector: "div[component=video]",
      attribute: "url",
      values: ["https://vimeo.com/911365406/78b4c4fc54?share=copy"],
    },
    {
      selector: "iframe",
      attribute: "src",
      values: ["https://player.vimeo.com/video/911365406?h=78b4c4fc54"],
    },
  ];
  const out = harvestDetail(doc({ attributes }));
  assert.equal(out.media.length, 1);
  assert.equal(out.media[0]!.url, "https://vimeo.com/911365406/78b4c4fc54?share=copy");
  assert.equal(out.media[0]!.embedUrl, "https://player.vimeo.com/video/911365406?h=78b4c4fc54");
});

// ---------------------------------------------------------------------------
// (1d) YouTube watch + embed forms of one video also fold to a single item.
// ---------------------------------------------------------------------------

// ---------------------------------------------------------------------------
// (1e) Buildout hosted-download links (the real Lee OM form: /sharing/...?file=
// with no extension/keyword) classify as a document, not an external link.
// ---------------------------------------------------------------------------

test("buildout /sharing/?file= link classifies as a document, not a link", () => {
  const out = harvestDetail(
    doc({
      links: [
        "https://buildout.com/sharing/1328826-sale?file=3211212",
        "https://buildout.com/plugins/abc/host/inventory/1328826-sale?iframe=true", // listing embed, NOT a doc
      ],
    })
  );
  const docUrls = out.documents.map((d) => d.url);
  assert.ok(
    docUrls.includes("https://buildout.com/sharing/1328826-sale?file=3211212"),
    "buildout sharing/file link is a document"
  );
  // The /plugins/ listing-embed url is not a document.
  assert.ok(!docUrls.some((u) => u.includes("/plugins/")), "listing embed is not a doc");
});

test("youtube watch + embed urls of one video fold to one item", () => {
  const out = harvestDetail(
    doc({
      links: ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"],
      rawHtml: '<iframe src="https://www.youtube.com/embed/dQw4w9WgXcQ"></iframe>',
    })
  );
  assert.equal(out.media.length, 1);
  const m = out.media[0]!;
  assert.equal(m.provider, "youtube");
  // Watch (landing) url preferred over the /embed/ form.
  assert.equal(m.url, "https://www.youtube.com/watch?v=dQw4w9WgXcQ");
  assert.equal(m.embedUrl, "https://www.youtube.com/embed/dQw4w9WgXcQ");
});

// ---------------------------------------------------------------------------
// (2) Parent-shell negative: a bare listing-index/shell page with no media,
// no documents, only same-host nav links and gallery photos -> empty media.
// ---------------------------------------------------------------------------

test("parent shell page yields empty media (no false positives)", () => {
  const attributes: AttrBlock[] = [
    {
      selector: "a",
      attribute: "href",
      values: [
        "https://www.example-brokerage.com/about",
        "https://www.example-brokerage.com/contact",
      ],
    },
  ];
  const out = harvestDetail(doc({ attributes, images: ["https://cdn.x.com/hero.jpg"] }));
  assert.equal(out.media.length, 0);
  assert.equal(out.documents.length, 0);
  // Same-host nav links still classify as 'other' links (not dropped); the
  // invariant under test is specifically that NO media was fabricated.
  assert.ok(out.media.length === 0);
  assert.equal(out.images.length, 1);
});

// ---------------------------------------------------------------------------
// (3) Provider matrix: youtube (watch/youtu.be/embed), wistia, brightcove,
// matterport, kuula, og:video meta (via rawHtml regex fallback), JSON-LD
// VideoObject contentUrl (via ctx.extraMedia, the promotion path).
// ---------------------------------------------------------------------------

test("provider matrix: youtube watch/youtu.be/embed all -> youtube video, embed normalized", () => {
  const watch = harvestDetail(doc({ links: ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"] }));
  assert.equal(watch.media[0]!.provider, "youtube");
  assert.equal(watch.media[0]!.embedUrl, "https://www.youtube.com/embed/dQw4w9WgXcQ");

  const short = harvestDetail(doc({ links: ["https://youtu.be/dQw4w9WgXcQ"] }));
  assert.equal(short.media[0]!.provider, "youtube");
  assert.equal(short.media[0]!.embedUrl, "https://www.youtube.com/embed/dQw4w9WgXcQ");

  const embed = harvestDetail(
    doc({
      attributes: [
        { selector: "iframe", attribute: "src", values: ["https://www.youtube.com/embed/dQw4w9WgXcQ"] },
      ],
    })
  );
  assert.equal(embed.media[0]!.provider, "youtube");
  // iframe selector is embed-style -> embedUrl set to the (already-embed) src.
  assert.equal(embed.media[0]!.embedUrl, "https://www.youtube.com/embed/dQw4w9WgXcQ");
});

test("provider matrix: wistia / brightcove / matterport / kuula", () => {
  const cases: Array<[string, string, MediaItem["mediaType"]]> = [
    ["https://fast.wistia.net/embed/iframe/abc123", "wistia", "video"],
    ["https://players.brightcove.net/123/default_default/index.html?videoId=456", "brightcove", "video"],
    ["https://my.matterport.com/show/?m=abcdef", "matterport", "matterport"],
    ["https://kuula.co/share/collection/7abc", "kuula.co", "virtual_tour"],
  ];
  for (const [url, provider, mediaType] of cases) {
    const out = harvestDetail(doc({ links: [url] }));
    assert.equal(out.media.length, 1, `one media for ${url}`);
    assert.equal(out.media[0]!.provider, provider, `provider for ${url}`);
    assert.equal(out.media[0]!.mediaType, mediaType, `mediaType for ${url}`);
  }
});

test("rawHtml regex fallback extracts iframe media when the attributes format is absent", () => {
  // No doc.attributes (fork omitted the format) -> regex over rawHtml fires.
  const rawHtml = `
    <html><body>
      <iframe src="https://player.vimeo.com/video/99887766" allowfullscreen></iframe>
      <video><source src="https://cdn.x.com/tour.mp4"></video>
      <div data-video-url="https://www.youtube.com/watch?v=ABCDEFGabcd"></div>
    </body></html>`;
  const out = harvestDetail(doc({ rawHtml }));
  const vimeo = out.media.find((m) => m.provider === "vimeo");
  const youtube = out.media.find((m) => m.provider === "youtube");
  assert.ok(vimeo, "vimeo iframe extracted via regex fallback");
  assert.equal(vimeo!.embedUrl, "https://player.vimeo.com/video/99887766");
  assert.ok(youtube, "data-video-url youtube extracted via regex fallback");
  assert.equal(youtube!.embedUrl, "https://www.youtube.com/embed/ABCDEFGabcd");
});

test("regex media fallback does NOT fire when attributes ARE present (no double count)", () => {
  // attributes present (even if empty) -> trust the structured path; the rawHtml
  // iframe is ignored so we never double-count a url the format already saw.
  const rawHtml = `<iframe src="https://player.vimeo.com/video/55"></iframe>`;
  const out = harvestDetail(doc({ rawHtml, attributes: [] }));
  assert.equal(out.media.length, 0);
});

// ---------------------------------------------------------------------------
// (4) Document classification: each keyword bucket + bare-extension 'other'.
// ---------------------------------------------------------------------------

test("document classification covers every docType bucket", () => {
  const links = [
    "https://x.com/offering-memorandum.pdf", // om
    "https://x.com/property-flyer.pdf", // flyer
    "https://x.com/marketing-brochure.pdf", // brochure
    "https://x.com/floor-plan.pdf", // floor_plan
    "https://x.com/2024-financials.pdf", // financials
    "https://x.com/rent-roll.xlsx", // rent_roll
    "https://x.com/random-doc.pdf", // other (bare extension, no keyword)
  ];
  const out = harvestDetail(doc({ links }));
  const byType = new Map(out.documents.map((d) => [d.docType, d.url]));
  assert.equal(byType.get("om"), "https://x.com/offering-memorandum.pdf");
  assert.equal(byType.get("flyer"), "https://x.com/property-flyer.pdf");
  assert.equal(byType.get("brochure"), "https://x.com/marketing-brochure.pdf");
  assert.equal(byType.get("floor_plan"), "https://x.com/floor-plan.pdf");
  assert.equal(byType.get("financials"), "https://x.com/2024-financials.pdf");
  assert.equal(byType.get("rent_roll"), "https://x.com/rent-roll.xlsx");
  assert.equal(byType.get("other"), "https://x.com/random-doc.pdf");
  assert.equal(out.documents.length, 7);
  // No document leaked into links.
  assert.equal(out.links.length, 0);
});

test("a keyword-classified doc qualifies even without a file extension (dataroom link)", () => {
  const out = harvestDetail(doc({ links: ["https://deals.example.com/dataroom/abc"] }));
  assert.equal(out.documents.length, 1);
  assert.equal(out.documents[0]!.docType, "om");
});

// ---------------------------------------------------------------------------
// (5) Link classification: map / social / external_listing / other; broker-bio
// DROPPED (lives in contacts.profile_url, never emitted to links).
// ---------------------------------------------------------------------------

test("link classification: map / social / external_listing / other; broker-bio dropped", () => {
  const links = [
    "https://www.google.com/maps/place/Dallas", // map
    "https://www.facebook.com/somebrokerage", // social
    "https://www.loopnet.com/Listing/123/", // external_listing
    "https://www.example.com/some-resource", // other
    "https://www.example-brokerage.com/agents/jane-doe", // broker_bio -> DROPPED
  ];
  const out = harvestDetail(doc({ links }));
  const byType = new Map(out.links.map((l) => [l.linkType, l.url]));
  assert.equal(byType.get("map"), "https://www.google.com/maps/place/Dallas");
  assert.equal(byType.get("social"), "https://www.facebook.com/somebrokerage");
  assert.equal(byType.get("external_listing"), "https://www.loopnet.com/Listing/123/");
  assert.equal(byType.get("other"), "https://www.example.com/some-resource");
  // broker-bio is detected only to exclude it: 4 links, none of them the bio.
  assert.equal(out.links.length, 4);
  assert.ok(!out.links.some((l) => l.url.includes("/agents/")), "broker bio never emitted");
});

// ---------------------------------------------------------------------------
// (6) ctx.extra* passthrough: typed items + bare strings; promoted video string
// re-routes into media; promoted doc/link normalize with default types.
// ---------------------------------------------------------------------------

test("ctx.extra* passes through typed items and normalizes bare strings", () => {
  const ctx: HarvestCtx = {
    extraMedia: [
      "https://vimeo.com/111222", // bare string -> classified vimeo (re-routed)
      { mediaType: "matterport", provider: "matterport", url: "https://my.matterport.com/show/?m=zzz", embedUrl: null, title: "3D Tour" } as MediaItem,
      "https://cdn.x.com/clip.mp4", // bare non-provider string -> mediaType 'other'
    ],
    extraDocs: [
      "https://x.com/promoted.pdf", // bare string -> docType 'other'
      { url: "https://x.com/typed-om.pdf", title: "OM", docType: "om" } as DocItem,
    ],
    extraLinks: [
      "https://www.crexi.com/properties/9", // bare string -> re-routed -> external_listing
      { url: "https://x.com/raw-link", rel: "nofollow", linkType: "external_listing" } as LinkItem,
    ],
    extraImages: ["https://cdn.x.com/extra1.jpg", "data:image/png;base64,Z"], // 2nd is noise
  };
  const out = harvestDetail(doc({}), ctx);

  // extraMedia: bare vimeo string classified, typed matterport kept, bare mp4 -> 'other'.
  const vimeo = out.media.find((m) => m.provider === "vimeo");
  assert.ok(vimeo, "bare vimeo string promoted to a classified media item");
  assert.equal(vimeo!.embedUrl, "https://player.vimeo.com/video/111222");
  assert.ok(out.media.find((m) => m.mediaType === "matterport"));
  assert.ok(out.media.find((m) => m.mediaType === "other" && m.url.endsWith("clip.mp4")));

  // extraDocs: bare -> other; typed -> om honored.
  assert.ok(out.documents.find((d) => d.url.endsWith("promoted.pdf") && d.docType === "other"));
  assert.ok(out.documents.find((d) => d.url.endsWith("typed-om.pdf") && d.docType === "om"));

  // extraLinks: bare crexi string re-routed to external_listing; typed link kept.
  assert.ok(out.links.find((l) => l.url.includes("crexi.com") && l.linkType === "external_listing"));
  assert.ok(out.links.find((l) => l.url.endsWith("raw-link") && l.rel === "nofollow"));

  // extraImages: the real one kept, the data: noise dropped.
  assert.equal(out.images.filter((u) => u.includes("extra1.jpg")).length, 1);
  assert.ok(!out.images.some((u) => u.startsWith("data:")));
});

test("ctx.baseUrl resolves relative extra urls; un-resolvable relatives drop", () => {
  const withBase = harvestDetail(doc({}), {
    baseUrl: "https://site.example.com/listings/1",
    extraDocs: ["/docs/brochure.pdf"],
  });
  assert.equal(withBase.documents.length, 1);
  assert.equal(withBase.documents[0]!.url, "https://site.example.com/docs/brochure.pdf");

  const noBase = harvestDetail(doc({}), { extraDocs: ["/docs/brochure.pdf"] });
  assert.equal(noBase.documents.length, 0, "relative url with no base is dropped, not thrown");
});

// ---------------------------------------------------------------------------
// (7) Dedup + empty-input safety: never throws; dedups by url across sources.
// ---------------------------------------------------------------------------

test("dedups every output array by url across attributes / links / images / extra*", () => {
  const sameVideo = "https://vimeo.com/424242";
  const sameDoc = "https://x.com/offering.pdf";
  const samePhoto = "https://cdn.x.com/a.jpg";
  const out = harvestDetail(
    doc({
      attributes: [
        { selector: "div[component=video]", attribute: "url", values: [sameVideo] },
        { selector: "a", attribute: "href", values: [sameDoc, "https://www.loopnet.com/x/"] },
      ],
      // trailing-slash variant of the same loopnet url must dedup to one link.
      links: [sameVideo, sameDoc, "https://www.loopnet.com/x"],
      images: [samePhoto, samePhoto, samePhoto + "/"],
    }),
    { extraMedia: [sameVideo], extraDocs: [sameDoc], extraImages: [samePhoto] }
  );
  assert.equal(out.media.length, 1, "video deduped to one across attributes+links+extra");
  assert.equal(out.documents.length, 1, "doc deduped to one");
  assert.equal(out.links.length, 1, "loopnet link deduped across slash variant");
  assert.equal(out.images.length, 1, "photo deduped across slash + repeats");
});

test("harvestDetail never throws on empty / garbage / malformed input", () => {
  assert.doesNotThrow(() => harvestDetail({} as ScrapedDoc));
  assert.doesNotThrow(() => harvestDetail(null as any));
  assert.doesNotThrow(() => harvestDetail(undefined as any));
  assert.doesNotThrow(() =>
    harvestDetail({ rawHtml: 5, markdown: null, links: "nope", images: 7, attributes: "bad" } as any)
  );
  assert.doesNotThrow(() =>
    harvestDetail(doc({ attributes: [null as any, { selector: 1, values: null } as any] }))
  );
  assert.doesNotThrow(() =>
    harvestDetail(doc({}), {
      extraMedia: [null as any, 5 as any, ""],
      extraLinks: [{} as any, "not-a-url"],
      extraDocs: [{ url: 7 } as any],
      extraImages: [null as any, ""],
    })
  );

  const empty = harvestDetail({} as ScrapedDoc);
  assert.deepEqual(empty, { media: [], links: [], documents: [], images: [] });
});

test("garbage urls (relative, fragment, javascript:, mailto:) are filtered without base", () => {
  const out = harvestDetail(
    doc({
      links: [
        "#section", // fragment
        "javascript:void(0)", // js scheme
        "mailto:agent@x.com", // mailto
        "/relative/path", // relative, no base -> dropped
        "tel:+15555555555", // tel
        "https://www.example.com/keeper", // the only survivor
      ],
    })
  );
  assert.equal(out.links.length, 1);
  assert.equal(out.links[0]!.url, "https://www.example.com/keeper");
});
