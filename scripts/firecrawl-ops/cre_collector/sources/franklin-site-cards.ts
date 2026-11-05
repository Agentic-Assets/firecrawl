import * as cheerio from 'cheerio';
import { parseFranklinPage } from './franklin-street-evidence.js';
import type { Tx } from '../types.js';

type Observation = ReturnType<typeof parseFranklinPage>;

/** Offline projection only: no admission, persistence or completeness claim. */
function projectObservation(page: Observation) {
  const expectedPage = `https://franklinst.com/properties/for-${page.transaction}/`;
  if (page.schema !== 'franklin-street-page-observation-v1' || page.coverage !== 'partial_unknown'
      || page.sourceUrl !== expectedPage || !Number.isFinite(Date.parse(page.observedAt))
      || !/^[a-f0-9]{64}$/.test(page.decodedHtmlSha256)) throw new Error('invalid Franklin page observation');
  const prefix = `/properties/for-${page.transaction}/`;
  const candidates = page.cards.map((card, ordinal) => {
    const id = card.attributes['data-listing-id'];
    const offer = page.transaction === 'sale' ? 'FOR_SALE' : 'FOR_LEASE';
    const $ = cheerio.load(card.html);
    const urls = [...new Set($('a[href]').toArray().map(a => $(a).attr('href')).filter((x): x is string => typeof x === 'string'))];
    const valid = urls.filter(href => href.startsWith(prefix) && /^\/properties\/for-(?:sale|lease)\/[a-z0-9]+(?:[-a-z0-9]*[a-z0-9])?\/?$/.test(href));
    const reason = !id || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/i.test(id) ? 'invalid_id'
      : card.attributes['data-offer-type'] !== offer ? 'transaction_mismatch'
      : valid.length !== 1 ? 'ambiguous_url'
      : Buffer.byteLength(JSON.stringify(card), 'utf8') > 65536 ? 'card_too_large' : null;
    return { ordinal, id: id?.toLowerCase(), url: valid[0] ? new URL(valid[0], expectedPage).href : undefined, reason, card };
  });
  const duplicates = (key: 'id' | 'url') => {
    const counts = new Map<string, number>();
    for (const c of candidates) if (c[key]) counts.set(c[key]!, (counts.get(c[key]!) ?? 0) + 1);
    return counts;
  };
  const ids = duplicates('id');
  const urls = duplicates('url');
  const listings: unknown[] = [];
  const held: unknown[] = [];
  for (const c of candidates) {
    const reason = c.reason ?? ((ids.get(c.id!) ?? 0) > 1 ? 'duplicate_id' : (urls.get(c.url!) ?? 0) > 1 ? 'duplicate_url' : null);
    if (reason) { held.push({ ordinal: c.ordinal, reason, card: c.card }); continue; }
    listings.push({
      sourceKey: 'franklin-street', id: `franklin-site:${page.transaction}:${c.id}`,
      url: c.url, canonicalUrl: c.url, transactionMode: page.transaction,
      name: c.card.attributes['data-listing-name'] || undefined,
      state: c.card.attributes['data-state'] || undefined,
      latitude: c.card.attributes['data-lat'] || undefined,
      longitude: c.card.attributes['data-lng'] || undefined,
      preserveChildCollections: true,
      franklinSiteObservation: { coverage: 'partial_unknown', sourceUrl: page.sourceUrl,
        observedAt: page.observedAt, decodedHtmlSha256: page.decodedHtmlSha256,
        ordinal: c.ordinal, card: c.card },
    });
  }
  return { coverage: 'partial_unknown' as const, listings, held };
}
/** Derive each projection from the exact decoded page supplied by its caller.
 * The caller must separately bind the bytes, clock, generation and target.
 */
export function projectFranklinSiteCards(html: string, observedAt: string, tx: Tx) {
  if (typeof html !== 'string' || !['sale', 'lease'].includes(tx)) throw new Error('invalid Franklin page input');
  return projectObservation(parseFranklinPage(html, observedAt, tx));
}
