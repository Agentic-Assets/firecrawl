import { strict as assert } from 'node:assert';
import { mkdtempSync, mkdirSync, readdirSync, rmSync } from 'node:fs';
import { join } from 'node:path';
import { tmpdir } from 'node:os';
import { test } from 'node:test';
import { NativeJsonFeedCaptureError } from '../../../lib/source-evidence/native-json-feed.js';
import { srsFetchAll } from '../../../sources/srs.js';
import { kidderFetchAll } from '../../../sources/kidder-mathews.js';

test('non-strict page catch propagates capture failure for both POST feeds', async () => {
  const root=mkdtempSync(join(tmpdir(),'native-feed-nonstrict-'));const original=globalThis.fetch;
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER='1';
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES='1';
  delete process.env.CRE_REQUIRE_FRESH_DETAILS;
  try {
    for(const source of ['srs','kidder-mathews']) {
      const dir=join(root,source);mkdirSync(dir,{mode:0o700});
      process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR=dir;process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY=source;
      let calls=0;
      globalThis.fetch=(async (_url:unknown, options:RequestInit)=>{
        calls++;const req=JSON.parse(String(options.body));
        if(source==='srs') return new Response(JSON.stringify({total:24,properties:Array.from({length:12},(_,i)=>({id:i+req.query.offset}))}));
        return new Response(JSON.stringify({totalResultCount:100,results:Array.from({length:50},(_,i)=>({listing_key:String(i+req.startIndex)}))}));
      }) as typeof fetch;
      await assert.rejects(source==='srs'?srsFetchAll(Infinity):kidderFetchAll(Infinity),NativeJsonFeedCaptureError);
      assert.equal(calls,2);assert.equal(readdirSync(dir).length,1);
    }
  } finally {
    globalThis.fetch=original;
    for(const k of ['DIR','SOURCE_KEY','SINGLE_WRITER','MAX_PAGES']) delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${k}`];
    rmSync(root,{recursive:true,force:true});
  }
});
