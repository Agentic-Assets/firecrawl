import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";
import type { ScrapedDoc } from "../../../types.js";
import { assertAvisonYoungDetailDoc } from "../../../sources/avison-young.js";

const fixturePath = join(
  dirname(fileURLToPath(import.meta.url)),
  "../../fixtures/raw_data/avison-young-sharplaunch-building-detail.html"
);
const rawHtml = readFileSync(fixturePath, "utf8");
const nativeUrl = "https://ayusalmaschoolcorporatecenteriii.sharplaunch.com/";
const base = {
  id: "17304",
  name: "Alma School Corporate Center III",
  street: "1839 S Alma School Road",
  city: "Mesa",
  state: "AZ",
  postalCode: "85210",
  sharpLaunchUrl: "https://ayusalmaschoolcorporatecenteriii.sharplaunch.com",
};

function detail(html = rawHtml, sourceURL = nativeUrl): ScrapedDoc {
  return {
    rawHtml: html,
    markdown: "",
    links: [],
    metadata: { sourceURL, statusCode: 200 },
  };
}

test("Avison recognizes the saved native building layout by feed-bound metadata and visible address", () => {
  assert.doesNotThrow(() =>
    assertAvisonYoungDetailDoc(detail(), nativeUrl, base)
  );
});

test("Avison native building identity permits an empty availability section", () => {
  const noCards = rawHtml.replace(
    /<div class="availabilities-section"><\/div>/,
    '<div class="availabilities-section"><p>No current availabilities</p></div>'
  );
  assert.doesNotThrow(() =>
    assertAvisonYoungDetailDoc(detail(noCards), nativeUrl, base)
  );
});

test("Avison native building identity rejects a conflicting page title or Open Graph title", () => {
  const conflictingTitle = rawHtml.replace(
    "<title>Alma School Corporate Center III</title>",
    "<title>Other Commerce Center</title>"
  );
  const conflictingOgTitle = rawHtml.replace(
    'property="og:title" content="Alma School Corporate Center III"',
    'property="og:title" content="Other Commerce Center"'
  );
  const conflictingOgWithMatchingH1 = conflictingOgTitle.replace(
    "Office suites for lease and sublease",
    "Alma School Corporate Center III"
  );
  for (const html of [
    conflictingTitle,
    conflictingOgTitle,
    conflictingOgWithMatchingH1,
  ]) {
    assert.throws(
      () => assertAvisonYoungDetailDoc(detail(html), nativeUrl, base),
      /identity/
    );
  }
});

test("Avison native building identity rejects a non-owned canonical URL and mismatched visible address", () => {
  const conflictingOgUrl = rawHtml.replace(
    'property="og:url" content="https://ayusalmaschoolcorporatecenteriii.sharplaunch.com/"',
    'property="og:url" content="https://other-property.sharplaunch.com/"'
  );
  const queryMismatchedOgUrl = rawHtml.replace(
    'property="og:url" content="https://ayusalmaschoolcorporatecenteriii.sharplaunch.com/"',
    'property="og:url" content="https://ayusalmaschoolcorporatecenteriii.sharplaunch.com/?unit=1"'
  );
  const conflictingAddress = rawHtml.replace(
    "1839 S Alma School Rd, Mesa, AZ 85210",
    "900 Other Avenue, Tempe, AZ 85281"
  );
  const streetNumberSubstring = rawHtml.replace(
    "1839 S Alma School Rd, Mesa, AZ 85210",
    "11839 S Alma School Rd, Mesa, AZ 85210"
  );
  const citySubstring = rawHtml.replace(
    "1839 S Alma School Rd, Mesa, AZ 85210",
    "1839 S Alma School Rd, Mesa Heights, AZ 85210"
  );
  for (const html of [
    conflictingOgUrl,
    queryMismatchedOgUrl,
    conflictingAddress,
    streetNumberSubstring,
    citySubstring,
  ]) {
    assert.throws(
      () => assertAvisonYoungDetailDoc(detail(html), nativeUrl, base),
      /identity/
    );
  }
});

test("Avison native building identity requires a visible property identity heading and substantive facts", () => {
  const noIdentityHeading = rawHtml.replace(
    /<h2 class="hero__sub-title">.*?<\/h2>/,
    ""
  );
  const noFacts = rawHtml.replace(/<li>[\s\S]*?<\/li>/g, "");
  for (const html of [noIdentityHeading, noFacts]) {
    assert.throws(
      () => assertAvisonYoungDetailDoc(detail(html), nativeUrl, base),
      /structure|identity/
    );
  }
  for (const incompleteBase of [
    { ...base, street: "" },
    { ...base, city: "" },
    { ...base, state: "" },
  ]) {
    assert.throws(
      () => assertAvisonYoungDetailDoc(detail(), nativeUrl, incompleteBase),
      /identity/
    );
  }
});

