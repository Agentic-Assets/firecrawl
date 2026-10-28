import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../../../", import.meta.url));
const host = "https://www.foundrycommercial.com";

function run(notes: string[][], stdout: boolean, options: { duplicateAcceptedIds?: boolean; existing?: boolean } = {}) {
  const directory = mkdtempSync(join(tmpdir(), "collect-retained-"));
  try {
    const responses: Array<[string, string]> = [];
    const links = notes.map((_, index) => `${host}/property/synthetic-${index}/`);
    responses.push([`${host}/sitemap.xml`, `<sitemapindex><sitemap><loc>${host}/property-sitemap.xml</loc></sitemap></sitemapindex>`]);
    responses.push([`${host}/property-sitemap.xml`, `<urlset>${links.map((url) => `<url><loc>${url}</loc></url>`).join("")}</urlset>`]);
    for (const [index, url] of links.entries()) {
      const id = String(options.duplicateAcceptedIds && index > 0 ? 81001 : 81000 + index);
      const json = JSON.stringify({ "@type": "RealEstateListing", url, name: `Synthetic ${index}`,
        mainEntity: { additionalType: "Office", floorSize: { value: 4000 } } });
      responses.push([url, `<html><head><link rel="canonical" href="${url}"><link rel="shortlink" href="${host}/?p=${id}"><script type="application/ld+json">${json}</script></head><body class="postid-${id}"><h1>Synthetic ${index}</h1><ul class="property-notes">${notes[index]!.map((note) => `<li>${note}</li>`).join("")}</ul></body></html>`]);
    }
    const preload = join(directory, "preload.mjs");
    writeFileSync(preload, `
      import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
      const deny = () => { throw new Error("NETWORK_DENIED_BY_FIXTURE"); };
      net.Socket.prototype.connect = deny; tls.connect = deny; http.request = deny; https.request = deny;
      const responses = new Map(${JSON.stringify(responses)});
      globalThis.fetch = async (input) => {
        const url = String(input); if (!responses.has(url)) throw new Error("UNEXPECTED_FIXTURE_URL");
        return new Response(responses.get(url), { headers: { "content-type": url.endsWith(".xml") ? "application/xml" : "text/html" } });
      };
    `);
    const out = join(directory, "artifact.json");
    if (options.existing) writeFileSync(out, '{"marker":"preserve"}', { mode: 0o600 });
    const args = ["--import", "tsx", "--import", pathToFileURL(preload).href, "collect.ts",
      "--source=foundry-commercial", "--transaction=sale", ...(stdout ? [] : [`--out=${out}`])];
    const child = spawnSync(process.execPath, args, { cwd: root, encoding: "utf8", timeout: 30_000,
      maxBuffer: 4 * 1024 * 1024 });
    assert.ifError(child.error);
    assert.doesNotMatch(child.stderr, /NETWORK_DENIED_BY_FIXTURE|UNEXPECTED_FIXTURE_URL/);
    const body = stdout ? child.stdout : existsSync(out) ? readFileSync(out, "utf8") : "";
    return { status: child.status, stderr: child.stderr, artifact: body ? JSON.parse(body) : null,
      outputMode: existsSync(out) ? statSync(out).mode & 0o777 : null };
  } finally {
    rmSync(directory, { recursive: true, force: true });
  }
}

for (const stdout of [false, true]) {
  test(`all-held source writes evidence-only artifact and fails (${stdout ? "stdout" : "file"})`, () => {
    const result = run([[], ["Paused", "Office"]], stdout);
    assert.notEqual(result.status, 0);
    assert.match(result.stderr, /no listings collected from any source/);
    assert.doesNotMatch(result.stderr, /done: 0 listings/);
    assert.equal(result.artifact.runMeta.evidenceOnly, true);
    assert.equal(result.artifact.runMeta.evidencePurpose, "private_unqualified_source_observations");
    assert.deepEqual(result.artifact.listings, []);
    assert.equal(result.artifact.totalListings, 0);
    if (!stdout) assert.equal(result.outputMode, 0o600);
    const retained = result.artifact.sources[0].retainedObservations;
    assert.equal(retained.length, 2);
    assert.equal(retained[0].observation.status, null);
    assert.deepEqual(retained[0].observation.propertyNotes, []);
    assert.deepEqual(retained[1].observation.propertyNotes, ["Paused", "Office"]);
  });
}

test("mixed accepted and held rows retain source observations without evidence-only mode", () => {
  const result = run([["For Sale"], ["Paused", "Office"]], false);
  assert.equal(result.status, 0);
  assert.equal(result.artifact.listings.length, 1);
  assert.equal(result.artifact.sources[0].retainedObservations.length, 1);
  assert.equal("evidenceOnly" in result.artifact.runMeta, false);
  assert.equal("retainedObservations" in result.artifact.listings[0], false);
});

test("fatal duplicate accepted IDs retain held and conflicting observations without accepting listings", () => {
  const result = run([["Paused"], ["For Sale"], ["For Sale"]], false, { duplicateAcceptedIds: true });
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /duplicate provider ID/);
  assert.equal(result.artifact.runMeta.evidenceOnly, true);
  assert.deepEqual(result.artifact.listings, []);
  const source = result.artifact.sources[0];
  assert.match(source.error, /duplicate provider ID/);
  assert.equal(source.retainedObservations.length, 3);
  assert.ok(source.retainedObservations.some((item: any) => item.observation?.propertyNotes?.includes("Paused")));
  assert.equal(source.retainedObservations.filter((item: any) => item.observation?.foundryPostId === "81001").length, 2);
});

test("evidence-only output refuses to overwrite an existing artifact", () => {
  const result = run([["Paused"]], false, { existing: true });
  assert.notEqual(result.status, 0);
  assert.deepEqual(result.artifact, { marker: "preserve" });
});

test("zero listings without held observations does not write an artifact", () => {
  const result = run([["Closed"]], false);
  assert.notEqual(result.status, 0);
  assert.match(result.stderr, /no listings collected from any source/);
  assert.equal(result.artifact, null);
});
