import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

test("Essex mismatch remains a private failed evidence-only artifact", () => {
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
  writeFileSync(preload, `import net from 'node:net'; import tls from 'node:tls'; import http from 'node:http'; import https from 'node:https';
    const deny=()=>{throw Error('NETWORK_DENIED')}; net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
    const original=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>original(fn,ms>=9000&&ms<=10000?1:ms,...args);
    const pages=new Map(${JSON.stringify(responses)}); globalThis.fetch=async input=>{const url=String(input); if(!pages.has(url)) throw Error('UNEXPECTED_REQUEST'); return new Response(pages.get(url),{headers:{'content-type':'text/html'}})};`);
  try {
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href, "collect.ts", "--source=essex-realty", "--transaction=both", `--out=${output}`], { cwd: root, encoding: "utf8", timeout: 30000, maxBuffer: 1024 * 1024 });
    assert.ifError(child.error);
    assert.notEqual(child.status, 0);
    assert.doesNotMatch(child.stderr, /NETWORK_DENIED|UNEXPECTED_REQUEST/);
    const artifact = JSON.parse(readFileSync(output, "utf8"));
    assert.equal(artifact.runMeta.evidenceOnly, true);
    assert.deepEqual(artifact.listings, []);
    const sale = artifact.sources.find((entry: any) => entry.transaction === "sale");
    assert.match(sale.error, /reconciliation failed/);
    const observation = sale.retainedObservations[0];
    assert.equal(observation.coverage, "partial_unknown");
    assert.deepEqual(observation.responseText, { current, closed, sitemap });
    assert.deepEqual(observation.archiveOnly, [`${host}archive-only/`]);
    assert.deepEqual(observation.sitemapOnly, [`${host}sitemap-only/`]);
    if (process.platform !== "win32") assert.equal(statSync(output).mode & 0o077, 0);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
