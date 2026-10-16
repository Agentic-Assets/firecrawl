import test from "node:test";
import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { fileURLToPath } from "node:url";
import {
  PYRAMID_LEASE_TERM_ID,
  PYRAMID_INVENTORY_URL,
  PYRAMID_PAGE_SIZE,
  PYRAMID_SALE_TERM_ID,
  assertPyramidSnapshot,
  fetchPyramidSnapshot,
  mapPyramidListing,
  parsePyramidDetail,
  pyramidFetch,
  pyramidInventoryUrl,
} from "../../../sources/pyramid-brokerage.js";
import { brokers, resetBrokerStateForTests } from "../../../lib/broker.js";

function row(id: number, saleType: number) {
  return {
    id,
    status: "publish",
    type: "pbc-listings",
    link: `https://www.pyramidbrokerage.com/listings/property-${id}/`,
    slug: `property-${id}`,
    modified_gmt: "2026-07-30T15:00:48",
    title: { rendered: `100 Main St-Albany-NY-X${id}` },
    content: { rendered: "<p>Inventory description</p>" },
    "sale-type": [saleType],
    "property-use": [250],
    city: [263],
    county: [264],
    state: [108],
    _embedded: {
      "wp:term": [
        [{ taxonomy: "property-use", name: "Industrial" }],
        [{ taxonomy: "city", name: "Albany" }],
        [{ taxonomy: "county", name: "Albany" }],
        [{ taxonomy: "state", name: "NY" }],
      ],
      "wp:featuredmedia": [{ source_url: `https://www.pyramidbrokerage.com/photo-${id}.jpg` }],
    },
  };
}

const detailHtml = `
  <html>
    <head>
      <link rel="shortlink" href="https://www.pyramidbrokerage.com/?p=101">
      <meta property="og:image" content="https://www.pyramidbrokerage.com/listing.jpg">
    </head>
    <body><main id="content">
      <section class="property-details"><ul>
        <li><span class="name">Property Name:</span><span class="value">Main Warehouse</span></li>
        <li><span class="name">Sale/Lease:</span><span class="value">Sale</span></li>
        <li><span class="name">Major Use:</span><span class="value">Industrial</span></li>
        <li><span class="name">Price:</span><span class="value">$4,950,000</span></li>
      </ul></section>
      <div class="property-content"><p>Current detail description.</p></div>
      <div class="property-gallery"><img src="https://www.pyramidbrokerage.com/listing.jpg"></div>
      <section class="more-info">
        <a href="https://www.pyramidbrokerage.com/flyer.pdf">View flyer</a>
      </section>
      <ul class="agent-contact-list"><li>
        <img src="https://www.pyramidbrokerage.com/agent.jpg">
        <div class="details">
          <a class="name" href="https://www.pyramidbrokerage.com/agents/jane-doe/">Jane Doe</a>
          <span class="name">Albany Office</span>
          <span class="title">Senior Director</span>
          <a class="tel" href="tel:+15185550100">518-555-0100</a>
          <a class="mail" href="mailto:jane@example.com?Subject=Website">jane@example.com</a>
        </div>
      </li></ul>
    </main></body>
  </html>
`;

test("Pyramid inventory URL requests only the taxonomy terms consumed by mapping", () => {
  const url = new URL(pyramidInventoryUrl(21));
  assert.equal(url.pathname, "/wp-json/wp/v2/pbc-listings");
  assert.equal(url.searchParams.get("per_page"), String(PYRAMID_PAGE_SIZE));
  assert.equal(url.searchParams.get("page"), "21");
  assert.equal(url.searchParams.get("orderby"), "id");
  assert.equal(url.searchParams.get("order"), "asc");
  assert.equal(url.searchParams.get("_embed"), "wp:term");
});

