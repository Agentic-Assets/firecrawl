import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { NativeEvidenceTerminalError } from '../../../lib/source-evidence/terminal-error.js';
import { BuildoutNativeCaptureError, BuildoutRepresentationError } from '../../../lib/source-evidence/buildout-page.js';
import { PyramidNativeCaptureError } from '../../../lib/source-evidence/pyramid-page.js';
import { NativeJsonFeedCaptureError } from '../../../lib/source-evidence/native-json-feed.js';
import { groupEnrichItems, runEnrichGroups } from '../../../lib/enrich.js';

test('native capture error families remain terminal through enrichment dispatch', async () => {
  for (const ErrorType of [NativeEvidenceTerminalError, BuildoutNativeCaptureError, BuildoutRepresentationError, PyramidNativeCaptureError, NativeJsonFeedCaptureError]) {
    const error = new ErrorType('synthetic capture failure');
    assert.ok(error instanceof NativeEvidenceTerminalError);
    const groups = groupEnrichItems([{sourceKey:'first',externalId:'1',url:'https://example.com/1'}, {sourceKey:'second',externalId:'2',url:'https://example.com/2'}]);
    const called: string[] = [];
    const result = await runEnrichGroups(groups, key => ({label:'test', enricher:{async enrich() {called.push(key); throw error;}}}), () => 'test');
    assert.equal(result.terminalCaptureFailure, true);
    assert.equal(result.sources[0].error, 'native_capture_failed');
    assert.deepEqual(called, ['first']);
  }
});

test('ordinary enrichment errors continue to the next source', async () => {
  const groups = groupEnrichItems([{sourceKey:'first',externalId:'1',url:'https://example.com/1'}, {sourceKey:'second',externalId:'2',url:'https://example.com/2'}]);
  const called: string[] = [];
  const result = await runEnrichGroups(groups, key => ({label:'test', enricher:{async enrich() {called.push(key); if (key === 'first') throw new Error('ordinary'); return [];}}}), () => 'test');
  assert.deepEqual(called, ['first','second']);
  assert.match(result.sources[0].error, /ordinary/);
});

test('actual both-transaction CLI dispatch stops for every terminal class and continues for an ordinary error', async () => {
  const { mkdtempSync, readFileSync, rmSync, writeFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join, resolve } = await import('node:path');
  const { pathToFileURL } = await import('node:url');
  const { spawnSync } = await import('node:child_process');
  const dir = mkdtempSync(join(tmpdir(), 'terminal-cli-'));
  const calls = join(dir, 'calls');
  const preload = join(dir, 'preload.mjs');
  const root = resolve('lib/source-evidence');
  const imports = [
    ['NativeEvidenceTerminalError', 'terminal-error.ts'],
    ['BuildoutNativeCaptureError', 'buildout-page.ts'],
    ['BuildoutRepresentationError', 'buildout-page.ts'],
    ['PyramidNativeCaptureError', 'pyramid-page.ts'],
    ['NativeJsonFeedCaptureError', 'native-json-feed.ts'],
  ];
  const source = `${imports.map(([name,file]) => `import {${name}} from ${JSON.stringify(pathToFileURL(join(root,file)).href)};`).join('\n')}
    import {appendFileSync} from 'node:fs';
    const classes = {NativeEvidenceTerminalError,BuildoutNativeCaptureError,BuildoutRepresentationError,PyramidNativeCaptureError,NativeJsonFeedCaptureError,Error};
    export async function srcNewmark(tx) { appendFileSync(${JSON.stringify(calls)}, tx + '\\n');
      if (tx === (process.env.TEST_FAIL_TX ?? 'sale')) throw new classes[process.env.TEST_TERMINAL_CLASS]('synthetic');
      return {company:'synthetic', sourceUrl:'https://example.com', method:'fixture', totalAvailable:1, listings:[{url:'https://example.com/1'}]}; }`;
  writeFileSync(preload, `import {registerHooks} from 'node:module'; const replacement=${JSON.stringify('data:text/javascript,' + encodeURIComponent(source))}; registerHooks({resolve(specifier,context,next){ if(specifier === './sources/newmark.js' && context.parentURL?.endsWith('/collect.ts')) return {url:replacement,shortCircuit:true}; return next(specifier,context); }});`);
  try {
    for (const name of [...imports.map(([name]) => name), 'Error']) {
      try { rmSync(calls); } catch {}
      const child = spawnSync(process.execPath, ['--import','tsx','--import',preload,'collect.ts','--source','newmark','--transaction','both','--out',join(dir, `out-${name}.json`)], {cwd:process.cwd(),encoding:'utf8',timeout:20000,env:{...process.env, TEST_TERMINAL_CLASS:name}});
      assert.equal(child.error, undefined, child.stderr);
      assert.equal(child.status, name === 'Error' ? 0 : 1, child.stderr);
      assert.equal(readFileSync(calls,'utf8'), name === 'Error' ? 'sale\nlease\n' : 'sale\n', child.stderr);
    }
    rmSync(calls);
    const out = join(dir, 'partial.json');
    const partial = spawnSync(process.execPath, ['--import','tsx','--import',preload,'collect.ts','--source','newmark','--transaction','both','--out',out], {cwd:process.cwd(),encoding:'utf8',timeout:20000,env:{...process.env,TEST_TERMINAL_CLASS:'NativeEvidenceTerminalError',TEST_FAIL_TX:'lease'}});
    assert.equal(partial.status,1,partial.stderr);
    assert.equal(readFileSync(calls,'utf8'),'sale\nlease\n');
    const artifact = JSON.parse(readFileSync(out,'utf8'));
    assert.equal(artifact.runMeta.terminalCaptureFailure,true);
    assert.equal(artifact.listings.length,1);
    assert.equal(artifact.sources[1].error,'native_capture_failed');
    const { statSync } = await import('node:fs');
    assert.equal(statSync(out).mode & 0o777,0o600);
    rmSync(calls);
    const fallback = spawnSync(process.execPath, ['--import','tsx','--import',preload,'collect.ts','--source','newmark','--transaction','both'], {cwd:process.cwd(),encoding:'utf8',timeout:20000,env:{...process.env,TEST_TERMINAL_CLASS:'NativeEvidenceTerminalError',TEST_FAIL_TX:'lease',TMPDIR:dir}});
    assert.equal(fallback.status,1,fallback.stderr); assert.equal(fallback.stdout,'');
    assert.equal(readFileSync(calls,'utf8'),'sale\nlease\n');
    const fallbackPath = fallback.stderr.match(/terminal partial artifact: (.+)/)?.[1];
    assert.ok(fallbackPath); assert.equal(statSync(fallbackPath).mode & 0o777,0o600);
    assert.equal(statSync(join(fallbackPath,'..')).mode & 0o777,0o700);
    assert.equal(JSON.parse(readFileSync(fallbackPath,'utf8')).listings.length,1);
  } finally { rmSync(dir,{recursive:true,force:true}); }
});

