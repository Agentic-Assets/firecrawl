import { formatFromBytes, formatFromExtension, toMarkdownBytes } from '@firecrawl/anydoc';

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
try {
  const bytes = Buffer.concat(chunks);
  const detected = formatFromBytes(bytes);
  const extensionFormat = formatFromExtension(process.argv[2]);
  const hint = process.argv[3] === 'csv' ? 'csv' : null;
  const blocked = process.argv[3] === 'unresolved';
  const conflict = Boolean(hint && (detected ?? extensionFormat) && (detected ?? extensionFormat) !== hint);
  const fallback = blocked || conflict ? null : extensionFormat ?? hint;
  const format = detected ?? fallback;
  const source = { format: format ?? null, format_detection: detected ? 'content' : fallback ? (extensionFormat ? 'extension' : 'source_hint') : 'unknown' };
  if (hint && (detected || extensionFormat)) source.format_hint_conflict = conflict;
  try {
    const text = await toMarkdownBytes(bytes, format ?? undefined);
    process.stdout.write(JSON.stringify({ text, source }));
  } catch (error) {
    process.stdout.write(JSON.stringify({ source, code: typeof error?.code === 'string' ? error.code : 'unknown',
      pageCount: Number.isInteger(error?.pageCount) ? error.pageCount : null,
      pages: Array.isArray(error?.pages) ? error.pages.filter(p => Number.isInteger(p) && p > 0) : [] }));
  }
} catch (error) {
  process.stdout.write(JSON.stringify({ code: 'unknown' }));
}
