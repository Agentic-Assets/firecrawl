import { strict as assert } from "node:assert";
import { readFileSync } from "node:fs";
import { test } from "node:test";
import { extractNewmarkPage } from "../../../sources/pure/newmark-page-detail.js";
const url = "https://www.nmrk.com/properties/example";
const claim = {externalId:"example",url};
const witness = {recordId:"source-1",slug:"example",sourceUrl:url,address:"10 Main Street",city:"Example",state:"Florida",zip:"12345"};
// Sanitized structure from the retained official page; all values are synthetic.
const html = readFileSync(new URL("../../fixtures/newmark-property-detail-sanitized.html", import.meta.url), "utf8").replaceAll("{{URL}}", url);
test("exact source-bound single-address detail preserves prose and contacts",()=>{
 const out=extractNewmarkPage(claim,witness,html,200);
 assert.equal(out?.prose,"Property prose from source."); assert.equal(out?.contacts.length,2);
});
test("holds stale or duplicate identity and ambiguous unit",()=>{
 for (const [item,w,page,status] of [
  [claim,{...witness,address:"11 Main Street"},html,200],
  [claim,{...witness,address:"10 Main Street Suite 2"},html,200],
  [claim,witness,html.replace("<main>","<link rel=\"canonical\" href=\""+url+"\"><main>"),200],
  [claim,witness,html.replace("</main>","<div class=\"property-detail\"></div></main>"),200],
  [{...claim,externalId:"other"},witness,html,200],
  [claim,witness,html,403],
  [claim,witness,html.replace("10 Main Street</h1>","10 Main Street Suite 2</h1>"),200],
  [claim,witness,html.replace("Example, Florida 12345","Other, Florida 12345"),200],
  [claim,{...witness,sourceUrl:"https://evil.example/properties/example"},html,200],
  [claim,witness,html.replace("10 Main Street</h1>","10-Main Street</h1>"),200],
  [claim,witness,html.replace("</main>","<div class=\"property-detail\"></div></main>"),200],
 ] as const) assert.equal(extractNewmarkPage(item,w,page,status),null);
});

const fullUrl = "https://www.nmrk.com/properties/203-n-wisconsin-street-port-washington-sale";
const fullClaim = { externalId: "203-n-wisconsin-street-port-washington-sale", url: fullUrl };
const fullWitness = {
  recordId: "synthetic-record", slug: fullClaim.externalId, sourceUrl: fullUrl,
  address: "203 N Wisconsin Street", city: "Port Washington", state: "Wisconsin", zip: "53074",
};
const fullHtml = readFileSync(new URL("../../fixtures/newmark-property-detail-full-address.html", import.meta.url), "utf8");

test("accepts exact full street address when the H1 is a marketing title", () => {
  assert.equal(extractNewmarkPage(fullClaim, fullWitness, fullHtml, 200)?.prose, "Synthetic property description.");
  const normalized = fullHtml.replace("203 N Wisconsin Street, Port Washington, Wisconsin 53074",
    "203  n WISCONSIN Street,   Port Washington, WISCONSIN 53074");
  assert.equal(extractNewmarkPage(fullClaim, fullWitness, normalized, 200)?.prose, "Synthetic property description.");
});

test("full address layout refuses each mismatched witness component and ambiguous page", () => {
  for (const changed of [
    { address: "205 N Wisconsin Street" }, { city: "Milwaukee" },
    { state: "Minnesota" }, { zip: "53075" }, { address: "203 N Wisconsin Street Suite 2" },
  ]) assert.equal(extractNewmarkPage(fullClaim, { ...fullWitness, ...changed }, fullHtml, 200), null);
  for (const page of [
    fullHtml.replace("203 N Wisconsin Street,", "205 N Wisconsin Street,"),
    fullHtml.replace("Port Washington,", "Milwaukee,"),
    fullHtml.replace("Wisconsin 53074", "Minnesota 53074"),
    fullHtml.replace("53074", "53075"),
    fullHtml.replace("203 N Wisconsin Street,", "203 N Wisconsin Street Suite 2,"),
    fullHtml.replace("Historic Poole Property</h1>", " </h1>"),
    fullHtml.replace("</main>", '<div class="property-detail"></div></main>'),
    fullHtml.replace("</h1>", '</h1><p class="street-address">duplicate</p>'),
    fullHtml.replace('rel="canonical" href="' + fullUrl, 'rel="canonical" href="https://www.nmrk.com/properties/unrelated'),
    fullHtml.replace('property="og:url" content="' + fullUrl, 'property="og:url" content="https://www.nmrk.com/properties/unrelated'),
  ]) assert.equal(extractNewmarkPage(fullClaim, fullWitness, page, 200), null);
  assert.equal(extractNewmarkPage({ ...fullClaim, externalId: "unrelated" }, fullWitness, fullHtml, 200), null);
  assert.equal(extractNewmarkPage(fullClaim, fullWitness, fullHtml, 403), null);
});
