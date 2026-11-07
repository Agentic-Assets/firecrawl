import assert from "node:assert/strict";
import { createServer } from "node:http";
import { once } from "node:events";
import test from "node:test";
import { Agent } from "undici";
import { confirmMatthewsDestroy, matthewsFetch, MatthewsRedirectCleanupUnknown } from "../../../sources/matthews-transport.js";

test("dispatcher destroy confirmation distinguishes fulfillment, rejection and timeout", async () => {
  assert.equal(await confirmMatthewsDestroy(async () => {}, 20), true);
  assert.equal(await confirmMatthewsDestroy(async () => { throw new Error("unknown"); }, 20), false);
  assert.equal(await confirmMatthewsDestroy(() => new Promise(() => {}), 10), false);
});

test("strict full-source redirect path settles an actual loopback dispatcher", async () => {
  const server=createServer((_req,res)=>{res.writeHead(307,{location:"/other"});res.end("redirect");});
  server.listen(0,"127.0.0.1"); await once(server,"listening");
  try {
    const address=server.address(); assert.ok(address && typeof address !== "string");
    const result=await matthewsFetch(`http://127.0.0.1:${address.port}/detail`,true,true,true,true);
    assert.equal(result.status,307);
  } finally {server.close(); await once(server,"close");}
});

test("unacknowledged redirect body cancellation stops instead of retrying", async t => {
  const original=globalThis.fetch;
  let calls=0;
  globalThis.fetch=async ()=>{calls++; return {ok:false,status:307,headers:new Headers({location:"/other"}),
    body:{cancel:()=>Promise.reject(new Error("unsettled"))}} as unknown as Response;};
  t.after(()=>{globalThis.fetch=original});
  await assert.rejects(matthewsFetch("https://www.matthews.com/properties/test",true,true,true,true),MatthewsRedirectCleanupUnknown);
  assert.equal(calls,1);
});

test("targeted caller keeps its previous 307 behavior", async t => {
  const original=globalThis.fetch;
  let cancelled=false;
  globalThis.fetch=async ()=>({ok:false,status:307,headers:new Headers({location:"/other"}),
    body:{cancel:()=>{cancelled=true;return Promise.resolve()}}} as unknown as Response);
  t.after(()=>{globalThis.fetch=original});
  const result=await matthewsFetch("https://www.matthews.com/properties/test",true,true,true);
  assert.equal(result.status,307); assert.equal(cancelled,false);
});

test("dispatcher destruction rejection propagates cleanup unknown", async () => {
  const server=createServer((_req,res)=>{res.writeHead(307,{location:"/other"});res.end("redirect");});
  server.listen(0,"127.0.0.1"); await once(server,"listening");
  const original=Agent.prototype.destroy;
  let destroys=0;
  (Agent.prototype as any).destroy=function(this: Agent, ...args: unknown[]) {
    if (args.length) return (original as any).apply(this, args);
    destroys++;
    void original.call(this).catch(()=>{});
    return Promise.reject(new Error("unacknowledged"));
  };
  try {
    const address=server.address(); assert.ok(address && typeof address !== "string");
    await assert.rejects(matthewsFetch(`http://127.0.0.1:${address.port}/detail`,true,true,true,true),MatthewsRedirectCleanupUnknown);
    assert.equal(destroys,1);
  } finally {(Agent.prototype as any).destroy=original; server.close(); await once(server,"close");}
});