test("Pyramid maps requested taxonomy terms across complete pages and convergence passes", async () => {
  const requestedPages: number[] = [];
  const snapshot = await fetchPyramidSnapshot(async (input) => {
    const url = new URL(String(input));
    const page = Number(url.searchParams.get("page"));
    requestedPages.push(page);
    const ids = page === 1 ? Array.from({ length: 100 }, (_, index) => index + 1) : [101];
    const body = ids.map((id) => {
      const fixture = row(id, PYRAMID_SALE_TERM_ID);
      const value: Record<string, unknown> = { ...fixture };
      // The real REST API supplies labels only when the request embeds terms.
      if (url.searchParams.get("_embed") === "wp:term") {
        value._embedded = { "wp:term": fixture._embedded["wp:term"] };
      } else {
        delete value._embedded;
      }
      return value;
    });
    return Response.json(body, {
      headers: { "X-WP-Total": "101", "X-WP-TotalPages": "2" },
    });
  }, 2);
  assert.deepEqual(requestedPages, [1, 2, 1, 2]);
  assert.equal(snapshot.total, 101);
  assert.equal(snapshot.totalPages, 2);
  assert.deepEqual(snapshot.rows.map((value) => value.id), Array.from({ length: 101 }, (_, index) => index + 1));
  const mapped = snapshot.rows.map((value) => mapPyramidListing(value, "sale", snapshot.observedAt));
  for (const listing of mapped) {
    assert.equal(listing.state, "NY");
    assert.equal(listing.city, "Albany");
    assert.equal(listing.county, "Albany");
    assert.equal(listing.assetType, "Industrial");
  }
});

test("Pyramid transport enforces URL policy, manual redirects, and streaming byte limits", async () => {
  let calls = 0;
  const fetchImpl = async (input: string | URL | Request, init?: RequestInit) => {
    calls++;
    assert.equal(init?.redirect, "manual");
    if (calls === 1) {
      return new Response("", {
        status: 302,
        headers: { location: "/wp-json/wp/v2/pbc-listings?page=2" },
      });
    }
    assert.equal(
      String(input),
      "https://www.pyramidbrokerage.com/wp-json/wp/v2/pbc-listings?page=2"
    );
    return new Response("[]");
  };
  assert.equal(
    (await pyramidFetch(PYRAMID_INVENTORY_URL, fetchImpl, 1_000, 1, 100)).body,
    "[]"
  );
  assert.equal(calls, 2);

  for (const unsafe of [
    "http://www.pyramidbrokerage.com/wp-json/wp/v2/pbc-listings",
    "https://user@www.pyramidbrokerage.com/wp-json/wp/v2/pbc-listings",
    "https://www.pyramidbrokerage.com:8443/wp-json/wp/v2/pbc-listings",
  ]) {
    await assert.rejects(
      () => pyramidFetch(unsafe, async () => new Response("[]"), 1_000, 1),
      /unsafe URL/
    );
  }
  await assert.rejects(
    () =>
      pyramidFetch(
        PYRAMID_INVENTORY_URL,
        async () =>
          new Response("", {
            status: 302,
            headers: { location: "https://evil.example/listings/" },
          }),
        1_000,
        1
      ),
    /unsafe redirect target/
  );

  let cancelled = false;
  await assert.rejects(
    () =>
      pyramidFetch(
        PYRAMID_INVENTORY_URL,
        async () =>
          new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(new TextEncoder().encode("abc"));
                controller.enqueue(new TextEncoder().encode("def"));
              },
              cancel() {
                cancelled = true;
              },
            })
          ),
        1_000,
        1,
        4
      ),
    /exceeds 4 bytes/
  );
  assert.equal(cancelled, true);
});

test("Pyramid exact snapshot accepts a complete mutually exclusive taxonomy partition", () => {
  assert.doesNotThrow(() =>
    assertPyramidSnapshot(
      [row(1, PYRAMID_SALE_TERM_ID), row(2, PYRAMID_LEASE_TERM_ID)],
      2,
      1
    )
  );
});

test("Pyramid exact snapshot rejects partial pages, duplicates, and unknown taxonomy", () => {
  assert.throws(
    () => assertPyramidSnapshot([row(1, PYRAMID_SALE_TERM_ID)], 2, 1),
    /incomplete/
  );
  assert.throws(
    () =>
      assertPyramidSnapshot(
        [row(1, PYRAMID_SALE_TERM_ID), row(1, PYRAMID_LEASE_TERM_ID)],
        2,
        1
      ),
    /duplicate/
  );
  assert.throws(
    () => assertPyramidSnapshot([row(1, 999)], 1, 1),
    /unknown sale-type/
  );
});

