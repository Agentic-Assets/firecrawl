import { test } from 'node:test';
import assert from 'node:assert/strict';
import { mkdtempSync, writeFileSync, readFileSync, rmSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawnSync } from 'node:child_process';

for (const mode of ['stdout', 'existing', 'fresh', 'sidecar-failure', 'projection-failure'] as const) test(`both-mode ${mode} keeps healthy lease and private Franklin evidence`, () => {
  const dir = mkdtempSync(join(tmpdir(), 'franklin-cli-'));
  const preload = join(dir, 'preload.mjs');
  const output = join(dir, 'existing.json');
  const html = '<html><script>PRIVATE_PAGE_MARKER</script><article data-listing-card="true" data-listing-id="card-1" data-listing-price="123">Private card</article></html>';
  const saleKey = 'a234450b432b2b2bebc1ace7e6f692e4489bde70';
  const leaseKey = '2f82fcd26667c4b0126d0084938ffa265f05fa4a';
  writeFileSync(preload, `${mode === 'projection-failure' ? "process.stdout.write=()=>{throw Error('projection write failed')};" : ''} ${mode === 'sidecar-failure' ? "import fs from 'node:fs'; import {syncBuiltinESMExports} from 'node:module'; const old=fs.mkdtempSync; fs.mkdtempSync=(prefix,...args)=>String(prefix).includes('cre-terminal-partial-') ? (()=>{throw Error('test sidecar failure')})() : old(prefix,...args); syncBuiltinESMExports();" : ''} globalThis.fetch = async input => { const url=String(input); if (url.includes('franklinst.com/properties/for-sale')) return new Response(${JSON.stringify(html)}, {headers:{'content-type':'text/html'}}); if (url.includes(${JSON.stringify(saleKey)})) return new Response('{}'); if (url.includes(${JSON.stringify(leaseKey)})) return new Response(JSON.stringify({inventory:[{id:42,sale:false,closed:false,show_link:'https://www.franklinst.com/properties/?propertyId=42-lease',display_name:'Healthy lease', index_attributes:[]}],meta:{total:1,limit:100}})); throw new Error('unexpected URL '+url); };`);
  if (['existing','projection-failure'].includes(mode)) writeFileSync(output, 'previous contents');
  try {
    const args = ['--import','tsx','--import',preload,'collect.ts','--source','franklin-street','--transaction','both','--max-items','0',...(['existing','fresh','projection-failure'].includes(mode) ? ['--out',output] : [])];
    const result = spawnSync(process.execPath,args,{cwd:process.cwd(),encoding:'utf8',timeout:60000,maxBuffer:2000000,env:{...process.env,BUILDOUT_PAGE_JITTER_MS:'0',CRE_SCRAPE_MAX_ATTEMPTS:'1',...(mode === 'sidecar-failure' ? {TMPDIR:join(dir,'missing')} : {})}});
    assert.equal(result.status,['existing','sidecar-failure','projection-failure'].includes(mode) ? 1 : 0,result.stderr);
    if (mode === 'existing') { assert.equal(readFileSync(output,'utf8'),'previous contents'); assert.match(result.stderr,/existing output unchanged; Franklin projection written to stdout/); }
    if (mode === 'projection-failure') {
      assert.equal(readFileSync(output,'utf8'),'previous contents');
      assert.match(result.stderr,/projection write failed/);
      assert.doesNotMatch(result.stderr,/projection written to stdout/);
      const sidecar = /Franklin private sidecar: (.+)/.exec(result.stderr);
      assert.ok(sidecar,result.stderr);
      const path = sidecar[1].trim();
      assert.match(readFileSync(path,'utf8'),/PRIVATE_PAGE_MARKER/);
      rmSync(path.substring(0,path.lastIndexOf('/')),{recursive:true,force:true});
      return;
    }
    const text = ['stdout','existing','sidecar-failure'].includes(mode) ? result.stdout : readFileSync(output,'utf8');
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
    const result=spawnSync(process.execPath,['--import','tsx','collect.ts','--source',sources,'--transaction','both'],{cwd:process.cwd(),encoding:'utf8',timeout:60000});
    assert.equal(result.status,1);
    assert.match(result.stderr,/require a private --out file/);
    assert.equal(result.stdout,'');
  }
});

