import { createHash } from 'node:crypto';
import { constants } from 'node:fs';
import { open } from 'node:fs/promises';
import { extname } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath, pathToFileURL } from 'node:url';

const MAX_BYTES = 100 * 1024 * 1024;
const MAX_PROVENANCE_BYTES = 16 * 1024;
const MAX_TEXT_BYTES = 4 * 1024 * 1024;
const MAX_OUTPUT_BYTES = 6 * MAX_TEXT_BYTES + 65536;
const TIMEOUT_MS = 60_000;
const VERSION = '0.2.4';
const digest = bytes => createHash('sha256').update(bytes).digest('hex');

async function readBounded(input, limit = MAX_BYTES, privateFile = false) {
  // O_NONBLOCK prevents a FIFO from blocking before we can inspect its type.
  const handle = await open(input, constants.O_RDONLY | constants.O_NOFOLLOW | constants.O_NONBLOCK);
  try {
    const before = await handle.stat();
    if (!before.isFile()) throw Object.assign(new Error(), { code: 'not_regular_file' });
    if (privateFile && (typeof process.getuid !== 'function' || before.uid !== process.getuid() || (before.mode & 0o077))) throw new Error('provenance must be an owner-only file');
    if (before.size > limit) throw Object.assign(new Error(), { code: 'input_too_large' });
    const chunks = [];
    let total = 0;
    for (;;) {
      const buf = Buffer.alloc(Math.min(65536, limit + 1 - total));
      const { bytesRead } = await handle.read(buf, 0, buf.length, null);
      if (!bytesRead) break;
      total += bytesRead;
      if (total > limit) throw Object.assign(new Error(), { code: 'input_too_large' });
      chunks.push(buf.subarray(0, bytesRead));
    }
    const after = await handle.stat();
    if (before.size !== total || before.size !== after.size || before.mtimeMs !== after.mtimeMs || before.ctimeMs !== after.ctimeMs) {
      throw Object.assign(new Error(), { code: 'input_changed' });
    }
    return Buffer.concat(chunks, total);
  } finally { await handle.close(); }
}

function convert(bytes, extension) {
  return new Promise(resolve => {
    const child = spawn(process.execPath, ['--max-old-space-size=512', fileURLToPath(new URL('./worker.mjs', import.meta.url)), extension], { stdio: ['pipe', 'pipe', 'ignore'] });
    const chunks = [];
    let size = 0;
    let reason = null;
    const timer = setTimeout(() => { reason = 'timeout'; child.kill('SIGKILL'); }, TIMEOUT_MS);
    child.stdout.on('data', chunk => {
      size += chunk.length;
      if (size > MAX_OUTPUT_BYTES) { reason = 'output_too_large'; child.kill('SIGKILL'); }
      else chunks.push(chunk);
    });
    child.stdin.on('error', () => {});
    child.on('error', () => { reason ||= 'worker_start_failed'; });
    child.on('close', (code, signal) => {
      clearTimeout(timer);
      if (reason || code !== 0) return resolve({ code: reason || (signal ? 'worker_signaled' : 'worker_failed') });
      try { resolve(JSON.parse(Buffer.concat(chunks).toString('utf8'))); }
      catch { resolve({ code: 'invalid_worker_output' }); }
    });
    child.stdin.end(bytes);
  });
}

