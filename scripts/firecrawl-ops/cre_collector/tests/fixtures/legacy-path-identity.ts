import { observationNow } from "../../lib/observation-clock.js";
import { mapDaumListing, parseDaumDetail } from "../../sources/daum-commercial.js";
import { parseFoundryCommercialDetail } from "../../sources/foundry-commercial.js";
import { parseLyonStahlDetail } from "../../sources/lyon-stahl.js";

// Synthetic supported detail evidence, not fresh public status or inventory proof.
export function legacyIdentityListings(slug = "MiXeD%2fCase", postId = "101") {
  const at = observationNow();
  const context = { strict: true, inventoryObservedAt: at, detailObservedAt: at };
  const html = (url: string, host: string) => `<html><head>
    <link rel="canonical" href="${url}"><link rel="shortlink" href="${host}/?p=${postId}">
    <script type="application/ld+json">${JSON.stringify({
      "@type": "RealEstateListing", name: "Synthetic property", url,
      offers: { "@type": "Offer", availability: "https://schema.org/InStock" },
      mainEntity: { "@type": "Accommodation", name: "Synthetic property",
        address: { "@type": "PostalAddress", streetAddress: "1 Test Street", addressLocality: "Phoenix", addressRegion: "AZ" } },
    })}</script></head><body><h1>Synthetic property</h1>
    <ul class="property-notes"><li>For Sale</li></ul>
    <section class="main-section"><h1 class="title-property">Synthetic property</h1>
      <div class="title-about">Sale</div></section></body></html>`;
  const daumUrl = `https://daumcommercial.com/property/${slug}/`;
  const daum = mapDaumListing({
    url: daumUrl, title: "Synthetic property", tenure: "sale", tenureText: "Sale",
    city: "Phoenix", state: "AZ", postalCode: null, sizeText: null, spaceCount: null,
    assetType: null, salePriceText: null, leaseRateText: null, brokerNames: [],
    imageUrl: null, latitude: null, longitude: null,
  }, parseDaumDetail(html(daumUrl, "https://daumcommercial.com"), daumUrl), "sale", at, at);
  const foundryUrl = `https://www.foundrycommercial.com/property/${slug}/`;
  const foundry = parseFoundryCommercialDetail(html(foundryUrl, "https://www.foundrycommercial.com"), foundryUrl, "sale", context);
  const lyonUrl = `https://lyonstahl.com/properties/${slug}/`;
  const lyon = parseLyonStahlDetail(html(lyonUrl, "https://lyonstahl.com"), lyonUrl, "sale", context);
  if (foundry.kind !== "accepted" || lyon.kind !== "accepted") throw new Error("synthetic supported fixture refused");
  return [
    { ...daum, sourceKey: "daum-commercial", sourceCompany: "DAUM", transactionMode: "sale" },
    { ...foundry.listing, sourceKey: "foundry-commercial", sourceCompany: "Foundry", transactionMode: "sale" },
    { ...lyon.listing, sourceKey: "lyon-stahl", sourceCompany: "Lyon Stahl", transactionMode: "sale" },
  ];
}
