import assert from "node:assert/strict";
import { mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";
import { test } from "node:test";

const preload = `import fs from "node:fs";
import { registerHooks } from "node:module";
const calls=[];
globalThis.fetch=async (url) => {
 calls.push(String(url));
 if (!String(url).startsWith("https://services.kidder.com/")) throw new Error("unexpected offline request");
 if(process.env.KIDDER_UNKNOWN==="1") return {ok:false,status:403,body:{cancel:()=>Promise.reject(new Error("unsettled"))}};
 return new Response("denied",{status:Number(process.env.KIDDER_STATUS)});
};
const stub="data:text/javascript,"+encodeURIComponent(` + JSON.stringify('export async function srcNewmark(){return {company:"Synthetic",sourceUrl:"https://example.test",method:"stub",totalAvailable:1,listings:[{id:"peer",url:"https://example.test/peer"}]}}') + `);
registerHooks({resolve(s,c,next){if(c.parentURL?.endsWith("/collect.ts") && s==="./sources/newmark.js") return {url:stub,shortCircuit:true};return next(s,c)}});
process.on("exit",()=>fs.writeFileSync(process.env.KIDDER_CALLS,JSON.stringify(calls),{flag:"wx",mode:0o600}));`;

function run(status: number, mixed = false, unknown = false) {
  const dir = mkdtempSync(join(tmpdir(), "kidder-terminal-"));
  try {
    const hook = join(dir, "hook.mjs"), output = join(dir, "artifact.json"), calls = join(dir, "calls.json");
    writeFileSync(hook, preload);
    const result = spawnSync(process.execPath, ["--import", "tsx", "--import", hook, "collect.ts",
      `--source=${mixed ? "kidder-mathews,newmark" : "kidder-mathews"}`,
      "--transaction=both", "--max-items=0", `--out=${output}`], {
      cwd: process.cwd(), encoding: "utf8", timeout: 20000,
      env: { ...process.env, CRE_REQUIRE_FRESH_DETAILS: "1", CRE_REFRESH_GENERATION: "fixture-run",
        KIDDER_STATUS: String(status), KIDDER_UNKNOWN: unknown ? "1" : "0", KIDDER_CALLS: calls },
    });
    const requested = JSON.parse(readFileSync(calls, "utf8"));
    const artifact = mixed && result.status === 0 ? JSON.parse(readFileSync(output, "utf8")) : null;
    return { result, requested, artifact };
  } finally { rmSync(dir, { recursive: true, force: true }); }
}

for (const [status, expected] of [[401,72],[403,74],[429,75]]) {
  test(`settled ${status} stops the second transaction`, () => {
    const { result, requested } = run(status);
    assert.equal(result.status, expected, result.stderr);
    assert.equal(requested.length, 1);
    assert.doesNotMatch(result.stderr, /collecting kidder-mathews\/lease/);
  });
}

test("settled denial retains an independently successful peer", () => {
  const { result, requested, artifact } = run(403, true);
  assert.equal(result.status, 0, result.stderr);
  assert.equal(requested.length, 1);
  assert.ok(artifact.sources.some((s: any) => s.sourceKey === "kidder-mathews" && s.error === "inventory HTTP 403"));
  assert.ok(artifact.listings.some((s: any) => s.sourceKey === "newmark" && s.id === "peer"));
});

test("unsettled response cleanup gets a distinct exit and stops dispatch", () => {
  const { result, requested } = run(403, false, true);
  assert.equal(result.status, 73, result.stderr);
  assert.equal(requested.length, 1);
  assert.doesNotMatch(result.stderr, /collecting kidder-mathews\/lease/);
});