test("Pyramid detail parser extracts identity, facts, broker, brochure, and image", () => {
  const detail = parsePyramidDetail(detailHtml, 101);
  assert.equal(detail.postId, 101);
  assert.equal(detail.facts.Price, "$4,950,000");
  assert.equal(detail.description, "Current detail description.");
  assert.deepEqual(detail.brochures.map((document) => document.url), [
    "https://www.pyramidbrokerage.com/flyer.pdf",
  ]);
  assert.deepEqual(detail.photos, ["https://www.pyramidbrokerage.com/listing.jpg"]);
  assert.equal(detail.contacts[0].name, "Jane Doe");
  assert.equal(detail.contacts[0].email, "jane@example.com");
});

test("Pyramid detail parser rejects missing shells and mismatched shortlink identity", () => {
  assert.throws(() => parsePyramidDetail("<html>Page not found</html>", 101), /shell/);
  assert.throws(() => parsePyramidDetail(detailHtml, 102), /identity mismatch/);
  assert.throws(
    () => parsePyramidDetail(detailHtml.replace("Sale/Lease:", "Tenure:"), 101),
    /omitted its Sale\/Lease/
  );
  for (const shortlink of [
    "https://example.com/?p=101",
    "http://www.pyramidbrokerage.com/?p=101",
    "https://user@www.pyramidbrokerage.com/?p=101",
    "https://www.pyramidbrokerage.com:8443/?p=101",
    "https://www.pyramidbrokerage.com/listings/?p=101",
    "https://www.pyramidbrokerage.com/?p=101&p=101",
    "https://www.pyramidbrokerage.com/?p=101&preview=true",
    "https://www.pyramidbrokerage.com/?p=0",
  ]) {
    assert.throws(
      () =>
        parsePyramidDetail(
          detailHtml.replace(
            "https://www.pyramidbrokerage.com/?p=101",
            shortlink
          ),
          101
        ),
      /shortlink ID/
    );
  }
});

test("Pyramid snapshot requires two identical complete ordered generations", async () => {
  const generationOne = row(1, PYRAMID_SALE_TERM_ID);
  const generationTwo = row(2, PYRAMID_SALE_TERM_ID);
  let calls = 0;
  const converged = await fetchPyramidSnapshot(
    async (input) => {
      const url = new URL(String(input));
      assert.equal(url.searchParams.get("orderby"), "id");
      assert.equal(url.searchParams.get("order"), "asc");
      const body = calls++ === 0 ? [generationOne] : [generationTwo];
      return Response.json(body, {
        headers: { "X-WP-Total": "1", "X-WP-TotalPages": "1" },
      });
    },
    3
  );
  assert.equal(calls, 3);
  assert.equal(converged.rows[0].id, 2);

  let divergentCalls = 0;
  await assert.rejects(
    () =>
      fetchPyramidSnapshot(
        async () =>
          Response.json(
            [divergentCalls++ === 0 ? generationOne : generationTwo],
            {
              headers: { "X-WP-Total": "1", "X-WP-TotalPages": "1" },
            }
          ),
        2
      ),
    /did not converge/
  );
});

test("Pyramid snapshot fingerprint includes provider modification state", async () => {
  const original = row(1, PYRAMID_SALE_TERM_ID);
  const modified = { ...original, modified_gmt: "2026-07-30T16:00:00" };
  let calls = 0;
  const snapshot = await fetchPyramidSnapshot(
    async () =>
      Response.json([calls++ === 0 ? original : modified], {
        headers: { "X-WP-Total": "1", "X-WP-TotalPages": "1" },
      }),
    3
  );
  assert.equal(calls, 3);
  assert.equal(snapshot.rows[0].modified_gmt, "2026-07-30T16:00:00");
});

