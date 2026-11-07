import assert from "node:assert/strict";
import test from "node:test";
import { spawn, spawnSync } from "node:child_process";
import { existsSync, mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
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

for (const heldTx of ["sale", "lease"] as const) {
  test(`both transactions reject complete admission when ${heldTx} redirects`, () => {
    const dir = mkdtempSync(join(tmpdir(), "matthews-both-"));
    try {
      const preload = join(dir, "preload.mjs"), out = join(dir, "artifact.json");
      const generationStartedAt = new Date(Date.now() - 10000).toISOString();
      writeFileSync(preload, `
        import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
        const deny=()=>{throw new Error("NETWORK_DENIED_BY_FIXTURE")};
        net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
        await import(${JSON.stringify(pathToFileURL(join(root, "sources/matthews-transport.ts")).href)});
        const host="https://www.matthews.com", sale=host+"/properties/good-sale", lease=host+"/properties/leasing-good-lease";
        globalThis.fetch=async input=>{const url=String(input);
          if(url===host+"/sitemap.xml") return new Response("<urlset><url><loc>"+sale+"</loc></url><url><loc>"+lease+"</loc></url></urlset>");
          if(url===(${JSON.stringify(heldTx)} === "sale" ? sale : lease)) return new Response(null,{status:307,headers:{location:"/listings"}});
          if(url===sale || url===lease) return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><section><h1 id="propertyTitle">Good peer</h1><div id="propertyPrice">Contact broker</div><div class="prose">Peer</div></section></body></html>');
          throw new Error("UNEXPECTED_FIXTURE_URL");
        };
      `);
      const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href,
        "collect.ts", "--source=matthews", "--transaction=both", `--out=${out}`],
        {cwd:root, env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"test-generation",CRE_REFRESH_STARTED_AT:generationStartedAt}, encoding:"utf8",timeout:30000});
      assert.ifError(child.error); assert.doesNotMatch(child.stderr,/NETWORK_DENIED_BY_FIXTURE|UNEXPECTED_FIXTURE_URL/);
      const artifact=JSON.parse(readFileSync(out,"utf8"));
      assert.equal(artifact.listings.length,1);
      assert.ok(artifact.sources.find((x: any)=>x.transaction===heldTx)?.error);
      const check = `from pathlib import Path
from cre_checkpoint_refresh import _validate_source_artifact_local, ArtifactValidationError
from cre_gate import count_artifacts
import sys
p=Path(sys.argv[1])
assert count_artifacts([p], True)[1].get("matthews")
try:
 _validate_source_artifact_local(p, "matthews", sys.argv[2], require_strict_freshness=True, expected_generation_id="test-generation", expected_generation_started_at=sys.argv[2])
except ArtifactValidationError as e:
 assert "reported an error" in str(e), str(e)
else:
 raise AssertionError("partial artifact accepted")
`;
      const verify=spawnSync("python3",["-c",check,out,generationStartedAt],{cwd:root,encoding:"utf8",timeout:30000});
      assert.ifError(verify.error); assert.equal(verify.status,0,verify.stderr);
    } finally { rmSync(dir,{recursive:true,force:true}); }
  });
}

function interruptedFixture(dir: string, hang: boolean): string {
  const preload = join(dir, "interrupted.mjs");
  writeFileSync(preload, `
    import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
    const deny=()=>{throw new Error("NETWORK_DENIED_BY_FIXTURE")};
    net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
    await import(${JSON.stringify(pathToFileURL(join(root, "sources/matthews-transport.ts")).href)});
    const host="https://www.matthews.com", urls=["good-peer","redirect","later"].map(x=>host+"/properties/"+x);
    globalThis.fetch=async input=>{const url=String(input);
      if(url===host+"/sitemap.xml") return new Response("<urlset>"+urls.map(u=>"<url><loc>"+u+"</loc></url>").join("")+"</urlset>");
      if(url===urls[1]) return new Response(null,{status:307,headers:{location:"/listings"}});
      if(url===urls[2] && ${JSON.stringify(hang)}) return new Promise(()=>{});
      if(url===urls[0]||url===urls[2]) return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><section><h1 id="propertyTitle">Good peer</h1><div id="propertyPrice">Contact broker</div><div class="prose">Peer</div></section></body></html>');
      throw new Error("UNEXPECTED_FIXTURE_URL");
    };
  `);
  return preload;
}

test("no output path still retains a private unqualified redirect sidecar", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-direct-"));
  try {
    const preload=interruptedFixture(dir,false);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,"collect.ts","--source=matthews","--transaction=sale"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.notEqual(child.status,0);
    const path=child.stderr.match(/Matthews private evidence \(unverified until sealed\): ([^\r\n]+)/)?.[1];
    assert.ok(path,child.stderr); assert.equal(statSync(path).mode & 0o777,0o600);
    const records=readFileSync(path,"utf8").trim().split("\n").map(line=>JSON.parse(line));
    assert.ok(records.some(x=>x.documents?.[0]?.outcome==="held_temporary_redirect"));
    assert.ok(records.some(x=>x.documents?.[0]?.schema==="matthews-completed-parse-observation/v1"));
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test("process termination leaves previously written private observations as unverified evidence", async () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-killed-"));
  const preload=interruptedFixture(dir,true);
  const child=spawn(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,"collect.ts","--source=matthews","--transaction=sale"],
    {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir},stdio:["ignore","ignore","pipe"]});
  let stderr=""; child.stderr.on("data",chunk=>{stderr+=String(chunk)});
  try {
    let path: string | undefined;
    for(let i=0;i<400;i++) {
      path=stderr.match(/Matthews private evidence \(unverified until sealed\): ([^\r\n]+)/)?.[1];
      if(path && existsSync(path)) {
        const text=readFileSync(path,"utf8");
        if(text.includes("held_temporary_redirect") && text.includes("matthews-completed-parse-observation/v1")) break;
      }
      await new Promise(resolve=>setTimeout(resolve,25));
    }
    assert.ok(path && existsSync(path),stderr);
    const before=readFileSync(path,"utf8");
    assert.match(before,/held_temporary_redirect/); assert.match(before,/matthews-completed-parse-observation/);
    child.kill("SIGKILL");
    await new Promise<void>(resolve=>child.once("exit",()=>resolve()));
    const after=readFileSync(path,"utf8"); assert.equal(after,before);
    assert.equal(statSync(path).mode & 0o777,0o600);
  } finally {if(child.exitCode===null && child.signalCode===null) child.kill("SIGKILL"); rmSync(dir,{recursive:true,force:true});}
});
