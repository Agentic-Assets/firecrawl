import assert from "node:assert/strict";
import test from "node:test";
import {
  assertFoundryUniqueProviderIds,
  classifyFoundryStatus,
  foundryAssetUrl,
  foundryFetchText,
  foundryPropertySitemaps,
  foundryPropertyUrls,
  foundryProviderIdentity,
  parseFoundryCommercialDetail,
  srcFoundryCommercial,
} from "../../../sources/foundry-commercial.js";

type FoundryFixtureOptions = {
  id?: string;
  path?: string;
  status?: string;
  title?: string;
  transactionNote?: string | null;
};

function foundryFixture(options: FoundryFixtureOptions = {}): string {
  const id = options.id ?? "23716";
  const path = options.path ?? "offices-at-wade-park-wade-ii";
  const status = options.status ?? "For Lease";
  const title = options.title ?? "Offices at Wade Park | Wade II";
  const transactionNote = options.transactionNote ?? null;
  const url = `https://www.foundrycommercial.com/property/${path}/`;
  return `
    <html>
      <head>
        <link rel="canonical" href="${url}">
        <link rel="shortlink" href="https://www.foundrycommercial.com/?p=${id}">
        <meta property="og:title" content="${title}">
        <meta property="og:url" content="${url}">
        <meta property="og:image"
          content="https://www.foundrycommercial.com/wp-content/uploads/wade-two.jpg">
        <meta property="article:modified_time" content="2026-07-30T15:03:00+00:00">
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@type": "RealEstateListing",
            "name": "${title}",
            "url": "${url}",
            "description": "LEED Gold office building",
            "image": [
              "https://www.foundrycommercial.com/wp-content/uploads/wade-two.jpg"
            ],
            "mainEntity": {
              "@type": "Accommodation",
              "additionalType": "Office",
              "address": {
                "@type": "PostalAddress",
                "streetAddress": "5430 Wade Park Boulevard, Raleigh, NC, USA"
              },
              "geo": {
                "@type": "GeoCoordinates",
                "latitude": 35.8077617,
                "longitude": -78.7290377
              },
              "floorSize": {
                "@type": "QuantitativeValue",
                "value": 16464,
                "unitCode": "FTK"
              }
            }
          }
        </script>
      </head>
      <body class="single-property postid-${id}">
        <section class="hero--property">
          <h1>${title}</h1>
          <ul class="property-notes">
            <li>${status}</li>
            ${transactionNote ? `<li>${transactionNote}</li>` : ""}
            <li>office</li>
            <li>5430 Wade Park Boulevard Raleigh, North Carolina 27607</li>
          </ul>
          <a class="brochure-download-button"
            href="https://www.foundrycommercial.com/wp-content/uploads/wade-two.pdf">
            Download Brochure
          </a>
        </section>
        <section id="property-overview">
          <p class="property-description">LEED Gold office building</p>
          <table>
            <tr><th>Space Available</th><td>16,464</td></tr>
            <tr><th>Lease Rate</th><td>$32.00/SF/YR</td></tr>
          </table>
        </section>
      </body>
    </html>`;
}

test("Foundry sitemap parsers keep only same-host HTML property details", () => {
  assert.deepEqual(
    foundryPropertySitemaps(`
      <sitemapindex>
        <sitemap><loc>https://www.foundrycommercial.com/post-sitemap.xml</loc></sitemap>
        <sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap>
        <sitemap><loc>https://foundrycommercial.com/property-sitemap2.xml</loc></sitemap>
        <sitemap><loc>https://example.com/post-sitemap.xml</loc></sitemap>
      </sitemapindex>
    `),
    [
      "https://www.foundrycommercial.com/property-sitemap.xml",
      "https://foundrycommercial.com/property-sitemap2.xml",
    ]
  );
  assert.deepEqual(
    foundryPropertyUrls(`
      <urlset>
        <url><loc>https://www.foundrycommercial.com/property/alpha/</loc></url>
        <url><loc>https://www.foundrycommercial.com/property/alpha/</loc></url>
      </urlset>
    `),
    ["https://www.foundrycommercial.com/property/alpha/"]
  );
});