test('actual enrichment CLI preserves completed group on later terminal failure', async () => {
  const { mkdtempSync, readFileSync, rmSync, statSync, writeFileSync } = await import('node:fs');
  const { tmpdir } = await import('node:os');
  const { join, resolve } = await import('node:path');
  const { pathToFileURL } = await import('node:url');
  const { spawnSync } = await import('node:child_process');
  const dir = mkdtempSync(join(tmpdir(), 'terminal-enrich-'));
  const calls = join(dir, 'calls'); const preload = join(dir, 'preload.mjs');
  const input = join(dir, 'claims.json'); const output = join(dir, 'out.json');
  const enrich = pathToFileURL(resolve('lib/enrich.ts')).href;
  const terminal = pathToFileURL(resolve('lib/source-evidence/terminal-error.ts')).href;
  const source = `export {groupEnrichItems,runEnrichGroups} from ${JSON.stringify(enrich)};
    import {NativeEvidenceTerminalError} from ${JSON.stringify(terminal)};
    import {appendFileSync} from 'node:fs';
    export function resolveEnricher(key) {return {label:'fixture',enricher:{async enrichWithEvidence(items){appendFileSync(${JSON.stringify(calls)},key+'\\n'); if(key==='second') throw new NativeEvidenceTerminalError('private detail'); return {listings:[{url:items[0].url}],retainedObservations:[{fixture:'prior evidence'}]};}}};}`;
  writeFileSync(preload,`import {registerHooks} from 'node:module'; const replacement=${JSON.stringify('data:text/javascript,'+encodeURIComponent(source))}; registerHooks({resolve(s,c,next){if(s==='./lib/enrich.js' && c.parentURL?.endsWith('/collect.ts')) return {url:replacement,shortCircuit:true};return next(s,c);}});`);
  writeFileSync(input,JSON.stringify({items:['first','second','third'].map((sourceKey,i)=>({sourceKey,externalId:String(i),url:`https://example.com/${i}`}))}));
  try {
    const child=spawnSync(process.execPath,['--import','tsx','--import',preload,'collect.ts','--enrich-input',input,'--out',output],{cwd:process.cwd(),encoding:'utf8',timeout:20000});
    assert.equal(child.status,1,child.stderr);assert.equal(readFileSync(calls,'utf8'),'first\nsecond\n');
    const artifact=JSON.parse(readFileSync(output,'utf8'));
    assert.equal(artifact.runMeta.terminalCaptureFailure,true);assert.equal(artifact.listings.length,1);
    assert.deepEqual(artifact.sources[0].retainedObservations,[{fixture:'prior evidence'}]);
    assert.equal(artifact.sources[1].error,'native_capture_failed');assert.equal(statSync(output).mode & 0o777,0o600);
    rmSync(calls);
    const fallback=spawnSync(process.execPath,['--import','tsx','--import',preload,'collect.ts','--enrich-input',input],{cwd:process.cwd(),encoding:'utf8',timeout:20000,env:{...process.env,TMPDIR:dir}});
    assert.equal(fallback.status,1,fallback.stderr);assert.equal(fallback.stdout,'');
    assert.equal(readFileSync(calls,'utf8'),'first\nsecond\n');
    const fallbackPath=fallback.stderr.match(/terminal partial artifact: (.+)/)?.[1];
    assert.ok(fallbackPath); assert.equal(statSync(fallbackPath).mode & 0o777,0o600);
    assert.equal(statSync(join(fallbackPath,'..')).mode & 0o777,0o700);
    const saved=JSON.parse(readFileSync(fallbackPath,'utf8'));
    assert.equal(saved.listings.length,1);assert.deepEqual(saved.sources[0].retainedObservations,[{fixture:'prior evidence'}]);
  } finally {rmSync(dir,{recursive:true,force:true});}
});
