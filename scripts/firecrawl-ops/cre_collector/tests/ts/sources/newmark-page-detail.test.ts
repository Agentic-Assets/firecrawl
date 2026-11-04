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