for (const sibling of ['interra-realty', 'svn'] as const) test(`mixed ${sibling} existing output preserves private custody`, () => {
  const dir = mkdtempSync(join(tmpdir(), 'franklin-mixed-'));
  const preload=join(dir,'preload.mjs'), output=join(dir,'output.json');
  const html='<article data-listing-card="true" data-listing-id="p1">PRIVATE_FRANKLIN</article>';
  const inventory = {inventory:[{id:42,sale:false,closed:false,show_link:'https://www.franklinst.com/properties/?propertyId=42-lease',display_name:'Healthy lease',index_attributes:[]}],meta:{total:1,limit:100}};
  const svn = {inventory:[{id:55,sale:true,closed:false,show_link:'https://svn.com/properties/?propertyId=55-sale',display_name:'Private SVN',index_attributes:[]}],meta:{total:1,limit:100}};
  const interra={success:true,has_next_page:false,posts:[{ID:2,post_title:'Healthy Interra',post_content:'<p>Fixture.</p>',permalink:'https://interrarealty.com/listing/property-2/',meta:{listing_status:'available',listing_address:'123 Main St',listing_city:'Chicago, IL 60622',key_details:'0'}}]};
  const scrapePath=new URL('../../../lib/scrape.ts',import.meta.url).href;
  writeFileSync(preload,`import {firecrawl} from ${JSON.stringify(scrapePath)}; firecrawl.scrape=async()=>({rawHtml:${JSON.stringify(JSON.stringify(svn))}}); globalThis.fetch=async input=>{const url=String(input); if(url.includes('franklinst.com/properties/for-sale')) return new Response(${JSON.stringify(html)},{headers:{'content-type':'text/html'}}); if(url.includes('a234450b432b2b2bebc1ace7e6f692e4489bde70')) return new Response('{}'); if(url.includes('2f82fcd26667c4b0126d0084938ffa265f05fa4a')) return Response.json(${JSON.stringify(inventory)}); if(url.includes('interrarealty.com/wp-json/')) return Response.json(${JSON.stringify(interra)}); if(url.includes('b933480474026c41d248b77156c84aef37dcac68')) return Response.json(${JSON.stringify(svn)}); throw Error('unexpected '+url); };`);
  writeFileSync(output,'previous');
  try {
    const child=spawnSync(process.execPath,['--import','tsx','--import',preload,'collect.ts','--source',`franklin-street,${sibling}`,'--transaction','both','--max-items','0','--out',output],{cwd:process.cwd(),encoding:'utf8',timeout:60000,maxBuffer:2000000,env:{...process.env,BUILDOUT_PAGE_JITTER_MS:'0',CRE_SCRAPE_MAX_ATTEMPTS:'1'}});
    if(sibling==='svn') {
      assert.equal(child.status,1,child.stderr);
      assert.match(child.stderr,/svn\/sale: 1 listings/,child.stderr);
      assert.match(child.stderr,/EEXIST/,child.stderr);
      assert.equal(readFileSync(output,'utf8'),'previous');
      assert.doesNotMatch(child.stderr,/Franklin private sidecar:/);
      const fresh=join(dir,'private-fresh.json');
      const proof=spawnSync(process.execPath,['--import','tsx','--import',preload,'collect.ts','--source','franklin-street,svn','--transaction','both','--max-items','0','--out',fresh],{cwd:process.cwd(),encoding:'utf8',timeout:60000,maxBuffer:2000000,env:{...process.env,BUILDOUT_PAGE_JITTER_MS:'0',CRE_SCRAPE_MAX_ATTEMPTS:'1'}});
      assert.equal(proof.status,0,proof.stderr);
      const listing=JSON.parse(readFileSync(fresh,'utf8')).listings.find((row: any)=>row.sourceKey==='svn');
      assert.equal(listing.rawBuildout.id,55);
      assert.equal(listing.buildoutRawRetention.status,'complete');
      return;
    }
    assert.equal(child.status,1,child.stderr);
    assert.equal(readFileSync(output,'utf8'),'previous');
    assert.match(child.stderr,/existing output unchanged; Franklin projection written to stdout/);
    const data=JSON.parse(child.stdout);
    assert.equal(data.runMeta.evidenceOnly,true);
    assert.ok(data.listings.some((x: any)=>x.name==='Healthy lease'));
    assert.ok(data.listings.some((x: any)=>x.name==='Healthy Interra'));
    assert.doesNotMatch(JSON.stringify(data),/PRIVATE_FRANKLIN|rawBuildout|buildoutRawRetention/);
    const match=/Franklin private sidecar: (.+)/.exec(child.stderr); assert.ok(match,child.stderr);
    const sidecar=JSON.parse(readFileSync(match[1].trim(),'utf8'));
    assert.equal(sidecar.sources[0].retainedObservations[0].decodedHtml,html);
    assert.ok(sidecar.listings.some((x: any)=>x.name==='Healthy Interra'));
    rmSync(match[1].trim().substring(0,match[1].trim().lastIndexOf('/')),{recursive:true,force:true});
  } finally { rmSync(dir,{recursive:true,force:true}); }
});