test("Pyramid snapshot fingerprint covers every mapper-consumed inventory field", async (t) => {
  const cases: Array<[string, (value: any) => void]> = [
    ["title, content, and slug", (value) => {
      value.title.rendered = "Changed title";
      value.content.rendered = "<p>Changed content</p>";
      value.slug = "changed-slug";
    }],
    ["raw taxonomy identifiers", (value) => {
      value["property-use"] = [251];
      value.city = [301];
      value.county = [302];
      value.state = [303];
    }],
    ["embedded taxonomy names", (value) => {
      value._embedded["wp:term"][0][0].name = "Changed property use";
      value._embedded["wp:term"][1][0].name = "Changed city";
      value._embedded["wp:term"][2][0].name = "Changed county";
      value._embedded["wp:term"][3][0].name = "Changed state";
    }],
    ["embedded featured media", (value) => {
      value._embedded["wp:featuredmedia"][0].source_url =
        "https://www.pyramidbrokerage.com/changed-featured.jpg";
    }],
    ["Yoast featured-media fallback", (value) => {
      delete value._embedded["wp:featuredmedia"];
      value.yoast_head_json = {
        og_image: [{ url: "https://www.pyramidbrokerage.com/changed-yoast.jpg" }],
      };
    }],
  ];

  for (const [name, mutate] of cases) {
    await t.test(name, async () => {
      const original = row(1, PYRAMID_SALE_TERM_ID);
      const changed = structuredClone(original);
      mutate(changed);
      let calls = 0;
      const snapshot = await fetchPyramidSnapshot(
        async () =>
          Response.json([calls++ === 0 ? original : changed], {
            headers: { "X-WP-Total": "1", "X-WP-TotalPages": "1" },
          }),
        3
      );
      assert.equal(calls, 3);
      assert.deepEqual(snapshot.rows[0], changed);
    });
  }
});

test("Pyramid snapshot fingerprint is stable across JSON object key ordering", async () => {
  const stable = row(1, PYRAMID_SALE_TERM_ID);
  const reverseKeys = (value: any): any => {
    if (Array.isArray(value)) return value.map(reverseKeys);
    if (!value || typeof value !== "object") return value;
    return Object.fromEntries(
      Object.keys(value)
        .reverse()
        .map((key) => [key, reverseKeys(value[key])])
    );
  };
  const reordered = reverseKeys(stable);
  let calls = 0;
  const snapshot = await fetchPyramidSnapshot(
    async () =>
      Response.json([calls++ === 0 ? stable : reordered], {
        headers: { "X-WP-Total": "1", "X-WP-TotalPages": "1" },
      }),
    2
  );
  assert.equal(calls, 2);
  assert.deepEqual(snapshot.rows[0], reordered);
});

test("Pyramid mapper emits strict live freshness on detail and preserves children in monitor", () => {
  resetBrokerStateForTests();
  const inventory = row(101, PYRAMID_SALE_TERM_ID);
  const detail = parsePyramidDetail(detailHtml, 101);
  const full = mapPyramidListing(
    inventory,
    "sale",
    "2026-07-30T18:00:00.000Z",
    detail,
    "2026-07-30T18:01:00.000Z"
  );
  assert.equal(full.id, "listings/property-101");
  assert.equal(full.wordpressRecord.id, 101);
  assert.equal(full.salePriceUsd, 4_950_000);
  assert.equal(full.freshnessProvenance.detailScope, "detail_page");
  assert.equal(full.freshnessProvenance.cacheDisposition, "live");
  assert.equal(full.preserveChildCollections, undefined);

  const monitor = mapPyramidListing(
    inventory,
    "sale",
    "2026-07-30T18:00:00.000Z"
  );
  assert.equal(monitor.freshnessProvenance.detailScope, "inventory_only");
  assert.equal(monitor.preserveChildCollections, true);
});

