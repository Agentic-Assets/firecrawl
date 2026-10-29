import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { mapNewmarkRcmFields } from '../../../sources/pure/newmark-rcm-fields.js';
const base='https://my.rcm1.com/landing.aspx?pv=synthetic';
test('60 distinct qualified synthetic source objects map without claim identity or brokerage attribution',()=>{
 for(let i=0;i<60;i++){
  const data={projectfields:{summary:`<p>Detail ${i}</p>`,showprice:true,value:i+1,valuesymbol:'$'},photos:[{type:'image',imageUrl:`https://example.test/p-${i}.jpg`}],sections:[{contacts:[{Fullname:`Person ${i}`,CompanyName:'Newmark',ShowCompany:true,Email:'person@example.test',ShowEmail:true,Phone:'555-0100',ShowPhone:false}],contents:[{content:'<a href="https://example.test/file.pdf">file</a>'}]}]};
  const m=mapNewmarkRcmFields(data,base,'sale',{priceCurrency:'USD'});assert.equal(m.description,`Detail ${i}`);assert.equal(m.salePriceUsd,i+1);assert.equal(m.photos?.length,1);assert.equal(m.contactsDetailed?.[0].company,'Newmark');assert.equal(m.contactsDetailed?.[0].phone,'555-0100');assert.equal(m.documents?.length,1);assert.equal('id' in m,false);assert.equal('url' in m,false);assert.equal('transaction' in m,false);
 }
});
test('source facts are retained independent of display flags and currency stays bound',()=>{
 for(const flag of [false,undefined,null,'true',1]){const m=mapNewmarkRcmFields({projectfields:{showprice:flag,value:25,valuesymbol:'$'},sections:[{contacts:[{Fullname:'Person',CompanyName:'Other',Email:'person@example.test',Phone:'555',ShowCompany:flag,ShowEmail:flag,ShowPhone:flag}]}]},base,'sale',{priceCurrency:'USD'});assert.equal(m.salePriceUsd,25);assert.deepEqual(m.contactsDetailed,[{name:'Person',company:'Other',email:'person@example.test',phone:'555',rcmContactSource:'newmark_published_rcm'}]);}
 assert.equal(mapNewmarkRcmFields({projectfields:{value:30,valuesymbol:'$'}},base,'lease',{priceCurrency:'USD'}).salePriceUsd,undefined);
 assert.equal(mapNewmarkRcmFields({projectfields:{value:0,valuesymbol:'$'}},base,'sale',{priceCurrency:'USD'}).salePriceUsd,undefined);
 assert.equal(mapNewmarkRcmFields({projectfields:{value:30,valuesymbol:'€'}},base,'sale',{priceCurrency:'USD'}).salePriceUsd,undefined);
});
test('unknown or hidden sections, risky links and ambiguous overview do not promote',()=>{
 const data={sections:[{isVisible:false,contacts:[{Fullname:'Hidden',ShowEmail:true,Email:'x@example.test'}]},{isVisible:true,title:'Overview',contents:[{content:'<p>One</p>'}]},{isVisible:true,title:'Overview',contents:[{content:'<p>Two</p>'}]},{images:[{imageUrl:'https://x.test/a.jpg?%2574oken=secret',link:'https://x.test/agreement.pdf'},{imageUrl:'http://x.test/a.jpg',link:'https://x.test/file.pdf?token=secret'}]}]};
 assert.deepEqual(mapNewmarkRcmFields(data,base,'sale').contactsDetailed,[{name:'Hidden',email:'x@example.test',rcmContactSource:'newmark_published_rcm'}]);
 assert.deepEqual(mapNewmarkRcmFields({projectfields:{showprice:true},sections:[{isVisible:true,title:'Overview',contents:[{content:'<p>Explicit overview</p>'}]}]},base,'sale'),{description:'Explicit overview'});
});

test('description retains display-hidden text, excludes scripts and preserves absent attributes',()=>{
 const m=mapNewmarkRcmFields({projectfields:{showprice:true,summary:'<p>Visible summary</p><script>do not retain</script><span hidden>hidden</span><span aria-hidden=\"true\">aria hidden</span>'},sections:[{contacts:[{Fullname:'Person'}]}]},base,'sale');assert.equal(m.description,'Visible summaryhiddenaria hidden');assert.deepEqual(m.contactsDetailed,[{name:'Person',rcmContactSource:'newmark_published_rcm'}]);
});

test('descriptions remain available regardless source display flags',()=>{
 for (const showprice of [false,null,undefined]) { const m=mapNewmarkRcmFields({projectfields:{showprice,summary:'Asking price $123'}},base,'sale'); assert.equal(m.description,'Asking price $123'); }
});

test('USD must be separately bound and only declared gallery images qualify',()=>{
 const data={projectfields:{showprice:true,value:19,valuesymbol:'$'},photos:[{type:'image',imageUrl:'https://example.test/p.jpg'},{type:'logo',imageUrl:'https://example.test/logo.jpg'},{type:'image',isVisible:false,imageUrl:'https://example.test/hidden.jpg'},{type:'image',imageUrl:'https://localhost/private.jpg'},{type:'image',imageUrl:'https://maps.googleapis.com/tile.jpg'},{type:'image',imageUrl:'https://example.test/p.jpg?x=%zz'}],sections:[{images:[{imageUrl:'https://example.test/unknown.jpg'}]}]};
 const unbound=mapNewmarkRcmFields(data,base,'sale');assert.equal(unbound.salePriceUsd,undefined);assert.deepEqual(unbound.photos,['https://example.test/p.jpg','https://example.test/hidden.jpg']);
 const bound=mapNewmarkRcmFields(data,base,'sale',{priceCurrency:'USD'});assert.equal(bound.salePriceUsd,19);
});

test('published Newmark contact source survives staging without invented affiliation', async () => {
 const {spawnSync}=await import('node:child_process');
 const fields=mapNewmarkRcmFields({sections:[{contacts:[{Fullname:'Person'}, {Fullname:'Co-broker',CompanyName:'Reported firm'}]}]},base,'sale');
 const listing={sourceKey:'newmark',id:'synthetic',url:'https://example.test/p',sourceCompany:'Newmark',...fields};
 const result=spawnSync('python3',['-c','import json,sys,cre_ingest; print(json.dumps(cre_ingest.to_row(json.load(sys.stdin),{},None)["contacts"]))'],{input:JSON.stringify(listing),encoding:'utf8'});
 assert.equal(result.status,0,result.stderr);
 const contacts=JSON.parse(result.stdout);
 assert.equal(contacts[0].company,null);
 assert.equal(contacts[1].company,'Reported firm');
});
