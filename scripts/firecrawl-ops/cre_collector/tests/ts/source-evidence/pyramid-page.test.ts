import { strict as assert } from 'node:assert';
import { mkdtempSync, readFileSync, readdirSync, rmSync } from 'node:fs';
import { gunzipSync } from 'node:zlib';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { test } from 'node:test';
import { publishPyramidPage, PyramidNativeCaptureError } from '../../../lib/source-evidence/pyramid-page.js';
import { fetchPyramidSnapshot, pyramidInventoryUrl } from '../../../sources/pyramid-brokerage.js';

test('capture failure stops snapshot without another request and leaves prior file', async () => {
  const dir = mkdtempSync(join(tmpdir(), 'pyramid-private-'));
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR = dir;
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER = '1';
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_MAX_PAGES = '1';
  const headers = new Headers({'X-WP-Total':'1','X-WP-TotalPages':'1'});
  let calls = 0;
  const row = {id:1,status:'publish',type:'pbc-listings',link:'https://www.pyramidbrokerage.com/listings/fixture/', 'sale-type':[246]};
  try {
    const fetcher = async () => { calls++; return new Response(JSON.stringify([row]), {status:200,headers}); };
    await assert.rejects(fetchPyramidSnapshot(fetcher, 2), PyramidNativeCaptureError);
    assert.equal(calls, 2); // second successful pass triggers publication failure, never an HTTP retry
    assert.equal(readdirSync(dir).length, 1);
  } finally {
    delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR; delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER;
    delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_MAX_PAGES; rmSync(dir,{recursive:true,force:true});
  }
});

test('oversize capture is typed and capture is off by default', () => {
  assert.doesNotThrow(() => publishPyramidPage('ignored',pyramidInventoryUrl(1),1,1,200,new Headers()));
  const dir = mkdtempSync(join(tmpdir(), 'pyramid-private-'));
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR=dir;process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER='1';
  try { assert.throws(() => publishPyramidPage('x'.repeat(16*1024*1024+1),pyramidInventoryUrl(1),1,1,200,new Headers()),PyramidNativeCaptureError); }
  finally { delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR; delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER; rmSync(dir,{recursive:true,force:true}); }
});


test('replacement-decoded text is retained honestly, without a wire-byte claim', () => {
  const dir = mkdtempSync(join(tmpdir(), 'pyramid-private-'));
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR=dir;process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER='1';
  try {
    const text = new TextDecoder().decode(Uint8Array.from([0x5b,0x22,0xff,0x22,0x5d]));
    publishPyramidPage(text,pyramidInventoryUrl(1),1,1,200,new Headers());
    const value = JSON.parse(readFileSync(join(dir,readdirSync(dir)[0]),'utf8'));
    assert.equal(gunzipSync(Buffer.from(value.responseBodyBase64,'base64')).toString('utf8'),text);
    assert.equal(value.provenance.utf8Decoding,'replacement_possible');
    assert.equal(value.provenance.representation,'collector_decoded_utf8_text_not_wire');
  } finally { delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR; delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER; rmSync(dir,{recursive:true,force:true}); }
});

test('normal source saves first response before header and JSON validation', async () => {
  const { srcPyramidBrokerage } = await import('../../../sources/pyramid-brokerage.js');
  const originalFetch = globalThis.fetch;
  for (const fixture of [
    { body: '[{"id":1}]', headers: new Headers(), error: /omitted valid X-WP-Total/ },
    { body: '{"password":"DUMMY_PRIVATE_VALUE"', headers: new Headers({'X-WP-Total':'1','X-WP-TotalPages':'1'}), error: /malformed JSON/ },
    { body: '{"unexpected":true}', headers: new Headers({'X-WP-Total':'1','X-WP-TotalPages':'1'}), error: /no rows/ },
  ]) {
    const dir = mkdtempSync(join(tmpdir(), 'pyramid-flow-'));
    process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR = dir;
    process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER = '1';
    let calls = 0;
    globalThis.fetch = (async () => { calls++; return new Response(fixture.body, {status:200, headers:fixture.headers}); }) as typeof fetch;
    try {
      await assert.rejects(srcPyramidBrokerage('sale', 1, true), fixture.error);
      assert.equal(calls, 1);
      const files = readdirSync(dir);
      assert.equal(files.length, 1);
      const saved = JSON.parse(readFileSync(join(dir, files[0]), 'utf8'));
      assert.equal(gunzipSync(Buffer.from(saved.responseBodyBase64, 'base64')).toString('utf8'), fixture.body);
    } finally {
      globalThis.fetch = originalFetch;
      delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR;
      delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER;
      rmSync(dir, {recursive:true, force:true});
    }
  }
});

test('normal source settles both in-flight pages and preserves earlier captures on typed failure', async () => {
  const { srcPyramidBrokerage } = await import('../../../sources/pyramid-brokerage.js');
  const dir = mkdtempSync(join(tmpdir(), 'pyramid-concurrent-'));
  const originalFetch = globalThis.fetch;
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR = dir;
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER = '1';
  process.env.CRE_PYRAMID_NATIVE_CAPTURE_MAX_PAGES = '2';
  const headers = new Headers({'X-WP-Total':'3','X-WP-TotalPages':'3'});
  const row = (id: number) => JSON.stringify([{id,status:'publish',type:'pbc-listings',link:`https://www.pyramidbrokerage.com/listings/fixture-${id}/`, 'sale-type':[246]}]);
  let release2!: () => void;
  let release3!: () => void;
  let started2!: () => void;
  let started3!: () => void;
  const started = [new Promise<void>(r => started2 = r), new Promise<void>(r => started3 = r)];
  const waits = [new Promise<void>(r => release2 = r), new Promise<void>(r => release3 = r)];
  const calls: number[] = [];
  globalThis.fetch = (async (input: string | URL | Request) => {
    const page = Number(new URL(String(input)).searchParams.get('page'));
    calls.push(page);
    if (page === 2) { started2(); await waits[0]; }
    if (page === 3) { started3(); await waits[1]; }
    return new Response(row(page), {status:200, headers});
  }) as typeof fetch;
  try {
    const result = srcPyramidBrokerage('sale', 1, true);
    await Promise.all(started);
    release2();
    await new Promise<void>(r => setImmediate(r));
    assert.equal(readdirSync(dir).length, 2);
    release3();
    await assert.rejects(result, PyramidNativeCaptureError);
    assert.deepEqual(calls, [1, 2, 3]);
    assert.deepEqual(readdirSync(dir).map(name => Number(name.split('-')[2])).sort(), [1, 2]);
  } finally {
    release2(); release3(); globalThis.fetch = originalFetch;
    delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_DIR;
    delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_SINGLE_WRITER;
    delete process.env.CRE_PYRAMID_NATIVE_CAPTURE_MAX_PAGES;
    rmSync(dir, {recursive:true, force:true});
  }
});