const publishedPropertyHtml = detailHtml.replace('<main id="content">', `<main id="content">
    <div class="content-container">
      <div class="featured-image sale"><img src="https://cdn.photos.sparkplatform.com/cir/featured.jpg"></div>
      <div class="image-gallery"><ul>
        <li data-full="https://cdn.photos.sparkplatform.com/cir/large.jpg"><img src="https://cdn.photos.sparkplatform.com/cir/thumb.jpg"></li>
        <li data-full="https://cdn.photos.sparkplatform.com/cir/second.jpg"></li>
      </ul></div>
    </div>
    <aside><div class="listings-map"><iframe src="https://www.google.com/maps/embed/v1/place?q=100%20Main%20St%2C%20Albany%2C%20NY"></iframe></div></aside>`)
  .replace("</body>", `<footer><iframe src="https://www.google.com/maps/embed/v1/place?q=Office%20Address"></iframe>
    <img src="https://cdn.photos.sparkplatform.com/cir/footer-logo.jpg"></footer></body>`);

test("Pyramid canonical keys preserve literal owned paths and native post evidence", () => {
  const inventory = { ...row(101, PYRAMID_SALE_TERM_ID), link: "https://www.pyramidbrokerage.com/listings/Mixed%20Case/?view=1#photos" };
  const mapped = mapPyramidListing(inventory, "sale", "2026-07-30T18:00:00Z");
  assert.equal(mapped.id, "listings/Mixed%20Case");
  assert.equal(mapped.wordpressRecord.id, 101);
  assert.equal(mapped.freshnessProvenance.identityMethod, "literal_owned_listing_path+wordpress_post_id");
  const changedSlug = mapPyramidListing({ ...inventory, link: "https://www.pyramidbrokerage.com/listings/changed/" }, "sale", "2026-07-30T18:00:00Z");
  assert.notEqual(changedSlug.id, mapped.id);
  for (const link of ["https://evil.test/listings/x/", "https://www.pyramidbrokerage.com.evil.test/listings/x/", "https://user@www.pyramidbrokerage.com/listings/x/", "https://www.pyramidbrokerage.com/not-listings/x/"]) {
    assert.throws(() => mapPyramidListing({ ...inventory, link }, "sale", "2026-07-30T18:00:00Z"), /listing URL/);
  }
});

test("Pyramid refuses different native posts claiming one canonical path", () => {
  const first = row(1, PYRAMID_SALE_TERM_ID);
  assert.throws(() => assertPyramidSnapshot([first, { ...row(2, PYRAMID_LEASE_TERM_ID), link: first.link }], 2, 1), /canonical.*path/);
});

test("Pyramid retains primary featured and gallery images instead of a page logo", () => {
  const detail = parsePyramidDetail(publishedPropertyHtml, 101);
  for (const image of ["featured.jpg", "large.jpg", "second.jpg"]) {
    assert.ok(detail.photos.includes(`https://cdn.photos.sparkplatform.com/cir/${image}`));
  }
  assert.ok(!detail.photos.some((url: string) => /logo|thumb/.test(url)));
});

test("Pyramid retains the literal property map address and scoped CDN photos", () => {
  const detail = parsePyramidDetail(publishedPropertyHtml, 101);
  const mapped = mapPyramidListing(row(101, PYRAMID_SALE_TERM_ID), "sale", "2026-07-30T18:00:00Z", detail, "2026-07-30T18:01:00Z");
  assert.equal(mapped.street, "100 Main St, Albany, NY");
  assert.equal(mapped.city, "Albany");
  assert.equal(mapped.state, "NY");
  assert.equal(mapped.latitude, undefined);
  assert.equal(mapped.longitude, undefined);
  for (const image of ["featured.jpg", "large.jpg", "second.jpg"]) assert.ok(mapped.photos.includes(`https://cdn.photos.sparkplatform.com/cir/${image}`));
  assert.ok(!mapped.photos.some((url: string) => /logo|thumb/.test(url)));
  for (const bad of ["http://cdn.photos.sparkplatform.com/x.jpg", "https://cdn.photos.sparkplatform.com.evil.test/x.jpg", "https://user@cdn.photos.sparkplatform.com/x.jpg", "javascript:alert(1)"]) {
    const invalid = parsePyramidDetail(publishedPropertyHtml.replaceAll("https://cdn.photos.sparkplatform.com/cir/featured.jpg", bad), 101);
    assert.ok(!invalid.photos.includes(bad));
  }
  const officeOnly = parsePyramidDetail(publishedPropertyHtml.replace('class="listings-map"', 'class="office-map"'), 101);
  assert.equal(mapPyramidListing(row(101, PYRAMID_SALE_TERM_ID), "sale", "2026-07-30T18:00:00Z", officeOnly).street, undefined);
  const coordinateQuery = parsePyramidDetail(publishedPropertyHtml.replace("100%20Main%20St%2C%20Albany%2C%20NY", "42.1,-73.2"), 101);
  assert.equal(mapPyramidListing(row(101, PYRAMID_SALE_TERM_ID), "sale", "2026-07-30T18:00:00Z", coordinateQuery).street, undefined);
  const logoOnly = parsePyramidDetail(detailHtml.replace(/<div class="property-gallery">.*?<\/div>/, "").replace("/listing.jpg", "/footer-logo.jpg"), 101);
  assert.deepEqual(logoOnly.photos, []);
});

