import assert from "node:assert/strict";
import test from "node:test";
import { brokers, resetBrokerStateForTests } from "../../../lib/broker.js";
import {
  assertLyonStahlUniqueProviderIds,
  classifyLyonStahlAvailability,
  lyonStahlAssetUrl,
  lyonStahlFetchText,
  lyonStahlPropertySitemaps,
  lyonStahlPropertyUrls,
  lyonStahlProviderIdentity,
  parseLyonStahlDetail,
  srcLyonStahl,
} from "../../../sources/lyon-stahl.js";

type LyonFixtureOptions = {
  id?: string;
  path?: string;
  availability?: string | null;
  name?: string;
};

function lyonFixture(options: LyonFixtureOptions = {}): string {
  const id = options.id ?? "39858";
  const path = options.path ?? "6789-sabado-tarde-rd-goleta-ca-93117";
  const availability =
    options.availability === undefined
      ? "https://schema.org/InStock"
      : options.availability;
  const name = options.name ?? "6789 Sabado Tarde Rd, Goleta, CA 93117";
  const url = `https://lyonstahl.com/properties/${path}/`;
  return `
    <html>
      <head>
        <link rel="canonical" href="${url}">
        <link rel="shortlink" href="https://lyonstahl.com/?p=${id}">
        <meta property="og:title" content="${name} - Lyon Stahl">
        <meta property="og:description" content="Coastal duplex near UCSB">
        <meta property="og:url" content="${url}">
        <meta property="og:updated_time" content="2026-04-17T18:00:05+00:00">
        <meta property="og:image"
          content="https://lyonstahl.com/wp-content/uploads/2026/03/property.jpg">
        <script type="application/ld+json">
          {
            "@context": "https://schema.org",
            "@graph": [
              {
                "@type": "RealEstateListing",
                "@id": "${url}#listing",
                "name": "${name}",
                "description": "Coastal duplex near UCSB",
                "url": "${url}",
                "price": "1800000",
                "priceCurrency": "USD",
                "image": [
                  "https://lyonstahl.com/wp-content/uploads/2026/03/property.jpg"
                ],
                "offers": {
                  "@type": "Offer",
                  "price": "1800000",
                  "priceCurrency": "USD"
                  ${availability ? `, "availability": "${availability}"` : ""}
                },
                "agent": [
                  {
                    "@type": "RealEstateAgent",
                    "name": "Spencer Chan",
                    "jobTitle": "Associate Agent",
                    "image": "https://lyonstahl.com/wp-content/uploads/team/spencer.png"
                  }
                ]
              },
              {
                "@type": "ApartmentComplex",
                "@id": "${url}#property",
                "name": "6789 Sabado Tarde Rd",
                "url": "${url}",
                "address": {
                  "@type": "PostalAddress",
                  "streetAddress": "6789 Sabado Tarde Rd",
                  "addressLocality": "Goleta",
                  "addressRegion": "CA",
                  "postalCode": "93117",
                  "addressCountry": "US"
                },
                "geo": {
                  "@type": "GeoCoordinates",
                  "latitude": "34.4124",
                  "longitude": "-119.8610"
                },
                "numberOfRooms": "2",
                "yearBuilt": "1968",
                "floorSize": {
                  "@type": "QuantitativeValue",
                  "value": "2200",
                  "unitCode": "FTK"
                },
                "accommodationCategory": "Duplex / Student Housing",
                "additionalProperty": [
                  {
                    "@type": "PropertyValue",
                    "name": "Current CAP Rate (In-Place)",
                    "value": "5.25%"
                  },
                  {
                    "@type": "PropertyValue",
                    "name": "Stabilized CAP Rate (Projected)",
                    "value": "5.51%"
                  }
                ]
              }
            ]
          }
        </script>
      </head>
      <body class="single-properties postid-${id}">
        <h1>${name}</h1>
      </body>
    </html>`;
}