test("Foundry sitemap parsing rejects malformed structure, unsafe URLs, and empty property snapshots", () => {
  assert.throws(
    () => foundryPropertySitemaps("<urlset><url><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></url></urlset>"),
    /sitemapindex root/
  );
  assert.throws(
    () => foundryPropertySitemaps("<sitemapindex><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemapindex>"),
    /sitemap child/
  );
  assert.throws(
    () => foundryPropertyUrls("<urlset><url><loc>https://example.com/property/foreign/</loc></url></urlset>"),
    /URL 0 is invalid/
  );
  assert.throws(
    () => foundryPropertyUrls("<urlset><url><loc>http://www.foundrycommercial.com/property/insecure/</loc></url></urlset>"),
    /URL 0 is invalid/
  );
  assert.throws(
    () => foundryPropertyUrls("<urlset><url><loc>https://user@www.foundrycommercial.com/property/credentialed/</loc></url></urlset>"),
    /URL 0 is invalid/
  );
  assert.throws(
    () => foundryPropertyUrls("<urlset><url><loc>https://www.foundrycommercial.com:8443/property/ported/</loc></url></urlset>"),
    /URL 0 is invalid/
  );
  assert.throws(
    () =>
      foundryPropertyUrls(
        `<urlset>
          <url><loc>https://www.foundrycommercial.com/property/valid/</loc></url>
          <url><loc>https://example.com/property/invalid/</loc></url>
        </urlset>`
      ),
    /URL 1 is invalid/
  );
  for (const unsafe of [
    "https://example.com/property-sitemap2.xml",
    "http://www.foundrycommercial.com/property-sitemap2.xml",
    "https://user@www.foundrycommercial.com/property-sitemap2.xml",
    "https://www.foundrycommercial.com:8443/property-sitemap2.xml",
    "https://www.foundrycommercial.com/property-sitemap2.xml?session=synthetic-secret",
    "https://www.foundrycommercial.com/property-sitemap2.xml#synthetic-secret",
    "javascript:property-sitemap2.xml",
  ]) {
    assert.throws(
      () =>
        foundryPropertySitemaps(
          `<sitemapindex>
            <sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap>
            <sitemap><loc>${unsafe}</loc></sitemap>
          </sitemapindex>`
        ),
      /property loc 1 is invalid/
    );
  }
  for (const unsafe of [
    "https://www.foundrycommercial.com/property/query/?session=synthetic-secret",
    "https://www.foundrycommercial.com/property/fragment/#synthetic-secret",
  ]) {
    assert.throws(
      () => foundryPropertyUrls(`<urlset><url><loc>${unsafe}</loc></url></urlset>`),
      /URL 0 is invalid/
    );
  }
});

test("Foundry asset URLs preserve benign queries and reject unsafe or sensitive URLs", () => {
  assert.equal(
    foundryAssetUrl("https://cdn.example.com/brochure.pdf?ver=2&w=1200"),
    "https://cdn.example.com/brochure.pdf?ver=2&w=1200"
  );
  for (const unsafe of [
    "http://www.foundrycommercial.com/wp-content/photo.jpg",
    "https://user@www.foundrycommercial.com/wp-content/photo.jpg",
    "https://www.foundrycommercial.com:8443/wp-content/photo.jpg",
    "https://www.foundrycommercial.com/wp-content/photo.jpg?session=secret",
    "https://www.foundrycommercial.com/wp-content/photo.jpg?download_token=secret",
    "https://www.foundrycommercial.com/wp-content/photo.jpg?X-Amz-Signature=secret",
    "https://www.foundrycommercial.com/wp-content/photo.jpg?ver=2&ver=3",
    "https://www.foundrycommercial.com/wp-content/photo.jpg?w=0",
    "https://www.foundrycommercial.com/wp-content/photo.jpg#token=secret",
  ]) {
    assert.equal(foundryAssetUrl(unsafe), null);
  }
});

test("Foundry emitted provider IDs must be unique", () => {
  assert.doesNotThrow(() =>
    assertFoundryUniqueProviderIds([{ id: "1" }, { id: "2" }])
  );
  assert.throws(
    () => assertFoundryUniqueProviderIds([{ id: "1" }, { id: "1" }]),
    /duplicate provider ID 1/
  );
});