test("Pyramid mapped historical keys and children survive actual Python staging and merge", () => {
  resetBrokerStateForTests();
  const full = mapPyramidListing(row(101, PYRAMID_SALE_TERM_ID), "sale", "2026-07-30T18:00:00Z", parsePyramidDetail(publishedPropertyHtml, 101), "2026-07-30T18:01:00Z");
  const result = spawnSync("python3", ["-c", [
    "import json,sys",
    "from cre_ingest import to_row,merge_rows",
    "d=json.load(sys.stdin)",
    "listing=dict(d['listing'],sourceKey='pyramid-brokerage')",
    "brokers={i:b for i,b in enumerate(d['brokers'])}",
    "a=to_row(listing,brokers,'2026-07-30T18:02:00Z')",
    "assert a is not None",
    "b=merge_rows(a,to_row(listing,brokers,'2026-07-30T18:02:00Z'))",
    "print(json.dumps({'id':b['external_id'],'address':b['address'],'images':len(b['images']),'contacts':len(b['contacts']),'documents':len(b['documents']),'post_id':b['raw_data']['wordpressRecord']['id']}))",
  ].join("\n")], { cwd: fileURLToPath(new URL("../../../", import.meta.url)), input: JSON.stringify({ listing: full, brokers }), encoding: "utf8", timeout: 10_000, maxBuffer: 64*1024 });
  assert.equal(result.error, undefined);
  assert.equal(result.status, 0, result.stderr);
  const staged = JSON.parse(result.stdout);
  assert.equal(staged.id, "listings/property-101");
  assert.equal(staged.post_id, 101);
  assert.equal(staged.address, "100 Main St, Albany, NY");
  assert.ok(staged.images >= 3);
  assert.equal(staged.contacts, 1);
  assert.equal(staged.documents, 1);
});

test("Pyramid refuses conflicting property maps and ignores unsafe map origins", () => {
  const another = '<div class="listings-map"><iframe src="https://www.google.com/maps/embed/v1/place?q=Different%20Property"></iframe></div>';
  assert.throws(() => parsePyramidDetail(publishedPropertyHtml.replace("</aside>", another + "</aside>"), 101), /conflicting property map addresses/);
  for (const origin of ["http://www.google.com", "https://www.google.com.evil.test", "https://user@www.google.com"]) {
    const detail = parsePyramidDetail(publishedPropertyHtml.replace("https://www.google.com/maps/embed/v1/place?q=100", origin + "/maps/embed/v1/place?q=100"), 101);
    assert.equal(detail.street, null);
  }
});

test("Pyramid monitor does not promote site branding without a featured property image", () => {
  const inventory = row(101, PYRAMID_SALE_TERM_ID);
  const mapped = mapPyramidListing({ ...inventory, featured_media: 0, _embedded: { "wp:term": inventory._embedded["wp:term"] }, yoast_head_json: { og_image: [{ url: "https://www.pyramidbrokerage.com/logo.png" }] } }, "sale", "2026-07-30T18:00:00Z");
  assert.deepEqual(mapped.photos ?? [], []);
  assert.equal(mapped.preserveChildCollections, true);
});
