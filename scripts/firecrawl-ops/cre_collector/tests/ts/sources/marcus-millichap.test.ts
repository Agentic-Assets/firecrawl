import test from "node:test";
import assert from "node:assert/strict";
import {
  marcusUrl,
  parseMarcusLocation,
  parseMarcusAddress,
  extractCssUrl,
  parseMarcusTileHtml,
} from "../../../sources/marcus-millichap.js";

test("marcusUrl resolves relative property links", () => {
  assert.equal(
    marcusUrl("/properties/dallas-retail-123"),
    "https://www.marcusmillichap.com/properties/dallas-retail-123"
  );
  assert.equal(marcusUrl(""), null);
});

test("parseMarcusLocation splits city, state, and zip", () => {
  assert.deepEqual(parseMarcusLocation("Dallas, TX 75201"), {
    city: "Dallas",
    state: "TX",
    postalCode: "75201",
  });
  assert.deepEqual(parseMarcusLocation("Unparsed"), {
    city: "Unparsed",
    state: null,
    postalCode: null,
  });
});

test("parseMarcusAddress splits full street address", () => {
  assert.deepEqual(parseMarcusAddress("123 Main St, Dallas, TX 75201"), {
    street: "123 Main St",
    city: "Dallas",
    state: "TX",
    postalCode: "75201",
  });
  assert.deepEqual(parseMarcusAddress("bad"), {
    street: null,
    city: null,
    state: null,
    postalCode: null,
  });
});

test("extractCssUrl pulls URL from inline style", () => {
  assert.equal(extractCssUrl('background-image: url("https://cdn.example/img.jpg")'), "https://cdn.example/img.jpg");
  assert.equal(extractCssUrl("no url here"), null);
});

test("parseMarcusTileHtml maps tile markup and row fields", () => {
  const tileHtml = `
    <div class="mm-tile" data-dealid="D-99" data-activityid="ACT-1">
      <a href="/properties/sample-deal">
        <h2>Sample Retail Center</h2>
        <h3>Retail</h3>
        <div class="mm-location">Austin, TX 78701</div>
        <div class="mm-listing-price">Listing Price: $3,500,000</div>
        <div class="mm-cap-rate">6.25%</div>
        <img src="https://mmimageservice.azurewebsites.net/api/image/property/1.jpg" />
      </a>
    </div>
  `;
  const listing = parseMarcusTileHtml(tileHtml, {
    DealId: "D-99",
    PropertyName: "Row Name Override",
    ListingPrice: "$3,400,000",
    PropertyType: "Retail",
    City: "Austin",
    StateProvince: "TX",
    PostalCode: "78701",
    Latitude: 30.27,
    Longitude: -97.74,
    PropertyUrl: "/properties/sample-deal",
  });
  assert.equal(listing.id, "D-99");
  assert.equal(listing.activityId, "ACT-1");
  assert.equal(listing.name, "Row Name Override");
  assert.equal(listing.city, "Austin");
  assert.equal(listing.state, "TX");
  assert.equal(listing.salePriceUsd, 3500000);
  assert.equal(listing.capRatePct, 6.25);
  assert.ok(listing.photos?.[0]?.includes("mmimageservice"));
  assert.equal(listing.url, "https://www.marcusmillichap.com/properties/sample-deal");
});