test("Foundry fetch validates redirect targets, streams byte bounds, and supplies an abort signal", async () => {
  let cancelled = false;
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/oversized/",
        "html",
        async (_input, init) => {
          assert.ok(init?.signal);
          assert.equal(init?.redirect, "manual");
          return new Response(
            new ReadableStream<Uint8Array>({
              start(controller) {
                controller.enqueue(new TextEncoder().encode("abc"));
                controller.enqueue(new TextEncoder().encode("def"));
              },
              cancel() {
                cancelled = true;
              },
            }),
            { headers: { "content-type": "text/html" } }
          );
        },
        1_000,
        4,
        1
      ),
    /exceeds 4 bytes/
  );
  assert.equal(cancelled, true);
  let redirectCalls = 0;
  assert.equal(
    await foundryFetchText(
      "https://www.foundrycommercial.com/sitemap.xml",
      "xml",
      async (input, init) => {
        assert.equal(init?.redirect, "manual");
        redirectCalls++;
        return redirectCalls === 1
          ? new Response("", {
              status: 301,
              headers: { location: "/sitemap_index.xml" },
            })
          : new Response("<sitemapindex><sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap></sitemapindex>", {
              headers: { "content-type": "application/xml" },
            });
      },
      1_000,
      1_000,
      1
    ),
    "<sitemapindex><sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap></sitemapindex>"
  );
  assert.equal(redirectCalls, 2);
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/redirected/",
        "html",
        async () =>
          new Response("", {
            status: 302,
            headers: { location: "http://www.foundrycommercial.com/property/insecure/" },
          }),
        1_000,
        1_000,
        1
      ),
    /refused unsafe html URL/
  );
  const secret = "synthetic-redirect-secret";
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/redirected/",
        "html",
        async () =>
          new Response("", {
            status: 302,
            headers: {
              location:
                `/property/redirected/?session=${secret}#${secret}`,
            },
          }),
        1_000,
        1_000,
        1
      ),
    (error: unknown) => {
      assert.match(String(error), /refused unsafe html URL/);
      assert.equal(String(error).includes(secret), false);
      return true;
    }
  );
  await assert.rejects(
    () =>
      foundryFetchText(
        "https://www.foundrycommercial.com/property/timeout/",
        "html",
        async (_input, init) =>
          new Promise<Response>((_resolve, reject) => {
            init?.signal?.addEventListener("abort", () => reject(new Error("aborted")), {
              once: true,
            });
          }),
        5,
        100,
        1
      ),
    /aborted/
  );
});

test("Foundry rejects the run when any indexed property sitemap has no valid snapshot", async (t) => {
  const originalFetch = globalThis.fetch;
  const responses = new Map<string, string>([
    [
      "https://www.foundrycommercial.com/sitemap.xml",
      `<sitemapindex>
        <sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap>
        <sitemap><loc>https://www.foundrycommercial.com/property-sitemap2.xml</loc></sitemap>
      </sitemapindex>`,
    ],
    [
      "https://www.foundrycommercial.com/property-sitemap.xml",
      `<urlset><url><loc>https://www.foundrycommercial.com/property/valid/</loc></url></urlset>`,
    ],
    [
      "https://www.foundrycommercial.com/property-sitemap2.xml",
      `<urlset><url><loc>https://example.com/property/not-foundry/</loc></url></urlset>`,
    ],
  ]);
  globalThis.fetch = (async (input) =>
    new Response(responses.get(String(input)) ?? "not found", {
      status: responses.has(String(input)) ? 200 : 404,
      headers: { "content-type": "application/xml" },
    })) as typeof fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  await assert.rejects(
    () => srcFoundryCommercial("sale", Infinity, false),
    /property-sitemap2\.xml is invalid/
  );
});

test("Foundry status policy is explicit and holds unknown values", () => {
  assert.deepEqual(classifyFoundryStatus("For Sale").tenures, ["sale"]);
  assert.deepEqual(classifyFoundryStatus("For Sale / Lease").tenures, ["sale", "lease"]);
  assert.equal(classifyFoundryStatus("Closed").disposition, "terminal");
  assert.equal(classifyFoundryStatus("Available").disposition, "active");
  assert.deepEqual(classifyFoundryStatus("Available").tenures, []);
  assert.equal(classifyFoundryStatus("Temporarily Paused").disposition, "held");
  assert.equal(classifyFoundryStatus(null).disposition, "held");
});

