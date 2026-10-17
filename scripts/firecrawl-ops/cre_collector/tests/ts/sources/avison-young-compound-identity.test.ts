import test from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import * as cheerio from "cheerio";
import { assertAvisonYoungDetailDoc, enrichAvisonYoungListing } from "../../../sources/avison-young.js";
import type { ScrapedDoc } from "../../../types.js";
const cases = [
 {key:"offering",name:"3555 N Highway 67",street:"3555 N Highway 67",city:"Example City",state:"MO",postalCode:"63034"},
 {key:"range",name:"11418 Moog Drive",street:"11418-11446 Moog Drive",city:"Example Heights",state:"MO",postalCode:"63146"},
].map(row=>({...row,sharpLaunchUrl:`https://ayusfixture${row.key}.sharplaunch.com`,html:readFileSync(new URL(`../../fixtures/raw_data/avison-young-compound-${row.key}.html`,import.meta.url),"utf8")}));
function check(html:string,base:any) { const doc:ScrapedDoc={rawHtml:html,markdown:"",links:[],metadata:{statusCode:200,sourceURL:base.sharpLaunchUrl}};return assertAvisonYoungDetailDoc(doc,base.sharpLaunchUrl,base); }
for (const row of cases) {
 test(`Avison compound ${row.key} exact observed identity accepts without changing incoming fields`,()=>{const before=structuredClone(row);assert.doesNotThrow(()=>check(row.html,row));assert.deepEqual(row,before);});
 for(const mutation of ["wrong-h1","unknown-offering","address-h1","duplicate-h1","wrong-name","wrong-street","wrong-city","wrong-state","wrong-zip","title","og-title","og-url","canonical","duplicate-title","duplicate-og","missing-hero","duplicate-hero","conflict-h2","description-conflict","challenge"]){
  test(`Avison compound ${row.key} ${mutation === "description-conflict" && row.key === "offering" ? "retains advisory" : "refuses"} ${mutation}`,()=>{const $=cheerio.load(row.html);const base={...row};
   if(mutation==="wrong-h1")$("h1").text("Different Property");
   if(mutation==="unknown-offering")$("h1").text("Unrecognized Property for Sale");
   if(mutation==="address-h1")$("h1").text("99999 Other Road");
   if(mutation==="duplicate-h1")$("body").append($("h1").clone());
   if(mutation==="wrong-name")base.name="Different Property";
   if(mutation==="wrong-street")base.street=row.key==="range"?"11418-11447 Moog Drive":"3556 N Highway 67";
   if(mutation==="wrong-city")base.city="Other City";
   if(mutation==="wrong-state")base.state="TX";
   if(mutation==="wrong-zip")$(".hero__sub-title").append(" 99999");
   if(mutation==="title")$("title").text("Different Property");
   if(mutation==="og-title")$('meta[property="og:title"]').attr("content","Different Property");
   if(mutation==="og-url")$('meta[property="og:url"]').attr("content","https://other.sharplaunch.com/");
   if(mutation==="canonical")$("head").append('<link rel="canonical" href="https://other.sharplaunch.com/">');
   if(mutation==="duplicate-title")$("head").append($("title").clone());
   if(mutation==="duplicate-og")$("head").append($('meta[property="og:title"]').clone());
   if(mutation==="missing-hero")$(".hero__sub-title").remove();
   if(mutation==="duplicate-hero")$("body").append($(".hero__sub-title").clone());
   if(mutation==="conflict-h2")$("body").append(`<h2>${row.name}, Other City, TX</h2>`);
   if(mutation==="description-conflict")$('meta[property="og:description"]').attr("content","Other Property, 999 Other Road, Other City, TX 99999");
   if(mutation==="duplicate-canonical")$("head").append(`<link rel="canonical" href="${row.sharpLaunchUrl}/"><link rel="canonical" href="${row.sharpLaunchUrl}/">`);
   if(mutation==="challenge")$("body").append("Checking your browser");
   if (mutation === "description-conflict" && row.key === "offering") {
    // Complete visible identity is independently bound; SEO is source text, not street authority.
    assert.doesNotThrow(() => check($.html(), base));
   } else assert.throws(()=>check($.html(),base),/identity|challenge|structure/);
  });
 }
}
const range=cases[1]!;
for(const mutation of ["endpoint-only","no-meta","missing-visible","meta-name","meta-city","meta-state","meta-zip","meta-nearby","meta-duplicate","meta-conflict","meta-empty","meta-unqualified-range"]){
 test(`Avison range compound refuses ${mutation}`,()=>{const $=cheerio.load(range.html);const selector='meta[name="description"],meta[name="twitter:description"],meta[property="og:description"]';
  if(mutation==="endpoint-only")$(selector).each((_,e)=>{ $(e).attr("content",$(e).attr("content")!.replace("11418-11446 Moog Drive","11418 Moog Drive")); });
  if(mutation==="no-meta")$(selector).remove();
  if(mutation==="missing-visible")$(".hero__sub-title").text("Example Heights, Missouri");
  if(mutation==="meta-name")$(selector).each((_,e)=>{ $(e).attr("content",$(e).attr("content")!.replace("11418 Moog Drive,","Other Property,")); });
  if(mutation==="meta-city")$(selector).each((_,e)=>{ $(e).attr("content",$(e).attr("content")!.replace("Example Heights","Other City")); });
  if(mutation==="meta-state")$(selector).each((_,e)=>{ $(e).attr("content",$(e).attr("content")!.replace("MO 63146","TX 63146")); });
  if(mutation==="meta-zip")$(selector).each((_,e)=>{ $(e).attr("content",$(e).attr("content")!.replace("63146","99999")); });
  if(mutation==="meta-nearby")$(selector).each((_,e)=>{ $(e).attr("content","Nearby: "+$(e).attr("content")); });
  if(mutation==="meta-duplicate")$("head").append($('meta[name="description"]').clone());
  if(mutation==="meta-conflict")$('meta[property="og:description"]').attr("content","Another Property, 999 Other Road, Other City, TX 99999");
  if(mutation==="meta-empty")$('meta[property="og:description"]').attr("content","");
  if(mutation==="meta-unqualified-range")$(selector).each((_,e)=>{ $(e).attr("content",$(e).attr("content")!+" Nearby shops"); });
  assert.throws(()=>check($.html(),range),/identity|structure/);
 });
}

