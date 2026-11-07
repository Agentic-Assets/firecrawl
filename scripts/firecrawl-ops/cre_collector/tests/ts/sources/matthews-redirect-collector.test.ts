import assert from "node:assert/strict";
import test from "node:test";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";

const root = fileURLToPath(new URL("../../../", import.meta.url));
test("collector keeps strict redirect peers private and fails without listings", () => {
  const dir = mkdtempSync(join(tmpdir(), "matthews-redirect-"));
  try {
    const preload = join(dir, "preload.mjs"), out = join(dir, "artifact.json");
    writeFileSync(preload, `
      import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
      const deny = () => { throw new Error("NETWORK_DENIED_BY_FIXTURE"); };
      net.Socket.prototype.connect = deny; tls.connect = deny; http.request = deny; https.request = deny;
      await import(${JSON.stringify(pathToFileURL(join(root, "sources/matthews-transport.ts")).href)});
      const host = "https://www.matthews.com";
      const urls = [host+"/properties/redirected", host+"/properties/good-peer"];
      globalThis.fetch = async input => { const url=String(input);
        if(url === host+"/sitemap.xml") return new Response("<urlset>"+urls.map(u=>"<url><loc>"+u+"</loc></url>").join("")+"</urlset>");
        if(url === urls[0]) return new Response(null,{status:307,headers:{location:"/listings"}});
        if(url === urls[1]) return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><section><h1 id="propertyTitle">Good peer</h1><div id="propertyPrice">Contact broker</div><div class="prose">Peer</div></section></body></html>');
        throw new Error("UNEXPECTED_FIXTURE_URL");
      };
    `);
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href,
      "collect.ts", "--source=matthews", "--transaction=sale", `--out=${out}`],
      { cwd: root, env: { ...process.env, CRE_REQUIRE_FRESH_DETAILS: "1" }, encoding: "utf8", timeout: 30_000 });
    assert.ifError(child.error); assert.notEqual(child.status, 0);
    assert.doesNotMatch(child.stderr, /NETWORK_DENIED_BY_FIXTURE|UNEXPECTED_FIXTURE_URL/);
    const artifact = JSON.parse(readFileSync(out, "utf8"));
    assert.equal(statSync(out).mode & 0o777, 0o600);
    assert.equal(artifact.runMeta.evidenceOnly, true); assert.deepEqual(artifact.listings, []);
    const observations = artifact.sources[0].retainedObservations;
    assert.ok(observations.some((x: any) => x.outcome === "held_temporary_redirect" && x.httpStatus === 307));
    assert.ok(observations.some((x: any) => x.schema === "matthews-completed-parse-observation/v1"));
    assert.match(artifact.sources[0].error, /partial.*temporary redirect/);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