test("Foundry provider identity requires matching canonical and numeric shortlink", () => {
  const url =
    "https://www.foundrycommercial.com/property/offices-at-wade-park-wade-ii/";
  assert.equal(foundryProviderIdentity(foundryFixture(), url), "23716");
  assert.equal(
    foundryProviderIdentity(
      foundryFixture().replace(
        "/property/offices-at-wade-park-wade-ii/",
        "/property/different-page/"
      ),
      url
    ),
    null
  );
  assert.equal(
    foundryProviderIdentity(
      foundryFixture().replace("?p=23716", "?post=23716"),
      url
    ),
    null
  );
  for (const shortlink of [
    "https://foundrycommercial.com/?p=23716",
    "http://www.foundrycommercial.com/?p=23716",
    "https://user@www.foundrycommercial.com/?p=23716",
    "https://www.foundrycommercial.com:8443/?p=23716",
    "https://www.foundrycommercial.com/property/?p=23716",
    "https://www.foundrycommercial.com/?p=23716&p=23716",
    "https://www.foundrycommercial.com/?p=23716&preview=true",
    "https://www.foundrycommercial.com/?p=0",
    "https://www.foundrycommercial.com/?p=9007199254740992",
    "https://www.foundrycommercial.com/?p=23716#preview",
    "/?p=23716",
  ]) {
    assert.equal(
      foundryProviderIdentity(
        foundryFixture().replace(
          "https://www.foundrycommercial.com/?p=23716",
          shortlink
        ),
        url
      ),
      null
    );
  }
});

test("Foundry parser combines stable identity, JSON-LD, and provider DOM without fetching brochure PDFs", () => {
  const url =
    "https://www.foundrycommercial.com/property/offices-at-wade-park-wade-ii/";
  const outcome = parseFoundryCommercialDetail(foundryFixture(), url, "lease", {
    strict: true,
    inventoryObservedAt: "2026-07-30T16:00:00.000Z",
    detailObservedAt: "2026-07-30T16:01:00.000Z",
  });
  assert.equal(outcome.kind, "accepted");
  if (outcome.kind !== "accepted") return;
  assert.equal(outcome.listing.id, "23716");
  assert.equal(outcome.listing.name, "Offices at Wade Park | Wade II");
  assert.equal(outcome.listing.transactionType, "Lease");
  assert.equal(outcome.listing.assetType, "Office");
  assert.equal(outcome.listing.street, "5430 Wade Park Boulevard");
  assert.equal(outcome.listing.city, "Raleigh");
  assert.equal(outcome.listing.state, "NC");
  assert.equal(outcome.listing.buildingSizeSqft, 16464);
  assert.equal(outcome.listing.statusBadge, "for lease");
  assert.equal(outcome.listing.brochures[0].url.endsWith(".pdf"), true);
  assert.equal(
    outcome.listing.freshnessProvenance.identityMethod,
    "wordpress_shortlink_id"
  );
});

test("Foundry parser output drops unsafe asset URLs and rejects query-bearing requested identities", () => {
  const url =
    "https://www.foundrycommercial.com/property/offices-at-wade-park-wade-ii/";
  const html = foundryFixture()
    .replaceAll(
      "https://www.foundrycommercial.com/wp-content/uploads/wade-two.jpg",
      "https://www.foundrycommercial.com/wp-content/uploads/wade-two.jpg?session=synthetic-secret#token"
    )
    .replace(
      "https://www.foundrycommercial.com/wp-content/uploads/wade-two.pdf",
      "https://www.foundrycommercial.com/wp-content/uploads/wade-two.pdf?session=synthetic-secret#token"
    );
  const outcome = parseFoundryCommercialDetail(html, url, "lease", { strict: true });
  assert.equal(outcome.kind, "accepted");
  if (outcome.kind !== "accepted") return;
  assert.deepEqual(outcome.listing.photos ?? [], []);
  assert.deepEqual(outcome.listing.brochures ?? [], []);
  assert.equal(JSON.stringify(outcome.listing).includes("synthetic-secret"), false);
  assert.equal(
    parseFoundryCommercialDetail(
      foundryFixture(),
      `${url}?session=synthetic-secret`,
      "lease",
      { strict: true }
    ).kind,
    "rejected"
  );
});

