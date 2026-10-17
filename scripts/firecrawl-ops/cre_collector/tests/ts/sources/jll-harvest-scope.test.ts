process.argv = [process.argv[0]!, process.argv[1]!];
import test from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { enrichJllListing, jllGraphqlItemToListing } from "../../../sources/jll.js";
import { jllPropertyHarvestDoc } from "../../../sources/jll-harvest-scope.js";
import { harvestDetail } from "../../../lib/harvest.js";
import { firecrawl } from "../../../lib/scrape.js";

const url = "https://property.jll.com/listings/target-property";
const target = "https://assets.example/target.jpg";
const related = "https://assets.example/other.jpg";
const other = "https://property.jll.com/listings/another-property";
const tile = "https://maps.googleapis.com/maps/vt?pb=tile";
const carousel = `<div><h2><span>Related properties</span></h2><div><div class="embla"><div class="embla-container"><div class="embla-slide"><a href="${other}"><img src="${related}"></a></div></div></div></div></div>`;

async function enrich(t: test.TestContext, body: string, images: string[], links: string[], native = [target], cardPhotos: string[] | null = null) {
  const cache = mkdtempSync(join(tmpdir(), "jll-scope-"));
  const prior = process.env.JLL_DETAIL_CACHE_DIR;
  process.env.JLL_DETAIL_CACHE_DIR = cache;
  t.after(() => { if (prior === undefined) delete process.env.JLL_DETAIL_CACHE_DIR; else process.env.JLL_DETAIL_CACHE_DIR = prior; rmSync(cache, { recursive: true, force: true }); });
  t.mock.method(globalThis, "fetch", async () => { throw new Error("offline fixture attempted network"); });
  const property = { id: "target", pageUrl: "/listings/target-property", images: native, brokers: [] };
  const rawHtml = `<script id="__NEXT_DATA__">${JSON.stringify({props:{pageProps:{property,brokers:[]}}})}</script>${body}`;
  const doc = {rawHtml, markdown:"Airport is 7.2 miles away. Full useful context.", images, links, attributes:[{selector:"a",attribute:"href",values:links}]};
  t.mock.method(firecrawl, "scrape", async () => doc);
  const base = cardPhotos === null ? {id:"target",url,transactionType:"Lease"}
    : jllGraphqlItemToListing({id:"target",pageUrl:url,title:"Target",images:cardPhotos},"lease","office",1,1);
  const row = await enrichJllListing(base);
  assert.equal(row.detailError, undefined);
  return {row,doc};
}

test("JLL actual enrichment excludes recommendation children and tile images without changing observations", async t => {
  const {row,doc} = await enrich(t, `<img src="${target}"><img src="${tile}">${carousel}`, [target,related,tile], [other]);
  assert.deepEqual(row.photos, [target]);
  assert(!row.links?.some((link:any) => link.url === other));
  assert.equal(doc.markdown, "Airport is 7.2 miles away. Full useful context.");
  assert.equal(row.jllDetail.scrape.linkCount, 1);
  assert.equal(row.jllDetail.scrape.rawHtmlLength, doc.rawHtml.length);
});

function project(body: string, images: string[] = [], links: string[] = [], native: string[] = []) {
  const doc = {rawHtml: body, markdown:"Airport is 7.2 miles away.", images, links,
    attributes:[{selector:"a",attribute:"href",values:links}]};
  const original = structuredClone(doc);
  const scoped = jllPropertyHarvestDoc(doc,url,native);
  assert.deepEqual(doc, original);
  return {...scoped, harvested:harvestDetail(scoped.doc,{baseUrl:url})};
}

test("JLL preserves supplemental target images and shared native/retained resources", () => {
  const extra="https://assets.example/supplement.jpg";
  const doc="https://assets.example/plan.pdf";
  const video="https://www.youtube.com/watch?v=abc12345678";
  const body=`<img src="${extra}"><a href="${doc}">Plan</a><iframe src="${video}"></iframe>${carousel}`;
  const scoped=project(body,[related,extra],[other,doc,video],[related]);
  assert(scoped.harvested.images.includes(related));
  assert(scoped.harvested.images.includes(extra));
  assert(scoped.harvested.documents.some(x=>x.url===doc));
  assert(scoped.harvested.media.some(x=>x.url===video));
  const retained=project(`<a href="${other}">Explicit target cross reference</a>${carousel}`,[],[other]);
  assert(retained.harvested.links.some(x=>x.url===other));
});

test("JLL exact URL scoping retains a similar but different target asset", () => {
  const extra=related+"?version=target";
  const scoped=project(`<img src="${extra}">${carousel}`,[related,extra],[other]);
  assert.deepEqual(scoped.harvested.images,[extra]);
  assert.equal(scoped.scope,"recommendations_scoped");
});

test("JLL parses complete comma-bearing srcset candidates and drops only proven fragments", () => {
  const image="https://res.cloudinary.com/example/image/upload/f_avif,c_fill,w_32/gallery.jpg";
  const fragment="https://property.jll.com/listings/c_fill";
  const scoped=project(`<img srcset="${image} 32w, ${image.replace('w_32','w_64')} 64w">`,[fragment]);
  assert.deepEqual(scoped.harvested.images,[image,image.replace('w_32','w_64')]);
  const genuine=project(`<img src="${fragment}" srcset="${image} 32w">`,[fragment]);
  assert(genuine.harvested.images.includes(fragment));
});