export async function extractFile({ input, sourceUrl, observedAt }) {
  if (!input || typeof sourceUrl !== 'string' || !/^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$/.test(observedAt || '') ||
      !Number.isFinite(Date.parse(observedAt)) || new Date(observedAt).toISOString() !== observedAt.replace('Z', '.000Z')) {
    throw new Error('input, sourceUrl and UTC observedAt (YYYY-MM-DDTHH:mm:ssZ) are required');
  }
  let url;
  try { url = new URL(sourceUrl); } catch { throw new Error('invalid source URL'); }
  if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password || /[\u0000-\u001f\u007f]/.test(sourceUrl) || Buffer.byteLength(sourceUrl) > 4096) throw new Error('sourceUrl must be HTTP(S) without credentials and at most 4096 bytes');
  const secretKey = /^(?:token|access[_-]?token|api[_-]?key|signature|sig|password|secret|session|access[_-]?key(?:[_-]?id)?|awsaccesskeyid|googleaccessid|x-(?:amz|goog)-(?:signature|credential|security[_-]?token))$/i;
  if (url.hash || [...url.searchParams.keys()].some(key => secretKey.test(key))) throw new Error('sourceUrl has a disallowed credential parameter or fragment');
  const artifact = {
    schema_version: 'cre-document-text/v1',
    source: { url: sourceUrl, observed_at: observedAt, sha256: null, byte_length: null, format: null, format_detection: 'unknown' },
    parser: { name: '@firecrawl/anydoc', version: VERSION, config: { ocr: 'disabled', max_bytes: MAX_BYTES, max_text_bytes: MAX_TEXT_BYTES, timeout_ms: TIMEOUT_MS } },
    extraction: { status: 'failed', coverage: 'unknown', page_count: null, ocr_needed_pages: [], text_format: 'markdown', text: null, text_sha256: null, error_code: null },
  };
  let bytes;
  try { bytes = await readBounded(input); }
  catch (error) {
    const allowed = ['not_regular_file', 'input_too_large', 'input_changed', 'ENOENT', 'ELOOP', 'EACCES'];
    artifact.extraction.error_code = allowed.includes(error?.code) ? error.code : 'input_read_failed';
    return artifact;
  }
  Object.assign(artifact.source, { sha256: digest(bytes), byte_length: bytes.length });
  const result = await convert(bytes, extname(input));
  if (result.source) Object.assign(artifact.source, result.source);
  if (typeof result.text === 'string' && result.text.trim()) {
    const textBytes = Buffer.from(result.text, 'utf8');
    if (textBytes.length > MAX_TEXT_BYTES) artifact.extraction.error_code = 'text_too_large';
    else Object.assign(artifact.extraction, { status: 'extracted', text: result.text, text_sha256: digest(textBytes) });
  } else {
    const allowed = ['unsupported', 'needsOcr', 'malformed', 'encrypted', 'resourceLimit', 'missingPart', 'io', 'timeout', 'output_too_large', 'worker_start_failed', 'worker_signaled', 'worker_failed', 'invalid_worker_output'];
    const code = allowed.includes(result.code) ? result.code : 'unknown';
    artifact.extraction.error_code = code;
    if (code === 'needsOcr') Object.assign(artifact.extraction, { status: 'ocr_needed', page_count: Number.isInteger(result.pageCount) ? result.pageCount : null, ocr_needed_pages: Array.isArray(result.pages) ? result.pages : [] });
  }
  return artifact;
}

export async function writePrivateArtifact(path, artifact) {
  const handle = await open(path, 'wx', 0o600);
  try { await handle.writeFile(JSON.stringify(artifact, null, 2) + '\n'); }
  finally { await handle.close(); }
}

async function main() {
  const args = process.argv.slice(2);
  if (args.length !== 3) throw new Error('usage: node extract.mjs INPUT PRIVATE_PROVENANCE_JSON OUTPUT_JSON');
  const provenance = JSON.parse((await readBounded(args[1], MAX_PROVENANCE_BYTES, true)).toString('utf8'));
  if (!provenance || typeof provenance !== 'object' || Array.isArray(provenance) || Object.keys(provenance).sort().join(',') !== 'observed_at,url') throw new Error('invalid provenance');
  const artifact = await extractFile({ input: args[0], sourceUrl: provenance.url, observedAt: provenance.observed_at });
  await writePrivateArtifact(args[2], artifact);
  process.exitCode = artifact.extraction.status === 'extracted' ? 0 : 2;
}
if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  main().catch(() => { console.error('Extraction failed: invalid arguments or filesystem error'); process.exitCode = 1; });
}
