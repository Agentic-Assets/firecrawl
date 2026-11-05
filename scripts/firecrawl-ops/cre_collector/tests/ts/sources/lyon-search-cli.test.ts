import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";
import { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

test("checkpoint Lyon collection seals the bounded partial search and never crawls the sitemap", () => {
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const dir = mkdtempSync(join(tmpdir(), "lyon-partial-"));
  const output = join(dir, "artifact.json"), spool = join(dir, "spool.jsonl"), preload = join(dir, "preload.mjs");
  const raw = JSON.stringify([{ permalink: "https://lyonstahl.com/?post_type=properties&p=40201", title: "Observed", status: null }]);
  writeFileSync(preload, `import net from 'node:net'; import tls from 'node:tls'; import http from 'node:http'; import https from 'node:https';
    const deny=()=>{throw Error('NETWORK_DENIED')}; net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
    const original=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>original(fn,ms>=9000&&ms<=10000?1:ms,...args);
    globalThis.fetch=async input=>{const url=String(input); if(!url.startsWith('https://lyonstahl.com/wp-admin/admin-ajax.php?')) throw Error('UNEXPECTED_REQUEST'); return new Response(${JSON.stringify(raw)}, {headers:{'content-type':'application/json'}})};`);
  try {
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href, "collect.ts", "--source=lyon-stahl", "--transaction=both", `--out=${output}`], {
      cwd: root, encoding: "utf8", timeout: 30000, maxBuffer: 1024 * 1024,
      env: { ...process.env, CRE_REFRESH_GENERATION: "lyon-test", CRE_REFRESH_STARTED_AT: new Date().toISOString(),
        CRE_REQUIRE_FRESH_DETAILS: "1", CRE_LYON_STAHL_PARTIAL_PATH: spool, CRE_LYON_STAHL_PARTIAL_ATTEMPT: "1" },
    });
    assert.ifError(child.error);
    assert.notEqual(child.status, 0);
    assert.doesNotMatch(child.stderr, /NETWORK_DENIED|UNEXPECTED_REQUEST/);
    const artifact = JSON.parse(readFileSync(output, "utf8"));
    assert.equal(artifact.runMeta.evidenceOnly, true);
    assert.deepEqual(artifact.listings, []);
    const sale = artifact.sources.find((row: any) => row.transaction === "sale");
    assert.match(sale.error, /partial/);
    assert.equal(sale.partialCapture.state, "sealed");
    assert.equal(sale.partialCapture.outcome, "source_error");
    const lines = readFileSync(spool, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(lines.length, 4);
    assert.deepEqual(lines.slice(1).map(line => line.documents[0].filter), ["all", "sale", "lease"]);
    assert.ok(lines.slice(1).every(line => line.documents[0].responseText === raw));
    if (process.platform !== "win32") assert.equal(statSync(spool).mode & 0o077, 0);
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("access denial retains earlier private page and does not dispatch the next filter", () => {
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const dir = mkdtempSync(join(tmpdir(), "lyon-denied-"));
  const output = join(dir, "artifact.json"), spool = join(dir, "spool.jsonl"), preload = join(dir, "preload.mjs");
  writeFileSync(preload, `import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
    const deny=()=>{throw Error("NETWORK_DENIED")}; net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
    const original=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>original(fn,ms>=9000&&ms<=10000?1:ms,...args);
    globalThis.fetch=async input=>{const url=String(input); if(url.includes("Status=all-status")) return new Response("[]", {headers:{"content-type":"application/json"}}); if(url.includes("Status=for-sale")) return new Response("denied", {status:403}); throw Error("UNEXPECTED_LEASE_REQUEST")};`);
  try {
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href, "collect.ts", "--source=lyon-stahl", "--transaction=both", `--out=${output}`], {
      cwd: root, encoding: "utf8", timeout: 30000, maxBuffer: 1024 * 1024,
      env: { ...process.env, CRE_REFRESH_GENERATION: "lyon-test", CRE_REFRESH_STARTED_AT: new Date().toISOString(),
        CRE_REQUIRE_FRESH_DETAILS: "1", CRE_LYON_STAHL_PARTIAL_PATH: spool, CRE_LYON_STAHL_PARTIAL_ATTEMPT: "1" },
    });
    assert.ifError(child.error);
    assert.notEqual(child.status, 0);
    assert.doesNotMatch(child.stderr, /NETWORK_DENIED|UNEXPECTED_LEASE_REQUEST/);
    const artifact = JSON.parse(readFileSync(output, "utf8"));
    const sale = artifact.sources.find((row: any) => row.transaction === "sale");
    assert.equal(sale.retainedObservations[0].stopped, true);
    assert.equal(sale.retainedObservations[0].failures[0].reason, "access_stopped");
    const lines = readFileSync(spool, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(lines.length, 2);
    assert.equal(lines[1].documents[0].responseText, "[]");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});

test("unsettled headers preserve earlier private page with terminal classification", () => {
  const root = fileURLToPath(new URL("../../../", import.meta.url));
  const dir = mkdtempSync(join(tmpdir(), "lyon-terminal-"));
  const output = join(dir, "artifact.json"), spool = join(dir, "spool.jsonl"), preload = join(dir, "preload.mjs");
  writeFileSync(preload, `import net from "node:net"; import tls from "node:tls"; import http from "node:http"; import https from "node:https";
    const deny=()=>{throw Error("NETWORK_DENIED")}; net.Socket.prototype.connect=deny; tls.connect=deny; http.request=deny; https.request=deny;
    const original=globalThis.setTimeout; globalThis.setTimeout=(fn,ms,...args)=>original(fn,ms===60000?50:(ms===1000||(ms>=9000&&ms<=10000))?1:ms,...args);
    globalThis.fetch=async input=>{const url=String(input); if(url.includes("Status=all-status")) return new Response("[]", {headers:{"content-type":"application/json"}}); if(url.includes("Status=for-sale")) return new Promise(()=>{}); throw Error("UNEXPECTED_LEASE_REQUEST")};`);
  try {
    const child = spawnSync(process.execPath, ["--import", "tsx", "--import", pathToFileURL(preload).href, "collect.ts", "--source=lyon-stahl", "--transaction=both", `--out=${output}`], {
      cwd: root, encoding: "utf8", timeout: 30000, maxBuffer: 1024 * 1024,
      env: { ...process.env, CRE_REFRESH_GENERATION: "lyon-test", CRE_REFRESH_STARTED_AT: new Date().toISOString(),
        CRE_REQUIRE_FRESH_DETAILS: "1", CRE_LYON_STAHL_PARTIAL_PATH: spool, CRE_LYON_STAHL_PARTIAL_ATTEMPT: "1" },
    });
    assert.ifError(child.error);
    assert.notEqual(child.status, 0);
    assert.doesNotMatch(child.stderr, /NETWORK_DENIED|UNEXPECTED_LEASE_REQUEST/);
    const artifact = JSON.parse(readFileSync(output, "utf8"));
    assert.equal(artifact.runMeta.terminalCaptureFailure, true);
    const sale = artifact.sources.find((row: any) => row.transaction === "sale");
    assert.equal(sale.error, "native_capture_failed");
    const lines = readFileSync(spool, "utf8").trim().split("\n").map(line => JSON.parse(line));
    assert.equal(lines.length, 2);
    assert.equal(lines[1].documents[0].responseText, "[]");
  } finally { rmSync(dir, { recursive: true, force: true }); }
});
