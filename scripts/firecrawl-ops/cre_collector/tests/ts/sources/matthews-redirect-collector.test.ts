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

function interruptedFixture(dir: string, hang: boolean, redirect = true): string {
  const preload = join(dir, "interrupted.mjs");
  writeFileSync(preload, `
    import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
    import fs from "node:fs"; import {syncBuiltinESMExports} from "node:module";
    const originalMkdtemp=fs.mkdtempSync, originalOpen=fs.openSync, originalWrite=fs.writeSync, originalSync=fs.fsyncSync;
    let spoolFd=null, spoolWrites=0;
    fs.mkdtempSync=(prefix,...args)=>{if(process.env.FAIL_SPOOL_INIT==="1" && String(prefix).includes("cre-matthews-partial-")) throw new Error("synthetic spool init failure"); if(process.env.FAIL_FALLBACK_INIT==="1" && String(prefix).includes("cre-terminal-partial-")) throw new Error("synthetic fallback failure"); return originalMkdtemp(prefix,...args)};
    fs.openSync=(path,...args)=>{const fd=originalOpen(path,...args); if(String(path).includes("cre-matthews-partial-") && String(path).endsWith("attempt-1.jsonl")) spoolFd=fd; return fd};
    fs.writeSync=(fd,...args)=>{if(process.env.FAIL_SPOOL_WRITE==="1" && fd===spoolFd && ++spoolWrites>1) throw new Error("synthetic spool write failure"); return originalWrite(fd,...args)};
    fs.fsyncSync=fd=>{if(process.env.FAIL_SPOOL_SYNC==="1" && fd===spoolFd) throw new Error("synthetic spool sync failure"); return originalSync(fd)};
    syncBuiltinESMExports();
    const {registerHooks}=await import("node:module");
    const brokerModule=${JSON.stringify(pathToFileURL(join(root, "lib/broker.ts")).href)};
    const stub="data:text/javascript,"+encodeURIComponent('import {brokerRef} from '+JSON.stringify(brokerModule)+'; export async function srcFoundryCommercial(){return {company:"Synthetic",sourceUrl:"https://secret.invalid/PRIVATE_URL",method:"PRIVATE_METHOD",totalAvailable:1,listings:[{id:"public-peer",name:process.env.STUB_SECRET||"Public Peer",url:"https://example.test/PRIVATE_OPAQUE_PATH",extraFacts:{unknown:"PRIVATE_NESTED_VALUE"},brokerIds:[brokerRef({name:"Public Broker",company:"Synthetic"})],...(process.env.STUB_PRIVATE==="1" ? {rawSrs:{key:"PRIVATE_VALUE"},srsRawRetention:{private:true}} : {})}]}}');
    const errorsModule=${JSON.stringify(pathToFileURL(join(root, "lib/source-observations.ts")).href)};
    const essexStub="data:text/javascript,"+encodeURIComponent('import {RetainedSourceError} from '+JSON.stringify(errorsModule)+'; export async function srcEssexRealty(){throw new RetainedSourceError(new Error("synthetic Essex mismatch"),[{schema:"essex-inventory-mismatch-v1", privateEvidence:"PRIVATE_ESSEX"}])}');
    registerHooks({resolve(s,c,next){if(c.parentURL?.endsWith("/collect.ts")){if(process.env.STUB_PEER==="1" && s==="./sources/foundry-commercial.js") return {url:stub,shortCircuit:true};if(process.env.STUB_ESSEX==="1" && s==="./sources/essex-realty.js") return {url:essexStub,shortCircuit:true}}return next(s,c)}});
    const deny=()=>{throw new Error("NETWORK_DENIED_BY_FIXTURE")};
    net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
    await import(${JSON.stringify(pathToFileURL(join(root, "sources/matthews-transport.ts")).href)});
    const host="https://www.matthews.com", urls=["good-peer","redirect","later"].map(x=>host+"/properties/"+x);
    globalThis.fetch=async input=>{const url=String(input);
      if(url===host+"/sitemap.xml") return new Response("<urlset>"+urls.map(u=>"<url><loc>"+u+"</loc></url>").join("")+"</urlset>");
      if(url===urls[0] && process.env.FAIL_ORDINARY_FIRST==="1") return new Response(null,{status:418});
      if(url===urls[1] && ${JSON.stringify(redirect)}) {
        if(process.env.FAIL_REDIRECT_CLEANUP==="1") return {ok:false,status:307,headers:new Headers({location:"/listings"}),body:{cancel:()=>Promise.reject(new Error("unsettled"))}};
        return new Response(null,{status:307,headers:{location:"/listings"}});
      }
      if(url===urls[2] && ${JSON.stringify(hang)}) return new Promise(()=>{});
      if(url===urls[0]||url===urls[1]||url===urls[2]) return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><section><h1 id="propertyTitle">Good peer</h1><div id="propertyPrice">Contact broker</div><div class="prose">Peer</div></section></body></html>');
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

for (const redirect of [false,true]) {
  test(`spool initialization failure preserves ${redirect ? "partial" : "healthy"} collector output`, () => {
    const dir=mkdtempSync(join(tmpdir(),"matthews-init-failure-"));
    try {
      const preload=interruptedFixture(dir,false,redirect),out=join(dir,"artifact.json");
      const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
        "collect.ts","--source=matthews","--transaction=sale",`--out=${out}`],
        {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:"1"},encoding:"utf8",timeout:30000});
      assert.ifError(child.error); assert.equal(child.status,redirect ? 1 : 0,child.stderr);
      assert.match(child.stderr,/spool unavailable; interrupted observations may be lost/);
      const artifact=JSON.parse(readFileSync(out,"utf8"));
      assert.equal(artifact.sources[0].partialCapture.state,"failed");
      if (redirect) {
        assert.equal(artifact.runMeta.evidenceOnly,true); assert.deepEqual(artifact.listings,[]);
        assert.match(artifact.sources[0].error,/partial.*temporary redirect/);
        assert.ok(artifact.sources[0].retainedObservations.some((x: any)=>x.schema==="matthews-completed-parse-observation/v1"));
      } else { assert.equal(artifact.listings.length,3); assert.equal(artifact.sources[0].truncated,false); }
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
}

test("final output collision remains a reported failure when spool is unavailable", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-output-failure-"));
  try {
    const preload=interruptedFixture(dir,false,true),out=join(dir,"artifact.json");
    writeFileSync(out,'{"marker":"unchanged"}');
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews","--transaction=sale",`--out=${out}`],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.notEqual(child.status,0);
    assert.match(child.stderr,/spool unavailable/);
    assert.deepEqual(JSON.parse(readFileSync(out,"utf8")),{marker:"unchanged"});
  } finally {rmSync(dir,{recursive:true,force:true});}
});

for (const redirect of [false,true]) {
  test(`mid-write spool failure preserves ${redirect ? "partial" : "healthy"} collector output`, () => {
    const dir=mkdtempSync(join(tmpdir(),"matthews-write-failure-"));
    try {
      const preload=interruptedFixture(dir,false,redirect),out=join(dir,"artifact.json");
      const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
        "collect.ts","--source=matthews","--transaction=sale",`--out=${out}`],
        {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_WRITE:"1"},encoding:"utf8",timeout:30000});
      assert.ifError(child.error); assert.equal(child.status,redirect ? 1 : 0,child.stderr);
      assert.match(child.stderr,/spool failed; interrupted observations may be lost/);
      const artifact=JSON.parse(readFileSync(out,"utf8"));
      assert.equal(artifact.sources[0].partialCapture.state,"failed");
      if (redirect) {
        assert.equal(artifact.runMeta.evidenceOnly,true); assert.deepEqual(artifact.listings,[]);
        assert.match(artifact.sources[0].error,/partial.*temporary redirect/);
        assert.ok(artifact.sources[0].retainedObservations.some((x: any)=>x.schema==="matthews-completed-parse-observation/v1"));
      } else {assert.equal(artifact.listings.length,3); assert.equal(artifact.sources[0].truncated,false);}
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
}

test("healthy direct stdout remains available when spool initialization fails", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-direct-init-failure-"));
  try {
    const preload=interruptedFixture(dir,false,false);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews","--transaction=sale"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.equal(child.status,0,child.stderr);
    assert.match(child.stderr,/spool unavailable/);
    const artifact=JSON.parse(child.stdout);
    assert.equal(artifact.listings.length,3); assert.equal(artifact.sources[0].partialCapture.state,"failed");
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test("spool sealing failure is reported without suppressing healthy output", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-sync-failure-"));
  try {
    const preload=interruptedFixture(dir,false,false),out=join(dir,"artifact.json");
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews","--transaction=sale",`--out=${out}`],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_SYNC:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.equal(child.status,0,child.stderr);
    assert.match(child.stderr,/spool was not fully sealed/);
    const artifact=JSON.parse(readFileSync(out,"utf8"));
    assert.equal(artifact.sources[0].partialCapture.state,"failed");
    assert.equal(artifact.listings.length,3);
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test("partial direct call without output retains private fallback when spool initialization fails", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-direct-partial-failure-"));
  try {
    const preload=interruptedFixture(dir,false,true);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews","--transaction=sale"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.notEqual(child.status,0);
    assert.match(child.stderr,/spool unavailable/);
    const path=child.stderr.match(/Matthews private fallback artifact: ([^\r\n]+)/)?.[1];
    assert.ok(path,child.stderr); assert.equal(statSync(path).mode & 0o777,0o600);
    const artifact=JSON.parse(readFileSync(path,"utf8"));
    assert.equal(artifact.runMeta.evidenceOnly,true); assert.deepEqual(artifact.listings,[]);
    assert.equal(artifact.sources[0].partialCapture.state,"failed");
    assert.ok(artifact.sources[0].retainedObservations.some((x: any)=>x.outcome==="held_temporary_redirect"));
  } finally {rmSync(dir,{recursive:true,force:true});}
});

for (const mixed of [false,true]) {
  test(`failed private fallback produces safe status projection${mixed ? " with healthy peer source" : ""}`, () => {
    const dir=mkdtempSync(join(tmpdir(),"matthews-fallback-loss-"));
    try {
      const preload=interruptedFixture(dir,false,true);
      const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
        "collect.ts",`--source=${mixed ? "matthews,foundry-commercial" : "matthews"}`,"--transaction=sale"],
        {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:"1",FAIL_FALLBACK_INIT:"1",STUB_PEER:mixed ? "1" : "0"},encoding:"utf8",timeout:30000});
      assert.ifError(child.error); assert.notEqual(child.status,0);
      assert.match(child.stderr,/private fallback artifact could not be retained; observations may be lost/);
      assert.doesNotMatch(child.stdout,/PRIVATE_|matthews-completed-parse|held_temporary_redirect|good-peer|secret\.invalid/);
      const artifact=JSON.parse(child.stdout);
      assert.equal(artifact.runMeta.evidenceOnly,true);
      assert.equal(artifact.runMeta.evidencePurpose,"private_retention_failed");
      assert.equal(artifact.runMeta.coverage,"unverified");
      assert.equal(artifact.sources.find((x: any)=>x.sourceKey==="matthews").failed,true);
      if(mixed) {
        const peer=artifact.sources.find((x: any)=>x.sourceKey==="foundry-commercial");
        assert.equal(peer.failed,false); assert.equal(peer.listingsCollected,1);
        assert.equal(artifact.runMeta.peerRowsAvailable,true);
        assert.equal(artifact.listings.length,1); assert.equal(artifact.listings[0].name,"Public Peer");
        assert.equal(artifact.listings[0].id,undefined); assert.equal(artifact.listings[0].url,undefined);
        assert.match(artifact.runMeta.omittedFields,/IDs, URLs/);
        assert.deepEqual(artifact.listings[0].brokerIds,[0]); assert.equal(artifact.brokers[0].name,"Public Broker");
        assert.equal(artifact.totalListings,1);
      } else {assert.deepEqual(artifact.listings,[]); assert.deepEqual(artifact.brokers,[]); assert.equal(artifact.totalListings,0);}
    } finally {rmSync(dir,{recursive:true,force:true});}
  });
}

test("private peer evidence forces status-only projection on fallback failure", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-private-peer-"));
  try {
    const preload=interruptedFixture(dir,false,true);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews,foundry-commercial","--transaction=sale"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:"1",FAIL_FALLBACK_INIT:"1",STUB_PEER:"1",STUB_PRIVATE:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.notEqual(child.status,0);
    assert.match(child.stderr,/Private peer evidence prevents row projection; peer data was not retained/);
    assert.doesNotMatch(child.stdout,/PRIVATE_|public-peer|secret\.invalid/);
    const artifact=JSON.parse(child.stdout);
    assert.equal(artifact.runMeta.peerRowsAvailable,false); assert.deepEqual(artifact.listings,[]);
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test("unknown 307 cleanup stops collector before later source dispatch", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-cleanup-unknown-"));
  try {
    const preload=interruptedFixture(dir,false,true);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews,foundry-commercial","--transaction=sale"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_REDIRECT_CLEANUP:"1",STUB_PEER:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.equal(child.status,71,child.stderr);
    assert.match(child.stderr,/redirect response cleanup unknown; source dispatch stopped/);
    assert.doesNotMatch(child.stderr,/collecting foundry-commercial/);
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test("known secret in a canonical peer field forces status-only fallback", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-secret-peer-"));
  try {
    const preload=interruptedFixture(dir,false,true);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews,foundry-commercial","--transaction=sale"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:"1",FAIL_FALLBACK_INIT:"1",STUB_PEER:"1",STUB_SECRET:"SYNTHETIC_SECRET_012345"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.notEqual(child.status,0);
    assert.doesNotMatch(child.stdout,/SYNTHETIC_SECRET_012345|public-peer/);
    const artifact=JSON.parse(child.stdout);
    assert.equal(artifact.runMeta.peerRowsAvailable,false); assert.deepEqual(artifact.listings,[]);
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test("later unknown cleanup takes precedence over earlier ordinary failure and stops peers", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-mixed-cleanup-"));
  try {
    const preload=interruptedFixture(dir,false,true);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews,foundry-commercial","--transaction=sale","--concurrency=2"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_ORDINARY_FIRST:"1",FAIL_REDIRECT_CLEANUP:"1",STUB_PEER:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.equal(child.status,71,child.stderr);
    const ordinary=child.stderr.indexOf("Matthews HTTP 418"), unknown=child.stderr.indexOf("Matthews redirect body cleanup unknown");
    assert.ok(ordinary >= 0 && unknown > ordinary,child.stderr);
    assert.match(child.stderr,/redirect response cleanup unknown; source dispatch stopped/);
    assert.doesNotMatch(child.stderr,/collecting foundry-commercial/);
  } finally {rmSync(dir,{recursive:true,force:true});}
});


test("settled identity failure retains later peers without admitting the partial source", () => {
  const dir = mkdtempSync(join(tmpdir(), "matthews-identity-peer-"));
  try {
    const preload = join(dir, "preload.mjs"), out = join(dir, "artifact.json");
    writeFileSync(preload, `
      import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
      const deny=()=>{throw new Error("NETWORK_DENIED_BY_FIXTURE")};
      net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
      await import(${JSON.stringify(pathToFileURL(join(root, "sources/matthews-transport.ts")).href)});
      const host="https://www.matthews.com", urls=["bad", "good-a", "good-b"].map(x=>host+"/properties/"+x);
      globalThis.fetch=async input=>{ const url=String(input);
        if(url===host+"/sitemap.xml") return new Response("<urlset>"+urls.map(u=>"<url><loc>"+u+"</loc></url>").join("")+"</urlset>");
        if(url===urls[0]) return new Response('<html><head><link rel="canonical" href="'+host+'/properties/different"></head><body><section><h1 id="propertyTitle">Bad identity</h1></section></body></html>');
        if(url===urls[1]||url===urls[2]) return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><section><h1 id="propertyTitle">Good peer</h1><div id="propertyPrice">Contact broker</div><div class="prose">Peer</div></section></body></html>');
        throw new Error("UNEXPECTED_FIXTURE_URL");
      };
    `);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,"collect.ts","--source=matthews","--transaction=sale",`--out=${out}`],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.notEqual(child.status,0);
    assert.doesNotMatch(child.stderr,/NETWORK_DENIED_BY_FIXTURE|UNEXPECTED_FIXTURE_URL/);
    const artifact=JSON.parse(readFileSync(out,"utf8"));
    assert.equal(artifact.runMeta.evidenceOnly,true); assert.deepEqual(artifact.listings,[]);
    assert.match(artifact.sources[0].error,/1 detail parse or identity failure/);
    const observations=artifact.sources[0].retainedObservations;
    assert.equal(observations.filter((x:any)=>x.schema==="matthews-completed-parse-observation/v1").length,2);
    assert.ok(observations.some((x:any)=>x.schema==="matthews-detail-observation/v1" && x.requestedUrl.endsWith("/bad")));
  } finally {rmSync(dir,{recursive:true,force:true});}
});

for (const spoolFails of [true, false]) test(`mixed Essex mismatch and failed private fallback report safe status with spool ${spoolFails ? "unavailable" : "available"}`, () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-essex-fallback-"));
  try {
    const preload=interruptedFixture(dir,false,true);
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews,essex-realty,foundry-commercial","--transaction=sale"],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",TMPDIR:dir,FAIL_SPOOL_INIT:spoolFails ? "1" : "0",FAIL_FALLBACK_INIT:"1",STUB_ESSEX:"1",STUB_PEER:"1"},encoding:"utf8",timeout:30000});
    assert.ifError(child.error); assert.notEqual(child.status,0);
    assert.match(child.stderr,/private fallback artifact could not be retained/);
    assert.doesNotMatch(child.stdout,/PRIVATE_|essex-inventory-mismatch|held_temporary_redirect/);
    const artifact=JSON.parse(child.stdout);
    assert.equal(artifact.runMeta.evidenceOnly,true); assert.equal(artifact.runMeta.peerRowsAvailable,false);
    assert.deepEqual(artifact.listings,[]);
    assert.equal(artifact.sources.find((x: any)=>x.sourceKey==="essex-realty").failed,true);
  } finally {rmSync(dir,{recursive:true,force:true});}
});


test("settled mixed Matthews artifact derives generation-bound valid peers", () => {
  const dir=mkdtempSync(join(tmpdir(),"matthews-partial-custody-"));
  try {
    const preload=join(dir,"preload.mjs"), out=join(dir,"artifact.json");
    writeFileSync(preload, `
      import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
      const deny=()=>{throw new Error("NETWORK_DENIED_BY_FIXTURE")};
      net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
      await import(${JSON.stringify(pathToFileURL(join(root,"sources/matthews-transport.ts")).href)});
      const host="https://www.matthews.com", urls=["redirect","good-sale","leasing-good"].map(x=>host+"/properties/"+x);
      globalThis.fetch=async input=>{const url=String(input);
        if(url===host+"/sitemap.xml") return new Response("<urlset>"+urls.map(u=>"<url><loc>"+u+"</loc></url>").join("")+"</urlset>");
        if(url===urls[0]) return new Response(null,{status:307,headers:{location:"/listings"}});
        if(url===urls[1]||url===urls[2]) return new Response('<html><head><link rel="canonical" href="'+url+'"></head><body><section><h1 id="propertyTitle">Good peer</h1><div id="propertyPrice">Contact broker</div><div class="prose">Peer</div></section></body></html>');
        throw new Error("UNEXPECTED_FIXTURE_URL");
      };
    `);
    const started=new Date(Date.now()-1000).toISOString();
    const child=spawnSync(process.execPath,["--import","tsx","--import",pathToFileURL(preload).href,
      "collect.ts","--source=matthews","--transaction=both",`--out=${out}`],
      {cwd:root,env:{...process.env,CRE_REQUIRE_FRESH_DETAILS:"1",CRE_REFRESH_GENERATION:"fixture-generation",CRE_REFRESH_STARTED_AT:started},encoding:"utf8",timeout:30000});
    const finished=new Date(Date.now()+1000).toISOString();
    assert.ifError(child.error); assert.doesNotMatch(child.stderr,/NETWORK_DENIED_BY_FIXTURE|UNEXPECTED_FIXTURE_URL/);
    const check=`import json,sys
from pathlib import Path
from matthews_partial.checkpoint import derive,gate_projection
from cre_artifact_scope import require_canonical_artifact
raw=Path(sys.argv[1]).read_bytes()
r=derive(raw,generation_id="fixture-generation",generation_started_at=sys.argv[2],attempt_started_at=sys.argv[2],attempt_finished_at=sys.argv[3])
assert {x["external_id"] for x in r["rows"]}=={"good-sale","leasing-good"},r
assert len(r["held"])==1 and r["mark_missing_safe"] is False
g=gate_projection(raw,r)
require_canonical_artifact(g,allow_matthews_partial_gate=True)
try: require_canonical_artifact(g)
except ValueError: pass
else: raise AssertionError("partial gate artifact admitted as canonical")
`;
    const verify=spawnSync("python3",["-c",check,out,started,finished],{cwd:root,encoding:"utf8",timeout:30000});
    assert.ifError(verify.error); assert.equal(verify.status,0,verify.stderr);
    if (process.env.CRE_MATTHEWS_POSTGRES_FIXTURE === "1") {
      const postgres=spawnSync("python3",["tests/run_matthews_collector_fill_postgres.py",out,started,finished],
        {cwd:root,encoding:"utf8",timeout:90000});
      assert.ifError(postgres.error); assert.equal(postgres.status,0,postgres.stderr);
    }
  } finally {rmSync(dir,{recursive:true,force:true});}
});
