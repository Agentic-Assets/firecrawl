import { test } from 'node:test';
import assert from 'node:assert/strict';
import { spawnSync } from 'node:child_process';
import { parseFranklinPage } from '../../../sources/franklin-street-evidence.js';
import { projectFranklinFailureArtifact } from '../../../sources/franklin-site-failure-artifact.js';

function fixture() {
  const observedAt = '2026-11-04T12:00:01Z';
  const decodedHtml = '<article data-listing-card="true" data-listing-id="12345678-1234-5123-8123-123456789abc" data-offer-type="FOR_SALE"><a href="/properties/for-sale/a">A</a></article>';
  return { runMeta: { mode: 'full', startedAt: '2026-11-04T12:00:00Z', finishedAt: '2026-11-04T12:00:02Z', freshness: {generationId:'run-1'} },
    sources: [{sourceKey:'franklin-street', transaction:'sale', error:'feed failed',
      retainedObservations: [{...parseFranklinPage(decodedHtml, observedAt, 'sale'), decodedHtml, httpStatus:200, contentType:'text/html; charset=utf-8'}]}],
    listings: [] as any[], brokers: [] as any[], totalListings: 0 };
}
test('rederives partial identity from the retained page bytes', () => {
  const result = projectFranklinFailureArtifact(fixture(), 'run-1');
  assert.equal(result.listings.length, 1);
  assert.equal(result.held.length, 0);
  assert.equal(result.coverage, 'partial_unknown');
});
test('rejects mutated cards, hash, clock, transaction, generation and mixed scope', () => {
  const mutations: Array<(x: any) => void> = [
    x => {x.sources[0].retainedObservations[0].cards[0].attributes['data-listing-id']='forged';},
    x => {x.sources[0].retainedObservations[0].decodedHtmlSha256='0'.repeat(64);},
    x => {x.sources[0].retainedObservations[0].observedAt='2026-11-03T00:00:00Z';},
    x => {x.sources[0].transaction='lease';},
    x => {x.runMeta.freshness.generationId='other';},
    x => {x.sources.push({sourceKey:'other', transaction:'sale'});},
    x => {x.sources[0].retainedObservations.push(x.sources[0].retainedObservations[0]);},
  ];
  for (const mutate of mutations) {
    const input = fixture(); mutate(input);
    assert.throws(() => projectFranklinFailureArtifact(input, 'run-1'));
  }
});

test('private bridge reads bounded input and never prints refused artifact content', () => {
  const argv = ['--import', 'tsx', 'sources/franklin-site-bridge.ts', 'run-1'];
  const okay = spawnSync(process.execPath, argv, { input: JSON.stringify(fixture()), encoding: 'utf8' });
  assert.equal(okay.status, 0);
  assert.equal(JSON.parse(okay.stdout).listings.length, 1);
  const bad = spawnSync(process.execPath, argv, { input: '{"private":"DO_NOT_PRINT"', encoding: 'utf8' });
  assert.notEqual(bad.status, 0);
  assert.equal(bad.stdout, '');
  assert.doesNotMatch(bad.stderr, /DO_NOT_PRINT/);
});


test('mixed source retains every successful Buildout peer alongside fallback cards', () => {
  const input = fixture();
  input.sources.push({sourceKey:'franklin-street', transaction:'lease', supported:true,
    truncated:false, listingsCollected:2} as any);
  input.listings.push(
    {sourceKey:'franklin-street', transactionMode:'lease', id:'buildout-1', url:'https://buildout.example/1'},
    {sourceKey:'franklin-street', transactionMode:'lease', id:'buildout-2', url:'https://buildout.example/2'},
  );
  input.brokers.push({name:'Broker'});
  input.totalListings = 2;
  const result = projectFranklinFailureArtifact(input, 'run-1');
  assert.equal(result.listings.length, 1);
  assert.deepEqual(result.buildout.listings, input.listings);
  assert.deepEqual(result.buildout.brokers, input.brokers);
  assert.deepEqual(result.buildout.transactions, [{transaction:'lease', listingsCollected:2}]);
  (input.sources[1] as any).listingsCollected = 3;
  assert.throws(() => projectFranklinFailureArtifact(input, 'run-1'));
});
