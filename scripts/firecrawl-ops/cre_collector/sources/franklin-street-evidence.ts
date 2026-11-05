import { createHash } from 'node:crypto';
import * as cheerio from 'cheerio';
import { observationNow } from '../lib/observation-clock.js';
import { readBoundedUtf8, settleResponseBodyRead, cancelResponseBody } from '../lib/response-body.js';
import { RetainedSourceError } from '../lib/source-observations.js';
import { NativeEvidenceTerminalError } from '../lib/source-evidence/terminal-error.js';
import type { Tx } from '../types.js';

const PAGE_URLS: Record<Tx, string> = {
  sale: 'https://franklinst.com/properties/for-sale/',
  lease: 'https://franklinst.com/properties/for-lease/',
};
const MAX_BYTES = 8 * 1024 * 1024;
const TIMEOUT_MS = 15000;

/** Preserve occurrences rather than deduplicating or assigning canonical identity. */
export function parseFranklinPage(html: string, observedAt: string, tx: Tx) {
  const $ = cheerio.load(html);
  const cards = $('article[data-listing-card="true"]').toArray().map(element => ({
    attributes: { ...element.attribs },
    html: $.html(element),
  }));
  return {
    schema: 'franklin-street-page-observation-v1',
    coverage: 'partial_unknown',
    transaction: tx,
    sourceUrl: PAGE_URLS[tx],
    observedAt,
    decodedHtmlSha256: createHash('sha256').update(html, 'utf8').digest('hex'),
    cards,
  };
}

export async function captureFranklinPage(tx: Tx, fetchImpl: typeof fetch = fetch) {
  const controller = new AbortController();
  const deadlineAt = performance.now() + TIMEOUT_MS;
  const timer = setTimeout(() => controller.abort(), TIMEOUT_MS);
  try {
    const response = await fetchImpl(PAGE_URLS[tx], { signal: controller.signal, redirect: 'manual', headers: { accept: 'text/html' } });
    if (!response.ok || !/^text\/html(?:\s*;|$)/i.test(response.headers.get('content-type') ?? '')) {
      if (!await cancelResponseBody(response.body, 1000)) throw new NativeEvidenceTerminalError('Franklin response cleanup unsettled');
      throw new Error('Franklin landing page unavailable');
    }
    if (!response.body) throw new Error('Franklin landing page empty');
    const reader = response.body.getReader();
    try {
      const html = await readBoundedUtf8(reader, { maxBytes: MAX_BYTES, signal: controller.signal, deadlineAt });
      return { ...parseFranklinPage(html, observationNow(), tx), decodedHtml: html, httpStatus: response.status, contentType: response.headers.get('content-type') };
    } catch (error) {
      controller.abort();
      if (!await settleResponseBodyRead(reader, error, 1000)) throw new NativeEvidenceTerminalError('Franklin response read cleanup unsettled', { cause: error });
      throw error;
    } finally {
      try { reader.releaseLock(); } catch { /* preserve failure */ }
    }
  } finally { clearTimeout(timer); }
}

export async function collectFranklinWithEvidence<T>(
  tx: Tx, monitor: boolean, collect: () => Promise<T>, capture: (tx: Tx) => Promise<ReturnType<typeof parseFranklinPage>> = captureFranklinPage,
): Promise<T> {
  try { return await collect(); }
  catch (error) {
    if (monitor || !(error instanceof Error) || error instanceof NativeEvidenceTerminalError) throw error;
    let observation: Awaited<ReturnType<typeof capture>>;
    try { observation = await capture(tx); }
    catch (captureError) { if (captureError instanceof NativeEvidenceTerminalError) throw captureError; throw error; }
    throw new RetainedSourceError(error, [...(error instanceof RetainedSourceError ? error.retainedObservations : []), observation]);
  }
}
