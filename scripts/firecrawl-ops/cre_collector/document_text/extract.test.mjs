import assert from 'node:assert/strict';
import { chmod, mkdtemp, open, readFile, rm, stat, symlink, writeFile } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { dirname, join } from 'node:path';
import { spawnSync } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { test } from 'node:test';
import { extractFile, writePrivateArtifact } from './extract.mjs';

test('local CSV produces a private provenance-bearing artifact without claiming complete coverage', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-text-'));
  try {
    const input = join(dir, 'test.csv');
    const output = join(dir, 'artifact.json');
    await writeFile(input, 'property,price\nExample,100\n');
    const result = await extractFile({ input, sourceUrl: 'https://example.test/test.csv', observedAt: '2026-10-28T12:00:00Z' });
    assert.equal(result.extraction.status, 'extracted');
    assert.equal(result.extraction.coverage, 'unknown');
    assert.match(result.extraction.text, /Example/);
    assert.equal(result.source.sha256, 'bbdbe9048d70dd290bbc960acba764fafb939e5cd930ec2138fc84eaf2c7ba11');
    assert.equal(result.parser.version, '0.2.4');
    assert.match(result.extraction.text_sha256, /^[a-f0-9]{64}$/);
    await writePrivateArtifact(output, result);
    assert.equal((await stat(output)).mode & 0o777, 0o600);
    assert.deepEqual(JSON.parse(await readFile(output, 'utf8')), result);
    await assert.rejects(writePrivateArtifact(output, result), { code: 'EEXIST' });
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('missing, symlink and oversized inputs are recorded without reading their contents', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-text-'));
  try {
    const input = join(dir, 'large.pdf');
    const handle = await open(input, 'w');
    await handle.truncate(100 * 1024 * 1024 + 1);
    await handle.close();
    const link = join(dir, 'link.pdf');
    await symlink(input, link);
    const args = { sourceUrl: 'https://example.test/file.pdf', observedAt: '2026-10-28T12:00:00Z' };
    for (const [path, code] of [[input, 'input_too_large'], [link, 'ELOOP'], [join(dir, 'missing'), 'ENOENT']]) {
      const result = await extractFile({ input: path, ...args });
      assert.equal(result.extraction.error_code, code);
      assert.equal(result.extraction.status, 'failed');
      assert.equal(result.source.sha256, null);
    }
    await assert.rejects(extractFile({ input, ...args, sourceUrl: 'https://user:password@example.test/' }));
    for (const query of ['token=x', 'api_key=x', 'signature=x', 'X-Amz-Credential=x', 'x-goog-signature=x', '%74oken=x']) {
      await assert.rejects(extractFile({ input, ...args, sourceUrl: `https://example.test/file?${query}` }));
    }
    await assert.rejects(extractFile({ input, ...args, observedAt: '2026-02-30T12:00:00Z' }));
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('CLI uses a bounded owner-only provenance file, never URL arguments', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-text-'));
  try {
    const input = join(dir, 'test.csv');
    const provenance = join(dir, 'provenance.json');
    const output = join(dir, 'output.json');
    await writeFile(input, 'name\nExample\n');
    await writeFile(provenance, JSON.stringify({ url: 'https://example.test/a.csv?view=public', observed_at: '2026-10-28T12:00:00Z' }), { mode: 0o600 });
    const run = () => spawnSync(process.execPath, [new URL('./extract.mjs', import.meta.url).pathname, input, provenance, output], { encoding: 'utf8' });
    assert.equal(run().status, 0);
    assert.equal(JSON.parse(await readFile(output, 'utf8')).source.url, 'https://example.test/a.csv?view=public');
    await rm(output);
    await writeFile(provenance, JSON.stringify({ url: 'https://example.test/a?token=private', observed_at: '2026-10-28T12:00:00Z' }));
    const rejected = run();
    assert.equal(rejected.status, 1);
    assert.doesNotMatch(rejected.stderr, /private|token/);
    await writeFile(provenance, ' '.repeat(16385));
    assert.equal(run().status, 1);
    await rm(provenance);
    await writeFile(provenance, JSON.stringify({ url: 'https://example.test/a.csv', observed_at: '2026-10-28T12:00:00Z' }));
    await chmod(provenance, 0o644);
    assert.equal(run().status, 1);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('CLI runs when its parent directory is reached through a symlink', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-text-link-'));
  try {
    const input = join(dir, 'test.csv');
    const provenance = join(dir, 'provenance.json');
    const output = join(dir, 'output.json');
    const alias = join(dir, 'adapter');
    await symlink(dirname(fileURLToPath(import.meta.url)), alias, 'dir');
    await writeFile(input, 'name\nExample\n');
    await writeFile(provenance, JSON.stringify({ url: 'https://example.test/a.csv', observed_at: '2026-10-28T12:00:00Z' }), { mode: 0o600 });
    const result = spawnSync(process.execPath, [join(alias, 'extract.mjs'), input, provenance, output], { encoding: 'utf8' });
    assert.equal(result.status, 0);
    assert.equal(JSON.parse(await readFile(output, 'utf8')).extraction.status, 'extracted');
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('malformed input retains identity and a failure without fabricated text', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-text-'));
  try {
    const input = join(dir, 'broken.pdf');
    await writeFile(input, '%PDF-1.7\ninvalid');
    const result = await extractFile({ input, sourceUrl: 'https://example.test/broken.pdf', observedAt: '2026-10-28T12:00:00Z' });
    assert.equal(result.extraction.status, 'failed');
    assert.equal(result.extraction.text, null);
    assert.ok(result.extraction.error_code);
    assert.match(result.source.sha256, /^[a-f0-9]{64}$/);
    await assert.rejects(extractFile({ input, sourceUrl: 'file:///tmp/test', observedAt: '2026-10-28T12:00:00Z' }));
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('oversized extracted text fails without emitting truncated text', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-text-'));
  try {
    const input = join(dir, 'large.csv');
    await writeFile(input, 'name\n' + 'abcdefghij\n'.repeat(400000));
    const result = await extractFile({ input, sourceUrl: 'https://example.test/large.csv', observedAt: '2026-10-28T12:00:00Z' });
    assert.equal(result.extraction.status, 'failed');
    assert.ok(['text_too_large', 'output_too_large', 'resourceLimit'].includes(result.extraction.error_code));
    assert.equal(result.extraction.text, null);
    assert.equal(result.extraction.text_sha256, null);
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('actual acquisition timestamps retain their supplied UTC fractional precision', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-text-time-'));
  try {
    // A missing file exercises only provenance parsing, without starting AnyDoc.
    const input = join(dir, 'missing.pdf');
    for (const observedAt of ['2026-10-28T12:00:00Z', '2026-10-28T12:00:00.1Z',
      '2026-10-28T12:00:00.123Z', '2026-10-28T12:00:00.123456Z']) {
      const result = await extractFile({ input, sourceUrl: 'https://example.test/doc', observedAt });
      assert.equal(result.source.observed_at, observedAt);
      assert.equal(result.extraction.error_code, 'ENOENT');
    }
    for (const observedAt of ['0000-01-01T12:00:00Z', '2026-02-30T12:00:00.123Z',
      '2026-10-28T12:00:00.1234567Z', '2026-10-28T12:00:60Z', '2026-10-28T08:00:00-04:00']) {
      await assert.rejects(extractFile({ input, sourceUrl: 'https://example.test/doc', observedAt }));
    }
  } finally { await rm(dir, { recursive: true, force: true }); }
});

test('extensionless source hint is used only when format detection cannot decide', async () => {
  const dir = await mkdtemp(join(tmpdir(), 'cre-hint-'));
  try {
    const input = join(dir, 'document.bin');
    const args = { input, sourceUrl: 'https://example.test/download', observedAt: '2026-10-28T12:00:00Z' };
    const hint = { schema: 'cre-document-format-hint/v1', status: 'resolved', format: 'csv', candidate_sha256: 'a'.repeat(64), selection_sha256: 'b'.repeat(64), assertions_sha256: 'c'.repeat(64), assertion_count: 1, omitted_assertions: 0, assertions: [{ occurrence: 0, field: 'type', format: 'csv' }], authority: 'source_asserted_unverified' };
    for (const invalid of [null, false, {}, { ...hint, assertion_count: true }]) await assert.rejects(extractFile({ ...args, formatHint: invalid }), /invalid format hint/);
    await writeFile(input, 'property,price\nExample,0\n');
    const detected = await extractFile({ ...args, formatHint: hint });
    assert.equal(detected.extraction.status, 'extracted');
    assert.equal(detected.source.format_detection, 'source_hint');
    assert.deepEqual(detected.source.format_hint, hint);
    assert.match(detected.extraction.text, /Example/);
    const refused = await extractFile({ ...args, formatHint: { ...hint, status: 'unresolved', format: null } });
    assert.notEqual(refused.source.format_detection, 'source_hint');
    await writeFile(input, '%PDF-1.4\n%%EOF');
    const conflict = await extractFile({ ...args, formatHint: hint });
    assert.equal(conflict.source.format_detection, 'content');
    assert.equal(conflict.source.format, 'pdf');
    assert.equal(conflict.source.format_hint_conflict, true);
  } finally { await rm(dir, { recursive: true, force: true }); }
});
