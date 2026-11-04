import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseFranklinPage, captureFranklinPage, collectFranklinWithEvidence } from '../../../sources/franklin-street-evidence.js';
import { RetainedSourceError } from '../../../lib/source-observations.js';

test('page keeps every card occurrence and original values with unknown completeness', () => {
  const result = parseFranklinPage(`<article data-listing-card="true" data-listing-id="x" data-listing-price="" data-offer-type="UNKNOWN"><a href="/properties/one">A</a></article><article data-listing-card="true" data-listing-id="x" data-lat="bad">B</article><article data-listing-card="true">C</article>`, '2026-01-01T00:00:00Z');
  assert.equal(result.coverage, 'partial_unknown');
  assert.equal(result.cards.length, 3);
  assert.equal(result.cards[0].attributes['data-listing-price'], '');
  assert.equal(result.cards[1].attributes['data-lat'], 'bad');
  assert.match(result.cards[0].html, /href="\/properties\/one"/);
  assert.equal(result.cards[2].attributes['data-listing-id'], undefined);
});

test('source error survives evidence capture and empty or failed capture', async () => {
  const failure = new Error('Buildout failed');
  const run = async () => { throw failure; };
  const page = parseFranklinPage('<article data-listing-card="true" data-listing-id="a"></article>', '2026-01-01T00:00:00Z');
  await assert.rejects(collectFranklinWithEvidence('sale', false, run, async () => page), (e: unknown) => e instanceof RetainedSourceError && e.message === failure.message && e.retainedObservations[0] === page);
  await assert.rejects(collectFranklinWithEvidence('sale', false, run, async () => { throw Error('unavailable'); }), e => e === failure);
  await assert.rejects(collectFranklinWithEvidence('sale', false, run, async () => parseFranklinPage('<html></html>', '2026-01-01T00:00:00Z')), (e: unknown) => e instanceof RetainedSourceError && (e.retainedObservations[0] as { cards: unknown[] }).cards.length === 0);
  await assert.rejects(collectFranklinWithEvidence('lease', false, run, async () => { throw Error('must not fetch'); }), e => e === failure);
  await assert.rejects(collectFranklinWithEvidence('sale', true, run, async () => { throw Error('must not fetch'); }), e => e === failure);
});

test('successful source does not perform fallback', async () => {
  const result = { listings: ['healthy'] };
  assert.equal(await collectFranklinWithEvidence('sale', false, async () => result, async () => { throw Error('must not fetch'); }), result);
});

test('capture rejects redirect, non-html, and failed responses without claiming page evidence', async () => {
  for (const response of [new Response(null, {status: 302, headers: {location: 'https://other.example/'}}), new Response('error', {status: 500}), new Response('{}', {status: 200, headers: {'content-type': 'application/json'}})]) {
    await assert.rejects(captureFranklinPage(async () => response));
  }
});
test('successful capture retains response metadata and literal card values', async () => {
  const page = await captureFranklinPage(async () => new Response('<article data-listing-card="true" data-listing-price="123.00" data-lat="30.5">x</article>', { status: 200, headers: {'content-type': 'text/html; charset=UTF-8'} }));
  assert.equal(page.sourceUrl, 'https://www.franklinst.com/properties/for-sale');
  assert.match(page.decodedHtml, /data-listing-price="123.00"/);
  assert.equal(page.httpStatus, 200);
  assert.equal(page.contentType, 'text/html; charset=UTF-8');
  assert.equal(page.cards[0].attributes['data-listing-price'], '123.00');
  assert.equal(page.cards[0].attributes['data-lat'], '30.5');
});
test('terminal Buildout custody failures remain terminal and do not fetch', async () => {
  const { BuildoutNativeCaptureError, BuildoutRepresentationError } = await import('../../../lib/source-evidence/buildout-page.js');
  for (const error of [new BuildoutNativeCaptureError(), new BuildoutRepresentationError()]) {
    let called = false;
    await assert.rejects(collectFranklinWithEvidence('sale', false, async () => { throw error; }, async () => { called = true; return parseFranklinPage('', '2026-01-01T00:00:00Z'); }), e => e === error);
    assert.equal(called, false);
  }
});

test('prior retained observations survive another failed source capture', async () => {
  const original = new RetainedSourceError(new Error('feed failed'), [{prior: true}]);
  const page = parseFranklinPage('<article data-listing-card="true"></article>', '2026-01-01T00:00:00Z');
  await assert.rejects(collectFranklinWithEvidence('sale', false, async () => { throw original; }, async () => page), (e: unknown) => e instanceof RetainedSourceError && e.retainedObservations[0] === original.retainedObservations[0] && e.retainedObservations[1] === page);
});

test('unsettled rejected response remains a terminal capture error', async () => {
  const { NativeEvidenceTerminalError } = await import('../../../lib/source-evidence/terminal-error.js');
  const body = new ReadableStream({ cancel() { return Promise.reject(Error('cleanup failed')); } });
  await assert.rejects(collectFranklinWithEvidence('sale', false, async () => { throw Error('feed failed'); }, () => captureFranklinPage(async () => new Response(body, {status: 500}))), e => e instanceof NativeEvidenceTerminalError);
});
test('unsettled oversized response read is terminal', async () => {
  const { NativeEvidenceTerminalError } = await import('../../../lib/source-evidence/terminal-error.js');
  const body = new ReadableStream<Uint8Array>({ start(controller) { controller.enqueue(new Uint8Array(8 * 1024 * 1024 + 1)); }, cancel() { return Promise.reject(Error('cleanup failed')); } });
  await assert.rejects(captureFranklinPage(async () => new Response(body, {status: 200, headers: {'content-type': 'text/html'}})), e => e instanceof NativeEvidenceTerminalError);
});
