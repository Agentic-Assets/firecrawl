import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, avisonYoungBaseListing, enrichAvisonYoungListing } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";
const cases = [
 { key: "dual-offer", name: "17998 Commerce Airport Road" },
 { key: "mixed-types", name: "670 Spirit of Commerce Blvd" },
].map(row => ({...row,street:row.name,city:"Example City",state:"MO",postalCode:"63005",sharpLaunchUrl:`https://ayusfixture${row.key}.sharplaunch.com`,html:readFileSync(new URL(`../../fixtures/raw_data/avison-young-primary-${row.key}.html`,import.meta.url),"utf8")}));
function check(html:string,base:any) { const doc:ScrapedDoc={rawHtml:html,markdown:"",links:[],metadata:{statusCode:200,sourceURL:base.sharpLaunchUrl}};return assertAvisonYoungDetailDoc(doc,base.sharpLaunchUrl,base); }
for (const row of cases) {
 test(`Avison complete primary ${row.key} identity accepts optional SEO without address rewriting`,()=>{ const before=structuredClone(row);assert.doesNotThrow(()=>check(row.html,row));assert.deepEqual(row,before); });
 for(const mutation of ["street","city","state","zip","title","og-title","og-url","canonical","name","missing-hero","duplicate-hero","duplicate-title","duplicate-h1","wrong-h1","unknown-offering","contradictory-h2","structured-name","structured-address","structured-duplicate","challenge"]){
  test(`Avison complete primary ${row.key} refuses ${mutation}`,()=>{
   const $=cheerio.load(row.html);const base={...row};
   if(mutation==="street")base.street="999 Other Road";
   if(mutation==="city")base.city="Other City";
   if(mutation==="state")base.state="TX";
   if(mutation==="zip")$(".hero__sub-title").append(" 99999");
   if(mutation==="title")$("title").text("Unrelated Property");
   if(mutation==="og-title")$('meta[property="og:title"]').attr("content","Unrelated Property");
   if(mutation==="og-url")$('meta[property="og:url"]').attr("content","https://other.sharplaunch.com/");
   if(mutation==="canonical")$("head").append('<link rel="canonical" href="https://other.sharplaunch.com/">');
   if(mutation==="name")base.name="Unrelated Property";
   if(mutation==="missing-hero")$(".hero__sub-title").remove();
   if(mutation==="duplicate-hero")$("body").append($(".hero__sub-title").clone());
   if(mutation==="duplicate-title")$("head").append($("title").clone());
   if(mutation==="duplicate-h1")$("body").append($("h1").clone());
   if(mutation==="wrong-h1")$("h1").text("999 Other Road");
   if(mutation==="unknown-offering")$("h1").text("Unknown Thing for Lease");
   if(mutation==="contradictory-h2")$("body").append(`<h2>${row.name}, Other City, TX</h2>`);
   if(mutation.startsWith("structured")){
    const record={"@type":"RealEstateListing",name:mutation==="structured-name"?"Unrelated Property":row.name,url:row.sharpLaunchUrl,address:{streetAddress:mutation==="structured-address"?"999 Other Road":row.street,addressLocality:row.city,addressRegion:row.state}};
    $("head").append(`<script type="application/ld+json">${JSON.stringify(record)}</script>`);
    if(mutation==="structured-duplicate")$("head").append(`<script type="application/ld+json">${JSON.stringify(record)}</script>`);
   }
   if(mutation==="challenge")$("body").append("Checking your browser");
   assert.throws(()=>check($.html(),base),/identity|challenge|structure/);
  });
 }
}

test("Avison complete visible identity retains different SEO source text without rewriting structured street", async () => {
 const row=cases[1]!;
 const seo="For lease - 620 Spirit of Commerce Blvd, Example City, MO 63005";
 const base=avisonYoungBaseListing({id:123,name:row.name,address:row.street,city:row.city,state:row.state,zip:row.postalCode,url:row.sharpLaunchUrl,description:"",meta_description:seo},new Map());
 assert.equal(base.name,row.name);assert.equal(base.street,row.street);
 assert.equal(base.description,seo);assert.equal(base.rawSharpLaunch.meta_description,seo);
 const previous=process.env.AVISON_YOUNG_DETAIL_TRANSPORT;process.env.AVISON_YOUNG_DETAIL_TRANSPORT="direct";
 try {
  const enriched=await enrichAvisonYoungListing(base,false,async()=>({rawHtml:row.html,markdown:"Provider detail",links:[],metadata:{statusCode:200,sourceURL:row.sharpLaunchUrl}}));
  assert.equal(enriched.name,row.name);assert.equal(enriched.street,row.street);
  assert.equal(enriched.description,seo);assert.equal(enriched.rawSharpLaunch.meta_description,seo);
  assert.equal(enriched.detailError,undefined);
 } finally {if(previous===undefined)delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;else process.env.AVISON_YOUNG_DETAIL_TRANSPORT=previous;}
});
for(const heading of ["Office and Industrial for Lease or Sale","Retail / Office for Sale or Lease","Special Purpose & Office for Lease"]){
 test(`Avison bounded offering composition ${heading} is synthetic role coverage`,()=>{const row=cases[0]!;const $=cheerio.load(row.html);$("h1").text(heading);assert.doesNotThrow(()=>check($.html(),row));});
}