for (const [name,body] of [
  ["no identified section",`<img src="${related}">`],
  ["ambiguous headings",carousel+carousel],
  ["foreign recommendation route",carousel.replace(other,"https://elsewhere.example/listings/other")],
  ["current property in carousel",carousel.replace(other,url)],
  ["unsupported carousel structure",carousel.replaceAll("embla", "unknown")],
  ["empty HTML",""],
]) {
  test(`JLL unknown scope retains legacy assets: ${name}`, () => {
    const scoped=project(body,[related],[other]);
    assert.equal(scoped.scope,"unknown");
    assert(scoped.harvested.images.includes(related));
    assert(scoped.harvested.links.some(x=>x.url===other));
  });
}

test("JLL map tiles leave gallery but map links and useful context survive", () => {
  const scoped=project(`<img src="${tile}"><a href="${tile}">Map</a><p>Airport is 7.2 miles away.</p>`,[tile],[tile]);
  assert.deepEqual(scoped.harvested.images,[]);
  assert(scoped.harvested.links.some(x=>x.url===tile));
  assert.match(scoped.doc.rawHtml,/Airport is 7.2 miles away/);
  assert.equal(scoped.doc.markdown,"Airport is 7.2 miles away.");
});

test("JLL ambiguous srcset never invents exclusion authority", () => {
  const fragment="https://property.jll.com/listings/c_fill";
  const scoped=project('<img srcset="https://res.cloudinary.com/a,f_avif/x.jpg unsupported">',[fragment]);
  assert(scoped.harvested.images.includes(fragment));
});

for (const cardPhotos of [[],[target]]) {
  test(`JLL empty detail-native gallery retains real target DOM supplements with ${cardPhotos.length}-photo card`, async t => {
    const extra="https://assets.example/target-extra.jpg";
    const {row}=await enrich(t,`<img src="${target}"><img src="${extra}">${carousel}`,
      [target,extra,related],[other],[],cardPhotos);
    assert.deepEqual(row.photos,[target,extra]);
    assert(!row.links?.some((link:any)=>link.url===other));
  });
}

test("JLL cached/no-attributes fallback cannot reintroduce removed descendants",()=>{
  const doc={rawHtml:`<img src="${target}">${carousel}`,markdown:"Full target context",links:[other]};
  const scoped=jllPropertyHarvestDoc(doc,url,[target]);
  const result=harvestDetail(scoped.doc,{baseUrl:url});
  assert.deepEqual(result.images,[target]);
  assert(!result.links.some(x=>x.url===other));
  assert.equal(doc.rawHtml.includes(related),true);
});


test("JLL portrait and consent roles do not become gallery images; contact/raw data remain",()=>{
  const avatar="https://assets.example/person.jpg";
  const logo="https://assets.example/company.png";
  const rawHtml=`<div data-slot="button"><img alt="Known Broker" src="${avatar}"><span>Broker details</span></div><div id="onetrust-consent-sdk"><img src="${logo}"></div>`;
  const original={rawHtml,markdown:"Known Broker details",images:[avatar,logo],links:[avatar],attributes:[{selector:"a",attribute:"href",values:[avatar]}]};
  const scoped=jllPropertyHarvestDoc(original,url,[],["Known Broker"]);
  assert.deepEqual(harvestDetail(scoped.doc,{baseUrl:url}).images,[]);
  assert.equal(original.rawHtml,rawHtml);
  const native=jllPropertyHarvestDoc(original,url,[avatar],["Known Broker"]);
  assert(harvestDetail(native.doc,{baseUrl:url}).images.includes(avatar));
  const shared=jllPropertyHarvestDoc({...original,rawHtml:rawHtml+`<img src="${avatar}" alt="Target building">`},url,[],["Known Broker"]);
  assert(harvestDetail(shared.doc,{baseUrl:url}).images.includes(avatar));
  const unknown=jllPropertyHarvestDoc(original,url,[]);
  assert(harvestDetail(unknown.doc,{baseUrl:url}).images.includes(avatar));
});

test("JLL two candidate carousels are unknown and retain legacy children",()=>{
  const body=carousel.replace('</div></div></div></div>','</div></div></div><div class="embla"><a href="'+other+'">Other</a></div></div>');
  const scoped=project(body,[related],[other]);
  assert.equal(scoped.scope,"unknown");
  assert(scoped.harvested.images.includes(related));
});


test("JLL missing contact name cannot authorize portrait exclusion",()=>{
  const avatar="https://assets.example/unknown-person.jpg";
  const original={rawHtml:`<div data-slot="button"><img src="${avatar}"><span>Broker details</span></div>`,markdown:"Property",images:[avatar],links:[]};
  const scoped=jllPropertyHarvestDoc(original,url,[],[""]);
  assert(harvestDetail(scoped.doc,{baseUrl:url}).images.includes(avatar));
});


for (const [role,markup] of [
  ["document cover background",`<a href="https://assets.example/target-plan.pdf"><div style='background-image: url("${related}")'></div></a>`],
  ["lazy target image",`<img data-lazy-src="${related}">`],
]) {
  test(`JLL retained ${role} wins over the same recommendation image`,()=>{
    const scoped=project(markup+carousel,[related],[other]);
    assert(scoped.harvested.images.includes(related));
    assert(!scoped.harvested.links.some(x=>x.url===other));
  });
}