test("Avison complete visible offering address does not invent an absent feed ZIP or require description metadata",()=>{
 const row=cases[0]!;assert.doesNotThrow(()=>check(row.html,{...row,postalCode:undefined}));
 const $=cheerio.load(row.html);$('meta[name="description"],meta[name="twitter:description"],meta[property="og:description"]').remove();
 assert.doesNotThrow(()=>check($.html(),row));
});

for(const heading of ["Office for Lease","Industrial for Sale","Retail for Lease","Special Purpose for Sale"]){
 test(`Avison complete-address witness supports typed offering role ${heading} (synthetic)`,()=>{
  const row=cases[0]!;const $=cheerio.load(row.html);$("h1").text(heading);
  assert.doesNotThrow(()=>check($.html(),row));
 });
}

test("Avison range compound cannot drop or change a unit token in the full street",()=>{
 const row=cases[1]!;assert.throws(()=>check(row.html,{...row,street:row.street+" Unit 2"}),/identity/);
});

for (const row of cases) {
 for (const mutation of ["name", "street", "city", "state", "zip", "url", "duplicate", "duplicate-conflict"]) {
  test(`Avison compound ${row.key} rejects conflicting/ambiguous JSON-LD ${mutation}`, () => {
   const $ = cheerio.load(row.html);
   const record = { "@type": "RealEstateListing", name: row.name, url: row.sharpLaunchUrl,
    address: { streetAddress: row.street, addressLocality: row.city, addressRegion: row.state, postalCode: row.postalCode } };
   if (mutation === "name") record.name = "Unrelated Plaza";
   if (mutation === "street") record.address.streetAddress = "999 Other Road";
   if (mutation === "city") record.address.addressLocality = "Other City";
   if (mutation === "state") record.address.addressRegion = "TX";
   if (mutation === "zip") record.address.postalCode = "99999";
   if (mutation === "url") record.url = "https://other.sharplaunch.com/";
   $("head").append(`<script type="application/ld+json">${JSON.stringify(record)}</script>`);
   if (mutation.startsWith("duplicate")) $("head").append(`<script type="application/ld+json">${JSON.stringify({ ...record, name: mutation === "duplicate" ? row.name : "Unrelated Plaza" })}</script>`);
   assert.throws(() => check($.html(), row), /identity/);
  });
 }
 test(`Avison compound ${row.key} rejects an unrelated structured record before enrichment promotion`, async () => {
  const $ = cheerio.load(row.html);
  $("head").append(`<script type="application/ld+json">${JSON.stringify({ "@type": "RealEstateListing", url: row.sharpLaunchUrl, name: "Unrelated Plaza", description: "Other property", datePosted: "2026-10-17" })}</script>`);
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  try {
   const enriched = await enrichAvisonYoungListing(row, false, async () => ({ rawHtml: $.html(), markdown: "", links: [], metadata: { statusCode: 200, sourceURL: row.sharpLaunchUrl } }));
   assert.equal(enriched.name, row.name);
   assert.match(enriched.detailError, /identity/);
  } finally {
   if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
   else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous;
  }
 });
 test(`Avison compound ${row.key} retains coherent fresh JSON-LD facts`, async () => {
  const $ = cheerio.load(row.html);
  const record = { "@type": "RealEstateListing", url: row.sharpLaunchUrl, name: row.name, description: "Fresh nonprice property facts", datePosted: "2026-10-17", address: { streetAddress: row.street, addressLocality: row.city, addressRegion: row.state, postalCode: row.postalCode } };
  $("head").append(`<script type="application/ld+json">${JSON.stringify(record)}</script>`);
  assert.doesNotThrow(() => check($.html(), row));
  const previous = process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
  process.env.AVISON_YOUNG_DETAIL_TRANSPORT = "direct";
  try {
   const enriched = await enrichAvisonYoungListing(row, false, async () => ({ rawHtml: $.html(), markdown: "", links: [], metadata: { statusCode: 200, sourceURL: row.sharpLaunchUrl } }));
   assert.equal(enriched.name, row.name);
   assert.equal(enriched.description, record.description);
   assert.equal(enriched.lastUpdated, "2026-10-17");
   assert.deepEqual(enriched.detailJsonLd, record);
  } finally {
   if (previous === undefined) delete process.env.AVISON_YOUNG_DETAIL_TRANSPORT;
   else process.env.AVISON_YOUNG_DETAIL_TRANSPORT = previous;
  }
 });
}
for (const row of cases) {
 test(`Avison compound ${row.key} accepts coherent repeated canonical authority`, () => {
  const $ = cheerio.load(row.html);
  $("head").append(`<link rel="canonical" href="${row.sharpLaunchUrl}/"><link rel="canonical" href="${row.sharpLaunchUrl}">`);
  assert.doesNotThrow(() => check($.html(), row));
  for (const extra of ["https://foreign.sharplaunch.com/", row.sharpLaunchUrl + "/other", row.sharpLaunchUrl + "/?other=1", "invalid URL"]) {
   const mixed = cheerio.load($.html()); mixed("head").append(`<link rel="canonical" href="${extra}">`);
   assert.throws(() => check(mixed.html(), row), /identity|structure/);
  }
 });
}
