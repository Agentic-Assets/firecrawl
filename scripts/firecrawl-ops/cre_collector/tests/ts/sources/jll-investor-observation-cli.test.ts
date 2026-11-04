import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtempSync, writeFileSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { spawnSync } from "node:child_process";

test("collector writes JLL excluded observations as source metadata, keeping healthy sibling", () => {
  const dir = mkdtempSync(join(tmpdir(), "jll-observation-"));
  const preload = join(dir, "preload.mjs"), output = join(dir, "attempt.json");
  const observation = { searchId: "excluded-id", searchAlias: "office/excluded", sourceUrl: "https://invest.jll.com/us/en/listings/office/excluded", inventoryObservedAt: "2026-07-29T12:00:00.000Z", embeddedProviderErrorStatusCode: 404, confirmation: "live_current_build_json_and_public_page" };
  const jll = `export async function srcJllInvestor(tx) { return tx === "sale" ? {company:"JLL",sourceUrl:"https://invest.jll.com/",method:"fixture",totalAvailable:2,listings:[{id:"good-id",name:"Good JLL"}],truncated:true,jllInvestorNotFoundObservations:[${JSON.stringify(observation)}]} : {company:"JLL",sourceUrl:"https://invest.jll.com/",method:"fixture",totalAvailable:0,listings:[],truncated:false}; }`;
  const sibling = `export async function srcInterraRealty(tx) { return {company:"Interra",sourceUrl:"https://interrarealty.com/",method:"fixture",totalAvailable:1,listings:[{id:"sibling-id",name:"Healthy sibling"}],truncated:false}; }`;
  writeFileSync(preload, `import {registerHooks} from "node:module"; const jll=${JSON.stringify("data:text/javascript,"+encodeURIComponent(jll))}, sibling=${JSON.stringify("data:text/javascript,"+encodeURIComponent(sibling))}; registerHooks({resolve(s,c,n){if(c.parentURL?.endsWith("/collect.ts")){if(s==="./sources/jll-investor.js")return {url:jll,shortCircuit:true};if(s==="./sources/interra-realty.js")return {url:sibling,shortCircuit:true};}return n(s,c);}});`);
  try {
    const child = spawnSync(process.execPath, ["--import","tsx","--import",preload,"collect.ts","--source=jll-investor,interra-realty","--transaction=both","--max-items=0",`--out=${output}`], {cwd:process.cwd(),encoding:"utf8",timeout:20000});
    assert.equal(child.status, 0, child.stderr);
    const data = JSON.parse(readFileSync(output, "utf8"));
    const source = data.sources.find((s: any) => s.sourceKey === "jll-investor" && s.transaction === "sale");
    assert.equal(source.truncated, true);
    assert.deepEqual(source.jllInvestorNotFoundObservations, [observation]);
    assert.ok(data.listings.some((l: any) => l.id === "good-id"));
    assert.ok(data.listings.some((l: any) => l.id === "sibling-id"));
    assert.ok(data.listings.every((l: any) => l.id !== "excluded-id"));
  } finally { rmSync(dir, {recursive:true,force:true}); }
});
