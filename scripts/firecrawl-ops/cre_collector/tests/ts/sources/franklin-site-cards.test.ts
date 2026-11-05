import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createHash } from 'node:crypto';
import { projectFranklinSiteCards } from '../../../sources/franklin-site-cards.js';

const uuid = '12345678-1234-5123-8123-123456789abc';
function card(id: string, slug: string, offer = 'FOR_SALE') {
  return `<article data-listing-card="true" data-listing-id="${id}" data-offer-type="${offer}" data-listing-name="Property" data-listing-price="25000000" data-listing-size="71418" data-lat="30.123" data-lng="-97.123" data-state="TX"><a href="/properties/for-${offer === 'FOR_SALE' ? 'sale' : 'lease'}/${slug}">Property</a>Subject to Offer 15,985 SF</article>`;
}
test('projects distinct transaction IDs and retains contradictory evidence without promotion', () => {
  for (const tx of ['sale', 'lease'] as const) {
    const html = card(uuid, 'a---b', tx === 'sale' ? 'FOR_SALE' : 'FOR_LEASE');
    const result = projectFranklinSiteCards(html, '2026-11-04T12:00:00Z', tx);
    assert.equal(result.coverage, 'partial_unknown');
    assert.equal(result.held.length, 0);
    const row = result.listings[0] as any;
    assert.equal(row.id, `franklin-site:${tx}:${uuid}`);
    assert.equal(row.url, `https://franklinst.com/properties/for-${tx}/a---b`);
    assert.equal(row.franklinSiteObservation.decodedHtmlSha256, createHash('sha256').update(html).digest('hex'));
    assert.equal(row.franklinSiteObservation.card.attributes['data-listing-price'], '25000000');
    assert.match(row.franklinSiteObservation.card.html, /Subject to Offer 15,985 SF/);
    assert.equal(row.latitude, '30.123');
    assert.equal(row.longitude, '-97.123');
    assert.equal(row.state, 'TX');
    assert.equal(row.salePriceUsd, undefined);
    assert.equal(row.buildingSizeSqft, undefined);
  }
});
test('holds duplicate identity and URL occurrences and retains their evidence', () => {
  const other = '87654321-4321-5432-8432-abcdefabcdef';
  const result = projectFranklinSiteCards(card(uuid, 'a') + card(uuid, 'b') + card(other, 'b') + card('bad', 'c'), '2026-11-04T12:00:00Z', 'sale');
  assert.equal(result.listings.length, 0);
  assert.deepEqual(result.held.map((x: any) => x.reason), ['duplicate_id', 'duplicate_id', 'duplicate_url', 'invalid_id']);
});
test('refuses invalid input clocks and holds transaction mismatches', () => {
  const html = card(uuid, 'a', 'FOR_LEASE').replace('/for-lease/', '/for-sale/');
  assert.equal((projectFranklinSiteCards(html, '2026-11-04T12:00:00Z', 'sale').held[0] as any).reason, 'transaction_mismatch');
  assert.throws(() => projectFranklinSiteCards(html, 'invalid', 'sale'));
});
test('retains a card over 64 KiB and its valid peer', () => {
  const other = '87654321-4321-5432-8432-abcdefabcdef';
  const result = projectFranklinSiteCards(card(uuid, 'a') + card(other, 'b').replace('</article>', 'x'.repeat(65536) + '</article>'), '2026-11-04T12:00:00Z', 'sale');
  assert.equal(result.listings.length, 2);
  assert.equal(result.held.length, 0);
  assert.ok((result.listings[1] as any).franklinSiteObservation.card.html.length > 65536);
});

test('holds both cards sharing a URL and retains their raw evidence alongside a valid peer', () => {
  const other = '87654321-4321-5432-8432-abcdefabcdef';
  const peer = 'aaaaaaaa-bbbb-5ccc-8ddd-eeeeeeeeeeee';
  const html = card(uuid, 'same') + card(other, 'same') + card(peer, 'unique');
  const result = projectFranklinSiteCards(html, '2026-11-04T12:00:00Z', 'sale');
  assert.deepEqual(result.held.map((x: any) => x.reason), ['duplicate_url', 'duplicate_url']);
  assert.deepEqual(result.held.map((x: any) => x.card.html), [card(uuid, 'same'), card(other, 'same')]);
  assert.equal((result.listings[0] as any).id, `franklin-site:sale:${peer}`);
});