test("Lyon Stahl sitemap parsers keep only numbered same-host property sitemaps and HTML details", () => {
  assert.deepEqual(
    lyonStahlPropertySitemaps(`
      <sitemapindex>
        <sitemap><loc>https://lyonstahl.com/post-sitemap.xml</loc></sitemap>
        <sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap>
        <sitemap><loc>https://www.lyonstahl.com/properties-sitemap2.xml</loc></sitemap>
        <sitemap><loc>https://example.com/post-sitemap.xml</loc></sitemap>
      </sitemapindex>
    `),
    [
      "https://lyonstahl.com/properties-sitemap1.xml",
      "https://lyonstahl.com/properties-sitemap2.xml",
    ]
  );
  assert.deepEqual(
    lyonStahlPropertyUrls(`
      <urlset>
        <url><loc>https://lyonstahl.com/properties/alpha/</loc></url>
        <url><loc>https://www.lyonstahl.com/properties/alpha/</loc></url>
      </urlset>
    `),
    ["https://lyonstahl.com/properties/alpha/"]
  );
});

test("Lyon Stahl sitemap parsing rejects malformed structure, unsafe URLs, and empty property snapshots", () => {
  assert.throws(
    () => lyonStahlPropertySitemaps("<urlset><url><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></url></urlset>"),
    /sitemapindex root/
  );
  assert.throws(
    () => lyonStahlPropertySitemaps("<sitemapindex><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemapindex>"),
    /sitemap child/
  );
  for (const unsafe of [
    "https://example.com/properties/foreign/",
    "http://lyonstahl.com/properties/insecure/",
    "https://user@lyonstahl.com/properties/credentialed/",
    "https://lyonstahl.com:8443/properties/ported/",
  ]) {
    assert.throws(
      () => lyonStahlPropertyUrls(`<urlset><url><loc>${unsafe}</loc></url></urlset>`),
      /URL 0 is invalid/
    );
  }
  assert.throws(
    () =>
      lyonStahlPropertyUrls(
        `<urlset>
          <url><loc>https://lyonstahl.com/properties/valid/</loc></url>
          <url><loc>https://example.com/properties/invalid/</loc></url>
        </urlset>`
      ),
    /URL 1 is invalid/
  );
  for (const unsafe of [
    "https://example.com/properties-sitemap3.xml",
    "http://lyonstahl.com/properties-sitemap3.xml",
    "https://user@lyonstahl.com/properties-sitemap3.xml",
    "https://lyonstahl.com:8443/properties-sitemap3.xml",
    "https://lyonstahl.com/properties-sitemap3.xml?session=synthetic-secret",
    "https://lyonstahl.com/properties-sitemap3.xml#synthetic-secret",
    "javascript:properties-sitemap3.xml",
  ]) {
    assert.throws(
      () =>
        lyonStahlPropertySitemaps(
          `<sitemapindex>
            <sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap>
            <sitemap><loc>${unsafe}</loc></sitemap>
          </sitemapindex>`
        ),
      /property loc 1 is invalid/
    );
  }
  for (const unsafe of [
    "https://lyonstahl.com/properties/query/?session=synthetic-secret",
    "https://lyonstahl.com/properties/fragment/#synthetic-secret",
  ]) {
    assert.throws(
      () => lyonStahlPropertyUrls(`<urlset><url><loc>${unsafe}</loc></url></urlset>`),
      /URL 0 is invalid/
    );
  }
});

test("Lyon Stahl asset URLs preserve benign queries and reject unsafe or sensitive URLs", () => {
  assert.equal(
    lyonStahlAssetUrl("https://cdn.example.com/photo.jpg?ver=2&w=1200"),
    "https://cdn.example.com/photo.jpg?ver=2&w=1200"
  );
  for (const unsafe of [
    "http://lyonstahl.com/wp-content/uploads/photo.jpg",
    "https://user@lyonstahl.com/wp-content/uploads/photo.jpg",
    "https://lyonstahl.com:8443/wp-content/uploads/photo.jpg",
    "https://lyonstahl.com/wp-content/uploads/photo.jpg?session=secret",
    "https://lyonstahl.com/wp-content/uploads/photo.jpg?download_token=secret",
    "https://lyonstahl.com/wp-content/uploads/photo.jpg?X-Goog-Signature=secret",
    "https://lyonstahl.com/wp-content/uploads/photo.jpg?ver=2&ver=3",
    "https://lyonstahl.com/wp-content/uploads/photo.jpg?w=0",
    "https://lyonstahl.com/wp-content/uploads/photo.jpg#token=secret",
  ]) {
    assert.equal(lyonStahlAssetUrl(unsafe), null);
  }
});

