import { strict as assert } from 'node:assert';
import { mkdtempSync, readdirSync, rmSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { NativeJsonFeedCaptureError, publishNativeJsonFeed } from '../../../lib/source-evidence/native-json-feed.js';

test('SRS without explicit quota retains old 128-page default', () => {
  const dir=mkdtempSync(join(tmpdir(),'srs-default-quota-'));
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_DIR=dir;process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SOURCE_KEY='srs';
  process.env.CRE_NATIVE_JSON_FEED_CAPTURE_SINGLE_WRITER='1';delete process.env.CRE_NATIVE_JSON_FEED_CAPTURE_MAX_PAGES;
  const publish=(page:number)=>publishNativeJsonFeed({profile:'srs_search_post_v1',sourceKey:'srs',requestVariant:'strict',
    requestBody:'{}',page,offset:page*12,status:200,body:'[]'});
  try {for(let i=0;i<128;i++)publish(i);assert.throws(()=>publish(128),NativeJsonFeedCaptureError);assert.equal(readdirSync(dir).length,128);}
  finally {for(const k of ['DIR','SOURCE_KEY','SINGLE_WRITER','MAX_PAGES'])delete process.env[`CRE_NATIVE_JSON_FEED_CAPTURE_${k}`];rmSync(dir,{recursive:true,force:true});}
});
