// Run with Node from the pinned collector document_text installation.
import { createRequire } from 'node:module';
import { readFile } from 'node:fs/promises';
import { resolve } from 'node:path';
const require = createRequire(resolve(process.argv[2], 'package.json'));
const { formatFromBytes, toMarkdownBytes } = require('@firecrawl/anydoc');
try {
  const bytes = await readFile(process.argv[3]);
  const format = formatFromBytes(bytes);
  if (format !== 'pdf') throw Object.assign(new Error('not_pdf'), {code:'not_pdf'});
  const text = await toMarkdownBytes(bytes, format);
  if (typeof text !== 'string' || !text.trim()) throw Object.assign(new Error('empty'), {code:'empty'});
  process.stdout.write(JSON.stringify({status:'extracted',text,format,coverage:'unknown'}));
} catch (e) {
  process.stdout.write(JSON.stringify({status:e?.code === 'needsOcr' ? 'ocr_needed' : 'failed',error_code:typeof e?.code === 'string' ? e.code : 'unknown',coverage:'unknown'}));
  process.exitCode = 2;
}
