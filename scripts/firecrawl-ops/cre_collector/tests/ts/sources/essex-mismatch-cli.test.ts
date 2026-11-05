import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

for (const mode of ["file", "stdout", "mixed", "private"] as const) test(`Essex mismatch preserves private evidence: ${mode}`, () => {
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const dir = mkdtempSync(join(tmpdir(), "essex-mismatch-"));
  const output = join(dir, "artifact.json");
  const preload = join(dir, "preload.mjs");
  const host = "https://essexrealtygroup.com/properties/";
  const card = (slug: string, status: string) => `<a href="${host}${slug}/" class="property-card"><div class="property-card__label label">${status}</div><span class="property-info__address--address-1">${slug} Street</span></a>`;
  const current = `<a class="allposts">ALL [2]</a>${card("present", "ON MARKET")}${card("archive-only", "UNDER CONTRACT")}`;
  const closed = `<a class="allposts">ALL [1]</a>${card("closed", "CLOSED")}`;
  const sitemap = `<urlset><url><loc>${host}present/</loc></url><url><loc>${host}closed/</loc></url><url><loc>${host}sitemap-only/</loc></url></urlset>`;
  const responses = [[host, current], [`${host}?sale_deal_status_id=3`, closed], [`${host.replace(/properties\/$/, "")}properties-sitemap.xml`, sitemap]];
  const interra = { success: true, has_next_page: false, posts: [{ ID: 2, post_title: "Healthy Interra", post_content: "<p>Fixture.</p>", permalink: "https://interrarealty.com/listing/property-2/", meta: { listing_status: "available", listing_address: "123 Main St", listing_city: "Chicago, IL 60622", key_details: "0" } }] };
  writeFileSync(preload, `import net from 'node:net'; import tls from 'node:tls'; import http from 'node:http'; import https from 'node:https';
    const deny=()=>{throw Error('NETWORK_DENIED')}; net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
    const original=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>original(fn,ms>=9000&&ms<=10000?1:ms,...args);
    const pages=new Map(${JSON.stringify(responses)}); globalThis.fetch=async input=>{const url=String(input); if(url.includes('interrarealty.com/wp-json/')) return Response.json(${JSON.stringify(interra)}); if(url.includes('franklinst.com/properties/for-')) return new Response('<html>PRIVATE_FRANKLIN</html>',{headers:{'content-type':'text/html'}}); if(url.includes('a234450b432b2b2bebc1ace7e6f692e4489bde70')||url.includes('2f82fcd26667c4b0126d0084938ffa265f05fa4a')) return new Response('{}'); if(!pages.has(url)) throw Error('UNEXPECTED_REQUEST'); return new Response(pages.get(url),{headers:{'content-type':'text/html'}})};`);
  let privatePath: string | undefined;
  try {
    const source = mode === "mixed" ? "essex-realty,interra-realty" : mode === "private" ? "essex-realty,franklin-street" : "essex-realty";
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href, "collect.ts", `--source=${source}`, "--transaction=both", ...(mode === "file" ? [`--out=${output}`] : [])], { cwd: root, encoding: "utf8", timeout: 30000, maxBuffer: 1024 * 1024, env: { ...process.env, BUILDOUT_PAGE_JITTER_MS: "0", CRE_SCRAPE_MAX_ATTEMPTS: "1" } });
    assert.ifError(child.error);
    if (mode === "mixed") assert.equal(child.status, 0, child.stderr);
    else assert.notEqual(child.status, 0);
    assert.doesNotMatch(child.stderr, /NETWORK_DENIED|UNEXPECTED_REQUEST/);
    if (mode === "private") {
      assert.equal(child.stdout, "");
      assert.match(child.stderr, /require a private --out file/);
      return;
    }
    if (mode !== "file") {
      const match = /Essex private evidence artifact: (.+)/.exec(child.stderr);
      assert.ok(match, child.stderr);
      privatePath = match[1].trim();
      const projection = JSON.parse(child.stdout);
      assert.equal(projection.runMeta.evidenceOnly, true);
      assert.doesNotMatch(child.stdout, /archive-only|sitemap-only/);
      if (mode === "mixed") assert.ok(projection.listings.some((row: any) => row.name === "Healthy Interra"));
    }
    const artifact = JSON.parse(readFileSync(privatePath ?? output, "utf8"));
    if (mode !== "mixed") {
      assert.equal(artifact.runMeta.evidenceOnly, true);
      assert.deepEqual(artifact.listings, []);
    } else assert.ok(artifact.listings.some((row: any) => row.name === "Healthy Interra"));
    const sale = artifact.sources.find((entry: any) => entry.transaction === "sale");
    assert.match(sale.error, /reconciliation failed/);
    const observation = sale.retainedObservations[0];
    assert.equal(observation.coverage, "partial_unknown");
    assert.deepEqual(observation.responseText, { current, closed, sitemap });
    assert.deepEqual(observation.archiveOnly, [`${host}archive-only/`]);
    assert.deepEqual(observation.sitemapOnly, [`${host}sitemap-only/`]);
    if (process.platform !== "win32") assert.equal(statSync(privatePath ?? output).mode & 0o077, 0);
  } finally { if (privatePath) rmSync(dirname(privatePath), { recursive: true, force: true }); rmSync(dir, { recursive: true, force: true }); }
});
