import { formatFromBytes, formatFromExtension, toMarkdownBytes } from '@firecrawl/anydoc';

const chunks = [];
for await (const chunk of process.stdin) chunks.push(chunk);
try {
  const bytes = Buffer.concat(chunks);
  const detected = formatFromBytes(bytes);
  const extensionFormat = formatFromExtension(process.argv[2]);
  const format = detected ?? extensionFormat;
  const source = { format: format ?? null, format_detection: detected ? 'content' : extensionFormat ? 'extension' : 'unknown' };
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
