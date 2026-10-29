import { strict as assert } from 'node:assert';
import { mkdirSync, mkdtempSync, readFileSync, readdirSync, rmSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { gunzipSync } from 'node:zlib';
import { test } from 'node:test';
import { newmarkNimPage } from '../../../sources/newmark.js';
import { NativeJsonFeedCaptureError } from '../../../lib/source-evidence/native-json-feed.js';

test('Newmark captures exact decoded response before parsing and stops on publication failure', async () => {
  const dir=mkdtempSync(join(tmpdir(),'newmark-feed-')); const original=globalThis.fetch;
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR=dir; process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY='newmark';
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER='1'; process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES='1';
  delete process.env.CRE_REQUIRE_FRESH_DETAILS;
  const rows=Array.from({length:70},(_,i)=>({id:i, values:[false,null,'',{},[]]}));
  const raw=JSON.stringify({total:70, data:rows, extra:{empty:[], disabled:false, missing:null}});
  let calls=0; let request:any;
  globalThis.fetch=(async (_url:unknown, options:RequestInit)=>{calls++;request=JSON.parse(String(options.body));return new Response(raw);}) as typeof fetch;
  try {
    assert.deepEqual(await newmarkNimPage('sale',0),JSON.parse(raw));
    assert.equal(request.type,2); assert.equal(request.take,100); assert.equal(request.page,0);
    const names=readdirSync(dir); assert.equal(names.length,1);
    const entry=JSON.parse(readFileSync(join(dir,names[0]),'utf8'));
    assert.equal(entry.profile,'newmark_nim_sale_post_v1'); assert.equal(entry.receipt.offset,0);
    assert.equal(gunzipSync(Buffer.from(entry.responseBodyBase64,'base64')).toString('utf8'),raw);
    await assert.rejects(newmarkNimPage('lease',1),NativeJsonFeedCaptureError);
    assert.equal(request.type,1); assert.equal(request.page,1); assert.equal(calls,2);
    assert.deepEqual(readdirSync(dir),names);
    await assert.rejects(newmarkNimPage('sale',0,99),NativeJsonFeedCaptureError);
    await assert.rejects(newmarkNimPage('sale',-1),NativeJsonFeedCaptureError);
    assert.equal(calls,2);
    delete process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR;
    assert.deepEqual(await newmarkNimPage('sale',0,99),JSON.parse(raw)); assert.equal(request.take,99);
  } finally {globalThis.fetch=original; for(const k of ['DIR','SOURCE_KEY','SINGLE_WRITER','MAX_PAGES']) delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${k}`]; rmSync(dir,{recursive:true,force:true});}
});

test('malformed response is retained before normal parser retries', async () => {
  const dir=mkdtempSync(join(tmpdir(),'newmark-invalid-'));const original=globalThis.fetch;let calls=0;
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR=dir;process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY='newmark';process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER='1';
  globalThis.fetch=(async()=>{calls++;return new Response('{invalid');}) as typeof fetch;
  try {await assert.rejects(newmarkNimPage('lease',0,100,30000,0),SyntaxError);assert.equal(calls,6);
    const files=readdirSync(dir);assert.equal(files.length,6);
    for(const name of files){const entry=JSON.parse(readFileSync(join(dir,name),'utf8'));assert.equal(entry.profile,'newmark_nim_lease_post_v1');assert.equal(gunzipSync(Buffer.from(entry.responseBodyBase64,'base64')).toString(),'{invalid');}
  } finally {globalThis.fetch=original;for(const k of ['DIR','SOURCE_KEY','SINGLE_WRITER'])delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${k}`];rmSync(dir,{recursive:true,force:true});}
});

test('normal both-transactions CLI terminates after capture failure without a second dispatch', () => {
  const dir=mkdtempSync(join(tmpdir(),'newmark-cli-'));const preload=join(dir,'preload.mjs');const count=join(dir,'calls');
  writeFileSync(preload,`import {appendFileSync} from 'node:fs'; globalThis.fetch=async()=>{appendFileSync(${JSON.stringify(count)},'x');return new Response('{"total":0,"data":[]}');};`);
  try {
    const child=spawnSync(process.execPath,['--import','tsx','--import',preload,'collect.ts','--source','newmark','--transaction','both','--max-items','1','--out',join(dir,'out.json')],{
      cwd:process.cwd(),encoding:'utf8',timeout:20000,env:{...process.env,CRE_NATIVE_JSON_FEED_CAPTURE_DIR:dir,CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY:'newmark',CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER:'1',CRE_NATIVE_JSON_FEED_CAPTURE_MAX_BYTES:'1'}});
    assert.equal(child.status,1);assert.equal(readFileSync(count,'utf8'),'x');assert.match(child.stderr,/native_capture_failed/);
  } finally {rmSync(dir,{recursive:true,force:true});}
});

test('strict source captures all 70 distinct rows before geography and max filtering', async () => {
  const dir=mkdtempSync(join(tmpdir(),'newmark-source-'));const original=globalThis.fetch;let calls=0;
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR=dir;process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY='newmark';process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER='1';process.env.CRE_REQUIRE_FRESH_DETAILS='1';
  const rows=Array.from({length:70},(_,i)=>({id:`id-${i}`,slug:`slug-${i}`,properties:[{countryCode:'CA'}],retained:[null,false,'',[]]}));
  const raw=JSON.stringify({data:rows,total:70,context:{other:false}});
  globalThis.fetch=(async()=>{calls++;return new Response(raw);}) as typeof fetch;
  try {const {srcNewmark}=await import('../../../sources/newmark.js');const result=await srcNewmark('sale',1,false);assert.equal(calls,1);assert.equal(result.listings.length,0);
    const entry=JSON.parse(readFileSync(join(dir,readdirSync(dir)[0]),'utf8'));assert.equal(entry.receipt.requestVariant,'strict');assert.equal(gunzipSync(Buffer.from(entry.responseBodyBase64,'base64')).toString(),raw);
  } finally {globalThis.fetch=original;for(const k of ['DIR','SOURCE_KEY','SINGLE_WRITER'])delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${k}`];delete process.env.CRE_REQUIRE_FRESH_DETAILS;rmSync(dir,{recursive:true,force:true});}
});

test('CLI retains first private page after later terminal publication failure', () => {
  const dir=mkdtempSync(join(tmpdir(),'newmark-cli-retained-')); const capture=join(dir,'capture');
  const preload=join(dir,'preload.mjs'); const calls=join(dir,'calls');
  mkdirSync(capture,{mode:0o700});
  writeFileSync(preload,`import {appendFileSync} from 'node:fs'; globalThis.fetch=async()=>{appendFileSync(${JSON.stringify(calls)},'x');return new Response(JSON.stringify({total:200,data:Array.from({length:100},(_,i)=>({id:String(i)}))}));};`);
  try {
    const child=spawnSync(process.execPath,['--import','tsx','--import',preload,'collect.ts','--source','newmark','--transaction','both','--max-items','1','--out',join(dir,'out.json')],{cwd:process.cwd(),encoding:'utf8',timeout:20000,env:{...process.env,CRE_NATIVE_JSON_FEED_CAPTURE_DIR:capture,CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY:'newmark',CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER:'1',CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES:'1'}});
    assert.equal(child.status,1);assert.equal(readFileSync(calls,'utf8'),'xx');assert.equal(readdirSync(capture).length,1);assert.match(child.stderr,/native_capture_failed/);
  } finally {rmSync(dir,{recursive:true,force:true});}
});
