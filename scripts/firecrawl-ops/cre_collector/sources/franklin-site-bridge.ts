/** Private stdin/stdout bridge. It never opens a network connection or a database. */
import { projectFranklinFailureArtifact } from './franklin-site-failure-artifact.js';

const MAX_INPUT = 64 * 1024 * 1024;
async function main() {
  const generationId = process.argv[2];
  if (!generationId || generationId.length > 128) throw new Error('invalid generation');
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of process.stdin) {
    const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += bytes.length;
    if (size > MAX_INPUT) throw new Error('input too large');
    chunks.push(bytes);
  }
  const raw = Buffer.concat(chunks);
  const text = new TextDecoder('utf-8', { fatal: true }).decode(raw);
  const result = projectFranklinFailureArtifact(JSON.parse(text), generationId);
  // Return only bounded references; the owner restores exact bytes from its
  // immutable input after this process has independently rederived each card.
  const listings = result.listings.map(value => {
    const row = value as Record<string, any>;
    const { card: _card, ...observation } = row.franklinSiteObservation;
    return { ...row, franklinSiteObservation: observation };
  });
  const held = result.held.map(value => {
    const row = value as { transaction: string; card: Record<string, any> };
    const { card: _card, ...reference } = row.card;
    return { transaction: row.transaction, card: reference };
  });
  process.stdout.write(JSON.stringify({ ...result, listings, held,
    buildout: { transactions: result.buildout.transactions } }));
}
main().catch(() => {
  process.stderr.write('Franklin artifact projection refused\n');
  process.exitCode = 1;
});