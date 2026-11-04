import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

for (const mode of ['stdout', 'existing', 'fresh', 'sidecar-failure'] as const) test(`both-mode ${mode} keeps healthy lease and private Franklin evidence`, () => {
  const dir = mkdtempSync(join(tmpdir(), 'franklin-cli-'));
  const preload = join(dir, 'preload.mjs');
  const output = join(dir, 'existing.json');
  const html = '<html><script>PRIVATE_PAGE_MARKER</script><article data-listing-card="true" data-listing-id="card-1" data-listing-price="123">Private card</article></html>';
  const saleKey = 'a234450b432b2b2bebc1ace7e6f692e4489bde70';
  const leaseKey = '2f82fcd26667c4b0126d0084938ffa265f05fa4a';
  writeFileSync(preload, `${mode === 'sidecar-failure' ? "import fs from 'node:fs'; import {syncBuiltinESMExports} from 'node:module'; const old=fs.mkdtempSync; fs.mkdtempSync=(prefix,...args)=>String(prefix).includes('cre-terminal-partial-') ? (()=>{throw Error('test sidecar failure')})() : old(prefix,...args); syncBuiltinESMExports();" : ''} globalThis.fetch = async input => { const url=String(input); if (url.includes('franklinst.com/properties/for-sale')) return new Response(${JSON.stringify(html)}, {headers:{'content-type':'text/html'}}); if (url.includes(${JSON.stringify(saleKey)})) return new Response('{}'); if (url.includes(${JSON.stringify(leaseKey)})) return new Response(JSON.stringify({inventory:[{id:42,sale:false,closed:false,show_link:'https://www.franklinst.com/properties/?propertyId=42-lease',display_name:'Healthy lease', index_attributes:[]}],meta:{total:1,limit:100}})); throw new Error('unexpected URL '+url); };`);
  if (mode === 'existing') writeFileSync(output, 'previous contents');
  try {
    const args = ['--import','tsx','--import',preload,'collect.ts','--source','franklin-street','--transaction','both','--max-items','0',...(['existing','fresh'].includes(mode) ? ['--out',output] : [])];
    const result = spawnSync(process.execPath,args,{cwd:process.cwd(),encoding:'utf8',timeout:20000,maxBuffer:2000000,env:{...process.env,BUILDOUT_PAGE_JITTER_MS:'0',CRE_SCRAPE_MAX_ATTEMPTS:'1',...(mode === 'sidecar-failure' ? {TMPDIR:join(dir,'missing')} : {})}});
    assert.equal(result.status,mode === 'sidecar-failure' ? 1 : 0,result.stderr);
    const text = ['stdout','sidecar-failure'].includes(mode) ? result.stdout : readFileSync(output,'utf8');
    if (mode === 'fresh') {
      const data=JSON.parse(text);
      assert.equal(data.sources[0].retainedObservations[0].decodedHtml,html);
      assert.equal(data.listings[0].rawBuildout.id,42);
      assert.doesNotMatch(result.stderr,/Franklin private sidecar:/);
      return;
    }
    assert.doesNotMatch(text, /PRIVATE_PAGE_MARKER|Private card|rawBuildout|buildoutRawRetention/);
    const data = JSON.parse(text);
    assert.equal(data.runMeta.evidenceOnly,true);
    assert.match(data.sources[0].error,/Buildout page 0/);
    assert.equal(data.listings.length,1);
    assert.equal(data.listings[0].name,'Healthy lease');
    if (mode === 'sidecar-failure') {
      assert.equal(data.runMeta.franklinSidecar,'failed');
      assert.match(result.stderr,/private evidence was not retained/);
      return;
    }
    const match = /Franklin private sidecar: (.+)/.exec(result.stderr);
    assert.ok(match,result.stderr);
    const path=match[1].trim();
    const privateData=JSON.parse(readFileSync(path,'utf8'));
    assert.equal(privateData.runMeta.evidenceOnly,true);
    assert.equal(privateData.sources[0].retainedObservations[0].decodedHtml,html);
    assert.equal(privateData.sources[0].retainedObservations[0].cards[0].attributes['data-listing-price'],'123');
    assert.equal(privateData.listings[0].rawBuildout.id,42);
    if (process.platform !== 'win32') assert.equal(statSync(path).mode & 0o077,0);
    rmSync(path.substring(0,path.lastIndexOf('/')),{recursive:true,force:true});
  } finally { rmSync(dir,{recursive:true,force:true}); }
});

test('other private Buildout sources keep the existing stdout refusal', () => {
  for (const sources of ['svn', 'franklin-street,svn']) {
    const result=spawnSync(process.execPath,['--import','tsx','collect.ts','--source',sources,'--transaction','both'],{cwd:process.cwd(),encoding:'utf8',timeout:20000});
    assert.equal(result.status,1);
    assert.match(result.stderr,/require a private --out file/);
    assert.equal(result.stdout,'');
  }
});