test("Foundry parser excludes terminal pages and holds unknown or tenure-ambiguous pages", () => {
  const base = "https://www.foundrycommercial.com/property/example/";
  assert.equal(
    parseFoundryCommercialDetail(
      foundryFixture({ path: "example", status: "Closed" }),
      base,
      "sale",
      { strict: true }
    ).kind,
    "terminal"
  );
  assert.equal(
    parseFoundryCommercialDetail(
      foundryFixture({ path: "example", status: "Temporarily Paused" }),
      base,
      "sale",
      { strict: true }
    ).kind,
    "held"
  );
  assert.equal(
    parseFoundryCommercialDetail(
      foundryFixture({ path: "example", status: "Available" }),
      base,
      "sale",
      { strict: true }
    ).kind,
    "held"
  );
  assert.equal(
    parseFoundryCommercialDetail(
      foundryFixture({
        path: "example",
        status: "Available",
        transactionNote: "For Sale",
      }),
      base,
      "sale",
      { strict: true }
    ).kind,
    "accepted"
  );
  assert.equal(
    parseFoundryCommercialDetail(foundryFixture(), "https://www.foundrycommercial.com/property/offices-at-wade-park-wade-ii/", "sale", {
      strict: true,
    }).kind,
    "other_tenure"
  );
});

test("Foundry source holds unknown status, marks coverage incomplete, and never requests PDFs", async (t) => {
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  const activeUrl = "https://www.foundrycommercial.com/property/active-sale/";
  const unknownUrl = "https://www.foundrycommercial.com/property/unknown-status/";
  const responses = new Map<string, string>([
    [
      "https://www.foundrycommercial.com/sitemap.xml",
      `<sitemapindex>
        <sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap>
      </sitemapindex>`,
    ],
    [
      "https://www.foundrycommercial.com/property-sitemap.xml",
      `<urlset>
        <url><loc>${activeUrl}</loc></url>
        <url><loc>${unknownUrl}</loc></url>
      </urlset>`,
    ],
    [
      activeUrl,
      foundryFixture({
        id: "30001",
        path: "active-sale",
        status: "For Sale",
        title: "Active Sale",
      }),
    ],
    [
      unknownUrl,
      foundryFixture({
        id: "30002",
        path: "unknown-status",
        status: "Temporarily Paused",
        title: "Unknown Status",
      }),
    ],
  ]);
  globalThis.fetch = (async (input) => {
    const url = String(input);
    calls.push(url);
    const body = responses.get(url);
    if (body === undefined) return new Response("not found", { status: 404 });
    return new Response(body, {
      status: 200,
      headers: {
        "content-type": url.endsWith(".xml") ? "application/xml" : "text/html",
      },
    });
  }) as typeof fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const result = await srcFoundryCommercial("sale", Infinity, false);
  assert.equal(result.totalAvailable, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.listings[0].id, "30001");
  assert.equal(result.truncated, true);
  assert.match(result.note ?? "", /1 detail page\(s\) held/);
  assert.equal(calls.some((url) => /\.pdf(?:$|[?#])/i.test(url)), false);
});

test("Foundry source rejects duplicate emitted shortlink IDs across different property URLs", async (t) => {
  const originalFetch = globalThis.fetch;
  const one = "https://www.foundrycommercial.com/property/duplicate-one/";
  const two = "https://www.foundrycommercial.com/property/duplicate-two/";
  const responses = new Map<string, string>([
    [
      "https://www.foundrycommercial.com/sitemap.xml",
      `<sitemapindex><sitemap><loc>https://www.foundrycommercial.com/property-sitemap.xml</loc></sitemap></sitemapindex>`,
    ],
    [
      "https://www.foundrycommercial.com/property-sitemap.xml",
      `<urlset><url><loc>${one}</loc></url><url><loc>${two}</loc></url></urlset>`,
    ],
    [one, foundryFixture({ id: "31001", path: "duplicate-one", status: "For Sale" })],
    [two, foundryFixture({ id: "31001", path: "duplicate-two", status: "For Sale" })],
  ]);
  globalThis.fetch = (async (input) => {
    const body = responses.get(String(input));
    return new Response(body ?? "not found", {
      status: body === undefined ? 404 : 200,
      headers: {
        "content-type": String(input).endsWith(".xml") ? "application/xml" : "text/html",
      },
    });
  }) as typeof fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });
  await assert.rejects(
    () => srcFoundryCommercial("sale", Infinity, false),
    /duplicate provider ID 31001/
  );
});
