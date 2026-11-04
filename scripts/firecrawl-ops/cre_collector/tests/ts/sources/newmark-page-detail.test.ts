import { strict as assert } from "node:assert";
import { test } from "node:test";
import { extractNewmarkPage } from "../../../sources/pure/newmark-page-detail.js";
const url = "https://www.nmrk.com/properties/example";
const claim = {externalId:"example",url};
const witness = {recordId:"source-1",slug:"example",sourceUrl:url,address:"10 Main Street",city:"Example",state:"Florida",zip:"12345"};
const html = `<link rel="canonical" href="${url}"><meta property="og:url" content="${url}">
<main><div class="property-detail"><div class="property-detail__title"><h1>10 Main Street</h1>
<p class="street-address">Example, Florida 12345</p></div><div class="property-detail__prose"><p>Observed property text.</p></div>
<div class="property-detail__contact"><span class="property-detail__contact-name">Example Broker</span><a href="mailto:broker@example.test">Email</a></div>
</div></main>`;
test("exact source-bound single-address detail preserves prose and contacts",()=>{
 const out=extractNewmarkPage(claim,witness,html,200);
 assert.equal(out?.prose,"Observed property text."); assert.equal(out?.contacts.length,1);
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
