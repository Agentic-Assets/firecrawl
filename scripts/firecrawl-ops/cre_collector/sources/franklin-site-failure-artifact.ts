import { parseFranklinPage } from './franklin-street-evidence.js';
import { projectFranklinSiteCards } from './franklin-site-cards.js';
import type { Tx } from '../types.js';

type RecordValue = Record<string, unknown>;
function object(value: unknown): RecordValue {
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('invalid Franklin artifact object');
  return value as RecordValue;
}

/** Offline verification; the refresh owner must bind the original artifact and target. */
export function projectFranklinFailureArtifact(value: unknown, generationId: string) {
  const artifact = object(value);
  const meta = object(artifact.runMeta);
  const freshness = object(meta.freshness);
  if (!generationId || freshness.generationId !== generationId || meta.mode !== 'full'
      || !Array.isArray(artifact.sources) || artifact.sources.length === 0) throw new Error('invalid Franklin artifact scope');
  const started = Date.parse(String(meta.startedAt));
  const finished = Date.parse(String(meta.finishedAt));
  if (!Number.isFinite(started) || !Number.isFinite(finished) || started > finished) throw new Error('invalid Franklin artifact clock');
  const seen = new Set<string>();
  const listings: unknown[] = [];
  const held: unknown[] = [];
  const pages: unknown[] = [];
  for (const entryValue of artifact.sources) {
    const entry = object(entryValue);
    if (entry.sourceKey !== 'franklin-street' || !['sale', 'lease'].includes(String(entry.transaction))
        || seen.has(String(entry.transaction))) throw new Error('mixed or duplicate Franklin source scope');
    const tx = entry.transaction as Tx;
    seen.add(tx);
    if (!entry.error) continue;
    if (!Array.isArray(entry.retainedObservations)) continue;
    const observations = entry.retainedObservations.filter(x => {
      const item = object(x);
      return item.schema === 'franklin-street-page-observation-v1';
    });
    if (observations.length !== 1) throw new Error('invalid Franklin page count');
    const observation = object(observations[0]);
    if (observation.transaction !== tx || observation.coverage !== 'partial_unknown'
        || observation.httpStatus !== 200 || typeof observation.contentType !== 'string'
        || !/^text\/html(?:\s*;|$)/i.test(observation.contentType)
        || typeof observation.decodedHtml !== 'string' || Buffer.byteLength(observation.decodedHtml, 'utf8') > 8 * 1024 * 1024
        || typeof observation.observedAt !== 'string') throw new Error('invalid Franklin page capture');
    const observed = Date.parse(observation.observedAt);
    if (!Number.isFinite(observed) || observed < started || observed > finished + 300000) throw new Error('Franklin page clock outside attempt');
    const derived = parseFranklinPage(observation.decodedHtml, observation.observedAt, tx);
    if (observation.sourceUrl !== derived.sourceUrl || observation.decodedHtmlSha256 !== derived.decodedHtmlSha256
        || JSON.stringify(observation.cards) !== JSON.stringify(derived.cards)) throw new Error('Franklin page differs from retained bytes');
    const projected = projectFranklinSiteCards(observation.decodedHtml, observation.observedAt, tx);
    listings.push(...projected.listings.map(item => ({ ...object(item), franklinSiteGenerationId: generationId })));
    held.push(...projected.held.map(card => ({ transaction: tx, card })));
    pages.push({ transaction: tx, sourceUrl: derived.sourceUrl, observedAt: observation.observedAt,
      decodedHtmlSha256: derived.decodedHtmlSha256, observedCards: derived.cards.length });
  }
  if (!pages.length) throw new Error('no Franklin fallback pages');
  return { coverage: 'partial_unknown' as const, generationId, pages, listings, held };
}