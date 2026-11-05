import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseFranklinPage } from '../../../sources/franklin-street-evidence.js';
import { projectFranklinSiteCards } from '../../../sources/franklin-site-cards.js';

const uuid = '12345678-1234-5123-8123-123456789abc';
function card(id: string, slug: string, offer = 'FOR_SALE') {
  return `<article data-listing-card="true" data-listing-id="${id}" data-offer-type="${offer}" data-listing-name="Property" data-listing-price="25000000" data-listing-size="71418" data-lat="30.123"><a href="/properties/for-${offer === 'FOR_SALE' ? 'sale' : 'lease'}/${slug}">Property</a>Subject to Offer 15,985 SF</article>`;
}
test('projects distinct transaction IDs and retains contradictory evidence without promotion', () => {
  for (const tx of ['sale', 'lease'] as const) {
    const page = parseFranklinPage(card(uuid, 'a---b', tx === 'sale' ? 'FOR_SALE' : 'FOR_LEASE'), '2026-11-04T12:00:00Z', tx);
    const result = projectFranklinSiteCards(page);
    assert.equal(result.coverage, 'partial_unknown');
    assert.equal(result.held.length, 0);
    const row = result.listings[0] as any;
    assert.equal(row.id, `franklin-site:${tx}:${uuid}`);
    assert.equal(row.url, `https://franklinst.com/properties/for-${tx}/a---b`);
    assert.equal(row.franklinSiteObservation.card.attributes['data-listing-price'], '25000000');
    assert.match(row.franklinSiteObservation.card.html, /Subject to Offer 15,985 SF/);
    assert.equal(row.salePriceUsd, undefined);
    assert.equal(row.buildingSizeSqft, undefined);
  }
});
test('holds duplicate identity and URL occurrences and retains their evidence', () => {
  const other = '87654321-4321-5432-8432-abcdefabcdef';
  const page = parseFranklinPage(card(uuid, 'a') + card(uuid, 'b') + card(other, 'b') + card('bad', 'c'), '2026-11-04T12:00:00Z', 'sale');
  const result = projectFranklinSiteCards(page);
  assert.equal(result.listings.length, 0);
  assert.deepEqual(result.held.map((x: any) => x.reason), ['duplicate_id', 'duplicate_id', 'duplicate_url', 'invalid_id']);
});
test('refuses forged page metadata and holds transaction mismatches and unsafe paths', () => {
  const page = parseFranklinPage(card(uuid, 'a', 'FOR_LEASE').replace('/for-lease/', '/for-sale/'), '2026-11-04T12:00:00Z', 'sale');
  assert.equal((projectFranklinSiteCards(page).held[0] as any).reason, 'transaction_mismatch');
  assert.throws(() => projectFranklinSiteCards({ ...page, sourceUrl: 'https://example.com/' }));
});