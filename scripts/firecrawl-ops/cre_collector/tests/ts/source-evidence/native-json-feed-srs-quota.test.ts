import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { NativeJsonFeedCaptureError, publishNativeJsonFeed } from '../../../lib/source-evidence/native-json-feed.js';

test('explicit SRS quota permits 256 individual pages but never 257 or an over-budget write', () => {
  const dir=mkdtempSync(join(tmpdir(),'srs-native-quota-'));
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR=dir;
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY='srs';
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER='1';
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES='256';
  const publish=(page:number)=>publishNativeJsonFeed({profile:'srs_search_post_v1',sourceKey:'srs',requestVariant:'standard',
    requestBody:'{}',page,offset:page*12,status:200,body:'{"properties":[]}'});
  try {
    for(let page=0;page<256;page++){
      if(page===128){
        process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_BYTES='1';
        assert.throws(()=>publish(page),NativeJsonFeedCaptureError);
        assert.equal(readdirSync(dir).length,128);
        delete process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_BYTES;
      }
      publish(page);
      if([128,129,177,256].includes(page+1)) assert.equal(readdirSync(dir).length,page+1);
    }
    const before=readdirSync(dir).sort();
    const entry129=before.find(name=>name.startsWith('page-129-'));
    assert(entry129);assert.equal(JSON.parse(readFileSync(join(dir,entry129),'utf8')).receipt.sequence,129);
    assert.throws(()=>publish(256),NativeJsonFeedCaptureError);
    assert.deepEqual(readdirSync(dir).sort(),before);
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES='257';
    assert.throws(()=>publish(256),NativeJsonFeedCaptureError);
    assert.deepEqual(readdirSync(dir).sort(),before);
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES='256';
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_BYTES='1';
    assert.throws(()=>publish(256),NativeJsonFeedCaptureError);
    assert.deepEqual(readdirSync(dir).sort(),before);
    delete process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_BYTES;
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY='kidder-mathews';
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES='129';
    assert.throws(()=>publishNativeJsonFeed({profile:'kidder_paged_post_v1',sourceKey:'kidder-mathews',requestVariant:'standard',
      requestBody:'{}',page:0,offset:0,status:200,body:'{}'}),NativeJsonFeedCaptureError);
    assert.deepEqual(readdirSync(dir).sort(),before);
    process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY='hanley';
    assert.throws(()=>publishNativeJsonFeed({profile:'hanley_embedded_array_v1',sourceKey:'hanley',requestVariant:'firecrawl',
      requestBody:null,page:null,offset:null,status:null,body:'[]'}),NativeJsonFeedCaptureError);
    assert.deepEqual(readdirSync(dir).sort(),before);
  } finally {
    for(const k of ['DIR','SOURCE_KEY','SINGLE_WRITER','MAX_PAGES','MAX_BYTES']) delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${k}`];
    rmSync(dir,{recursive:true,force:true});
  }
});