test("Lyon Stahl emitted provider IDs must be unique", () => {
  assert.doesNotThrow(() =>
    assertLyonStahlUniqueProviderIds([{ id: "1" }, { id: "2" }])
  );
  assert.throws(
    () => assertLyonStahlUniqueProviderIds([{ id: "1" }, { id: "1" }]),
    /duplicate provider ID 1/
  );
});

test("Lyon Stahl fetch validates redirect targets, streams byte bounds, and supplies an abort signal", async () => {
  let cancelled = false;
  await assert.rejects(
    () =>
      lyonStahlFetchText(
        "https://lyonstahl.com/properties/oversized/",
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
    await lyonStahlFetchText(
      "https://lyonstahl.com/sitemap.xml",
      "xml",
      async (_input, init) => {
        assert.equal(init?.redirect, "manual");
        redirectCalls++;
        return redirectCalls === 1
          ? new Response("", {
              status: 301,
              headers: { location: "/sitemap_index.xml" },
            })
          : new Response("<sitemapindex><sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap></sitemapindex>", {
              headers: { "content-type": "application/xml" },
            });
      },
      1_000,
      1_000,
      1
    ),
    "<sitemapindex><sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap></sitemapindex>"
  );
  assert.equal(redirectCalls, 2);
  await assert.rejects(
    () =>
      lyonStahlFetchText(
        "https://lyonstahl.com/properties/redirected/",
        "html",
        async () =>
          new Response("", {
            status: 302,
            headers: { location: "http://lyonstahl.com/properties/insecure/" },
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
      lyonStahlFetchText(
        "https://lyonstahl.com/properties/redirected/",
        "html",
        async () =>
          new Response("", {
            status: 302,
            headers: {
              location:
                `/properties/redirected/?session=${secret}#${secret}`,
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
      lyonStahlFetchText(
        "https://lyonstahl.com/properties/timeout/",
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

test("Lyon Stahl rejects the run when any indexed property sitemap has no valid snapshot", async (t) => {
  const originalFetch = globalThis.fetch;
  const responses = new Map<string, string>([
    [
      "https://lyonstahl.com/sitemap.xml",
      `<sitemapindex>
        <sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap>
        <sitemap><loc>https://lyonstahl.com/properties-sitemap2.xml</loc></sitemap>
      </sitemapindex>`,
    ],
    [
      "https://lyonstahl.com/properties-sitemap1.xml",
      `<urlset><url><loc>https://lyonstahl.com/properties/valid/</loc></url></urlset>`,
    ],
    [
      "https://lyonstahl.com/properties-sitemap2.xml",
      `<urlset><url><loc>https://example.com/properties/not-lyon/</loc></url></urlset>`,
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
    () => srcLyonStahl("sale", Infinity, false),
    /properties-sitemap2\.xml is invalid/
  );
});

test("Lyon Stahl availability policy admits only known Schema.org tokens", () => {
  assert.equal(
    classifyLyonStahlAvailability("https://schema.org/InStock").disposition,
    "active"
  );
  assert.equal(classifyLyonStahlAvailability("SoldOut").disposition, "terminal");
  assert.equal(classifyLyonStahlAvailability("BackOrder").disposition, "held");
  assert.equal(classifyLyonStahlAvailability(null).disposition, "held");
});

test("Lyon Stahl provider identity requires matching canonical and numeric shortlink", () => {
  const url =
    "https://lyonstahl.com/properties/6789-sabado-tarde-rd-goleta-ca-93117/";
  assert.equal(lyonStahlProviderIdentity(lyonFixture(), url), "39858");
  assert.equal(
    lyonStahlProviderIdentity(
      lyonFixture().replace(
        "/properties/6789-sabado-tarde-rd-goleta-ca-93117/",
        "/properties/different/"
      ),
      url
    ),
    null
  );
  assert.equal(
    lyonStahlProviderIdentity(lyonFixture().replace("?p=39858", "?post=39858"), url),
    null
  );
  for (const shortlink of [
    "https://www.lyonstahl.com/?p=39858",
    "http://lyonstahl.com/?p=39858",
    "https://user@lyonstahl.com/?p=39858",
    "https://lyonstahl.com:8443/?p=39858",
    "https://lyonstahl.com/properties/?p=39858",
    "https://lyonstahl.com/?p=39858&p=39858",
    "https://lyonstahl.com/?p=39858&preview=true",
    "https://lyonstahl.com/?p=0",
    "https://lyonstahl.com/?p=9007199254740992",
    "https://lyonstahl.com/?p=39858#preview",
    "/?p=39858",
  ]) {
    assert.equal(
      lyonStahlProviderIdentity(
        lyonFixture().replace("https://lyonstahl.com/?p=39858", shortlink),
        url
      ),
      null
    );
  }
});

test("Lyon Stahl parser maps current rich JSON-LD with stable identity and provenance", () => {
  resetBrokerStateForTests();
  const url =
    "https://lyonstahl.com/properties/6789-sabado-tarde-rd-goleta-ca-93117/";
  const outcome = parseLyonStahlDetail(lyonFixture(), url, "sale", {
    strict: true,
    inventoryObservedAt: "2026-07-30T16:00:00.000Z",
    detailObservedAt: "2026-07-30T16:01:00.000Z",
  });
  assert.equal(outcome.kind, "accepted");
  if (outcome.kind !== "accepted") return;
  assert.equal(outcome.listing.id, "39858");
  assert.equal(outcome.listing.transactionType, "Sale");
  assert.equal(outcome.listing.street, "6789 Sabado Tarde Rd");
  assert.equal(outcome.listing.city, "Goleta");
  assert.equal(outcome.listing.state, "CA");
  assert.equal(outcome.listing.salePriceUsd, 1800000);
  assert.equal(outcome.listing.capRatePct, 5.25);
  assert.equal(outcome.listing.buildingSizeSqft, 2200);
  assert.equal(outcome.listing.yearBuilt, 1968);
  assert.equal(outcome.listing.units, 2);
  assert.equal(outcome.listing.photos.length, 1);
  assert.equal(outcome.listing.brokerIds.length, 1);
  assert.equal(outcome.listing.statusBadge, "instock");
  assert.equal(
    outcome.listing.freshnessProvenance.identityMethod,
    "wordpress_shortlink_id"
  );
});

test("Lyon Stahl parser drops unsafe agent avatars before broker registration", () => {
  resetBrokerStateForTests();
  const html = lyonFixture().replace(
    "https://lyonstahl.com/wp-content/uploads/team/spencer.png",
    "https://user@lyonstahl.com/wp-content/uploads/team/spencer.png?token=secret"
  );
  const outcome = parseLyonStahlDetail(
    html,
    "https://lyonstahl.com/properties/6789-sabado-tarde-rd-goleta-ca-93117/",
    "sale",
    { strict: true }
  );
  assert.equal(outcome.kind, "accepted");
  assert.equal(brokers[0].avatarUrl, null);
});

test("Lyon Stahl parser output drops unsafe assets and rejects query-bearing requested identities", () => {
  resetBrokerStateForTests();
  const url =
    "https://lyonstahl.com/properties/6789-sabado-tarde-rd-goleta-ca-93117/";
  const html = lyonFixture()
    .replaceAll(
      "https://lyonstahl.com/wp-content/uploads/2026/03/property.jpg",
      "https://lyonstahl.com/wp-content/uploads/2026/03/property.jpg?session=synthetic-secret#token"
    )
    .replace(
      "https://lyonstahl.com/wp-content/uploads/team/spencer.png",
      "https://lyonstahl.com/wp-content/uploads/team/spencer.png?session=synthetic-secret#token"
    );
  const outcome = parseLyonStahlDetail(html, url, "sale", { strict: true });
  assert.equal(outcome.kind, "accepted");
  if (outcome.kind !== "accepted") return;
  assert.deepEqual(outcome.listing.photos ?? [], []);
  assert.equal(brokers[0].avatarUrl, null);
  assert.equal(JSON.stringify(outcome.listing).includes("synthetic-secret"), false);
  assert.equal(
    parseLyonStahlDetail(
      lyonFixture(),
      `${url}?session=synthetic-secret`,
      "sale",
      { strict: true }
    ).kind,
    "rejected"
  );
});

test("Lyon Stahl parser excludes terminal rows and holds missing or novel availability", () => {
  const url =
    "https://lyonstahl.com/properties/6789-sabado-tarde-rd-goleta-ca-93117/";
  assert.equal(
    parseLyonStahlDetail(
      lyonFixture({ availability: "https://schema.org/SoldOut" }),
      url,
      "sale",
      { strict: true }
    ).kind,
    "terminal"
  );
  assert.equal(
    parseLyonStahlDetail(
      lyonFixture({ availability: null }),
      url,
      "sale",
      { strict: true }
    ).kind,
    "held"
  );
  assert.equal(
    parseLyonStahlDetail(
      lyonFixture({ availability: "https://schema.org/BackOrder" }),
      url,
      "sale",
      { strict: true }
    ).kind,
    "held"
  );
});

test("Lyon Stahl lease pass is explicitly empty and performs no network calls", async (t) => {
  const originalFetch = globalThis.fetch;
  let calls = 0;
  globalThis.fetch = (async () => {
    calls++;
    return new Response("unexpected", { status: 500 });
  }) as typeof fetch;
  t.after(() => {
    globalThis.fetch = originalFetch;
  });

  const result = await srcLyonStahl("lease", Infinity, false);
  assert.equal(result.totalAvailable, 0);
  assert.deepEqual(result.listings, []);
  assert.equal(calls, 0);
});

test("Lyon Stahl source holds unknown availability, marks coverage incomplete, and never requests PDFs", async (t) => {
  resetBrokerStateForTests();
  const originalFetch = globalThis.fetch;
  const calls: string[] = [];
  const activeUrl = "https://lyonstahl.com/properties/active-sale/";
  const unknownUrl = "https://lyonstahl.com/properties/unknown-status/";
  const responses = new Map<string, string>([
    [
      "https://lyonstahl.com/sitemap.xml",
      `<sitemapindex>
        <sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap>
        <sitemap><loc>https://lyonstahl.com/properties-sitemap2.xml</loc></sitemap>
      </sitemapindex>`,
    ],
    [
      "https://lyonstahl.com/properties-sitemap1.xml",
      `<urlset>
        <url><loc>${activeUrl}</loc></url>
      </urlset>`,
    ],
    [
      "https://lyonstahl.com/properties-sitemap2.xml",
      `<urlset><url><loc>${unknownUrl}</loc></url></urlset>`,
    ],
    [
      activeUrl,
      lyonFixture({
        id: "40001",
        path: "active-sale",
        name: "Active Sale",
      }),
    ],
    [
      unknownUrl,
      lyonFixture({
        id: "40002",
        path: "unknown-status",
        availability: "https://schema.org/BackOrder",
        name: "Unknown Status",
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

  const result = await srcLyonStahl("sale", Infinity, false);
  assert.equal(result.totalAvailable, 2);
  assert.equal(result.listings.length, 1);
  assert.equal(result.listings[0].id, "40001");
  assert.equal(result.truncated, true);
  assert.match(result.note ?? "", /1 detail page\(s\) held/);
  assert.equal(calls.some((url) => /\.pdf(?:$|[?#])/i.test(url)), false);
});

test("Lyon Stahl source rejects duplicate emitted shortlink IDs across different property URLs", async (t) => {
  resetBrokerStateForTests();
  const originalFetch = globalThis.fetch;
  const one = "https://lyonstahl.com/properties/duplicate-one/";
  const two = "https://lyonstahl.com/properties/duplicate-two/";
  const responses = new Map<string, string>([
    [
      "https://lyonstahl.com/sitemap.xml",
      `<sitemapindex><sitemap><loc>https://lyonstahl.com/properties-sitemap1.xml</loc></sitemap></sitemapindex>`,
    ],
    [
      "https://lyonstahl.com/properties-sitemap1.xml",
      `<urlset><url><loc>${one}</loc></url><url><loc>${two}</loc></url></urlset>`,
    ],
    [one, lyonFixture({ id: "41001", path: "duplicate-one", name: "Duplicate One" })],
    [two, lyonFixture({ id: "41001", path: "duplicate-two", name: "Duplicate Two" })],
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
    () => srcLyonStahl("sale", Infinity, false),
    /duplicate provider ID 41001/
  );
});
