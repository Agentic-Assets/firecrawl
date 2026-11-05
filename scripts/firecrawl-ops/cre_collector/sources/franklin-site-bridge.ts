/** Private stdin/stdout bridge. It never opens a network connection or a database. */
import { projectFranklinFailureArtifact } from './franklin-site-failure-artifact.js';

const MAX_INPUT = 16 * 1024 * 1024;
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
  process.stdout.write(JSON.stringify(result));
}
main().catch(() => {
  process.stderr.write('Franklin artifact projection refused\n');
  process.exitCode = 1;
});