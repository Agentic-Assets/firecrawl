import { strict as assert } from 'node:assert';
import { test } from 'node:test';
import { prune } from '../../../lib/util.js';
import { resetBrokerStateForTests, brokers } from '../../../lib/broker.js';
import { cbreDealflowCardContacts, cbreDealflowContacts, enrichCbreDealflowCard } from '../../../sources/cbre-dealflow.js';
import { colliersContactsFromCard, colliersDetailContacts } from '../../../sources/colliers.js';
import { rcmSourceControls, rcmPublicContactUrl, RCM_CONTACT_FLAGS } from '../../../sources/pure/rcm-source-controls.js';

test('60 synthetic source contacts retain reported facts, flags and actual company',()=>{
 for(let i=0;i<60;i++){
  const flag=i%3===0?false:i%3===1?true:null;const c={Fullname:`Person ${i}`,Email:'sample@example.test',Phone:'555-0100',Title:'Broker',CompanyName:'Another Firm',License:'ID',ExpertBioUrl:'/bio',ProfileImageUrl:'/photo.jpg',ShowEmail:flag,ShowPhone:flag,ShowTitle:flag,ShowCompany:flag,ShowLicense:flag,ShowExpertBio:flag,ShowProfileImage:flag};
  const cb=cbreDealflowContacts({sections:[{contacts:[c]}]})[0];const co=colliersDetailContacts({ProjectContacts:[{Name:c.Fullname,Email:c.Email,Phone:c.Phone,Title:c.Title,Company:c.CompanyName,License:c.License,ExpertBioUrl:c.ExpertBioUrl,ProfileImageUrl:c.ProfileImageUrl,ShowEmail:flag,ShowPhone:flag,ShowTitle:flag,ShowExpertBio:flag}]})[0];
  for(const x of [cb,co]){assert.equal(x.email,c.Email);assert.equal(x.phone,c.Phone);assert.equal(x.company,'Another Firm');assert.equal(x.title,'Broker');assert.equal(x.license,'ID');assert.equal(x.sourceDisplayControls.ShowEmail.state,flag===null?'null':String(flag));assert.equal(prune(x).sourceDisplayControls.ShowEmail.state,flag===null?'null':String(flag));}
 }
});
test('contact urls cannot smuggle credentials and absent company is not invented',()=>{
 const d={sections:[{contacts:[{Email:'sample@example.test',ShowEmail:false,ProfileImageUrl:'https://example.test/p.jpg?%2574oken=secret',ExpertBioUrl:'https://localhost/bio'}]}]};const x=cbreDealflowContacts(d)[0];assert.equal(x.email,'sample@example.test');assert.equal(x.company,undefined);assert.equal(x.avatarUrl,undefined);assert.equal(x.profileUrl,undefined);
 const controls=rcmSourceControls({ShowEmail:false,ShowPhone:null,ShowTitle:'false',ShowName:{secret:'not-for-output'},ShowCompany:'pending review'},RCM_CONTACT_FLAGS);assert.equal(controls.ShowEmail.scalarJson,'false');assert.equal(controls.ShowPhone.scalarJson,'null');assert.equal(controls.ShowTitle.scalarJson,'"false"');assert.equal(controls.ShowName.state,'unresolved');assert.equal(controls.ShowCompany.scalarJson,'"pending review"');
});
test('CBRE source price survives false flag without fabricated USD or brokerage',async()=>{
 const original=globalThis.fetch;const card:any={id:'id',url:'https://www.cbredealflow.com/handler/landing.aspx?pv=synthetic',urlKind:'detail',listingPv:'synthetic',name:'Property',transactionType:'Investment Sale',brokerIds:[],contactsDetailed:[],brochures:[],photos:[]};
 try{resetBrokerStateForTests();let data:any={name:'Property',projectfields:{showprice:false,value:12345,valuesymbol:'$',currency:'CAD'},sections:[{contacts:[{Fullname:'Person',CompanyName:'Other Firm',Email:'person@example.test',ShowEmail:false}]}]};globalThis.fetch=async()=>new Response(`<script>var data = ${JSON.stringify(data)}</script>`);
  const a=await enrichCbreDealflowCard(card,'sale');assert.equal(a.salePriceUsd,undefined);assert.equal(a.salePriceText,'CAD $12345');assert.equal(a.extraFacts.cbre_dealflow_reported_price.reportedNumber,12345);assert.equal(a.extraFacts.cbre_dealflow_reported_price.currency,'CAD');assert.equal(a.cbreDealflowDetail.sourceDisplayControls.showprice.state,'false');assert.equal(a.contactsDetailed[0].email,'person@example.test');assert.equal(brokers[0].company,'Other Firm');
  data.projectfields.currency='USD';const b=await enrichCbreDealflowCard(card,'sale');assert.equal(b.salePriceUsd,12345);
  const lease=await enrichCbreDealflowCard(card,'lease');assert.equal(lease.salePriceUsd,undefined);assert.equal(lease.salePriceText,undefined);assert.equal(lease.extraFacts.cbre_dealflow_reported_price.reportedNumber,12345);
  data.projectfields.value=0;const z=await enrichCbreDealflowCard(card,'sale');assert.equal(z.extraFacts.cbre_dealflow_reported_price.reportedNumber,0);assert.equal(z.salePriceUsd,undefined);
  data.projectfields.value='';const empty=await enrichCbreDealflowCard(card,'sale');assert.equal(empty.extraFacts.cbre_dealflow_reported_price.valueState,'empty_string');assert.equal(empty.extraFacts.cbre_dealflow_reported_price.reportedTextJson,'""');
  data.projectfields.value=12345;data.projectfields.valuesymbol='€';const conflict=await enrichCbreDealflowCard(card,'sale');assert.equal(conflict.salePriceUsd,undefined);assert.equal(conflict.extraFacts.cbre_dealflow_reported_price.currencyBasis,'unverified_or_conflicting');
  data.projectfields.valuesymbol='X$';const unknownSymbol=await enrichCbreDealflowCard(card,'sale');assert.equal(unknownSymbol.salePriceUsd,undefined);assert.equal(unknownSymbol.extraFacts.cbre_dealflow_reported_price.currencyBasis,'unverified_or_conflicting');
 }finally{globalThis.fetch=original;resetBrokerStateForTests();}
});

test('empty records are excluded but partial source contacts survive', () => {
  assert.deepEqual(colliersDetailContacts({ProjectContacts:[{}]}), []);
  for (const [key, value] of [['Title','Broker'], ['CompanyName','Other'], ['License','ABC']]) {
    const cb = cbreDealflowContacts({sections:[{contacts:[{[key]:value}]}]});
    assert.equal(cb.length, 1);
    const coKey = key === 'CompanyName' ? 'Company' : key;
    assert.equal(colliersDetailContacts({ProjectContacts:[{[coKey]:value}]}).length, 1);
  }
  for (const url of ['https://localhost./x', 'https://router.local./x', 'https://a.localhost./x'])
    assert.equal(rcmPublicContactUrl(url), null);
  assert.equal(rcmPublicContactUrl('https://example.test./x'), 'https://example.test./x');
});

test('credential-bearing source price text does not enter canonical output', async () => {
  const original = globalThis.fetch;
  const card:any={id:'id',url:'https://www.cbredealflow.com/handler/landing.aspx?pv=synthetic',urlKind:'detail',listingPv:'synthetic',name:'Property',brokerIds:[],contactsDetailed:[],brochures:[],photos:[]};
  try {
    const data={name:'Property',projectfields:{currency:'USD',value:'https://example.test/doc?token=secret'},sections:[]};
    globalThis.fetch=async()=>new Response(`<script>var data = ${JSON.stringify(data)}</script>`);
    const result=await enrichCbreDealflowCard(card,'sale');
    assert.equal(result.salePriceText, undefined);
    assert.equal(result.extraFacts.cbre_dealflow_reported_price.valueState,'unresolved');
    assert.equal(JSON.stringify(result).includes('token=secret'),false);
  } finally { globalThis.fetch=original; }
});

test('actual adapter contacts survive Python staging without invented company', async () => {
  const { spawnSync } = await import('node:child_process');
  const listings = [
    {sourceKey:'cbre-dealflow', contactsDetailed:cbreDealflowContacts({sections:[{contacts:[{Title:'Broker',License:'ABC'}, {Fullname:'Person'}]}]})},
    {sourceKey:'colliers', contactsDetailed:colliersDetailContacts({ProjectContacts:[{Company:'Other Firm'}, {Name:'Person'}]})},
  ].map(value => ({...value, id:'test', url:'https://example.test/listing', sourceCompany:'Host Firm',
                   extraFacts:{rcm_source_contacts:value.contactsDetailed}}));
  const script = `import json,sys\nimport cre_ingest\nrows=[cre_ingest.to_row(x,{},None) for x in json.load(sys.stdin)]\nprint(json.dumps([{'contacts':r['contacts'],'extra':r['extra_facts']} for r in rows]))`;
  const result=spawnSync('python3',['-c',script],{input:JSON.stringify(listings),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  const rows=JSON.parse(result.stdout);
  assert.equal(rows[0].contacts.length,1);
  assert.equal(rows[0].contacts[0].company,null);
  assert.equal(rows[0].extra.rcm_unidentified_source_contacts[0].license,'ABC');
  assert.equal(rows[1].contacts.length,1);
  assert.equal(rows[1].contacts[0].company,null);
  assert.equal(rows[1].extra.rcm_unidentified_source_contacts[0].company,'Other Firm');
  assert.equal(rows[0].extra.rcm_source_contacts.length,2);
});

test('inventory card contacts retain source witness through Python staging', async () => {
  const cheerio = await import('cheerio');
  const { spawnSync } = await import('node:child_process');
  const $=cheerio.load(`<div class="contacts"><div class="tab-text"><span class="name">Person</span></div><div class="contact"><span class="name">Person</span></div></div>`);
  const listings=[
    {sourceKey:'cbre-dealflow',contactsDetailed:cbreDealflowCardContacts($,$('div.contacts').parent())},
    {sourceKey:'colliers',contactsDetailed:colliersContactsFromCard($,$('div.contacts').parent())},
  ].map(value=>({...value,id:'test',url:'https://example.test/p',sourceCompany:'Host Firm'}));
  for (const listing of listings) assert.equal(listing.contactsDetailed.length,1);
  const script=`import json,sys\nimport cre_ingest\nprint(json.dumps([cre_ingest.to_row(x,{},None)['contacts'] for x in json.load(sys.stdin)]))`;
  const result=spawnSync('python3',['-c',script],{input:JSON.stringify(listings),encoding:'utf8'});
  assert.equal(result.status,0,result.stderr);
  for (const contacts of JSON.parse(result.stdout)) assert.equal(contacts[0].company,null);
});

test('company-only inventory cards retain partial facts without inventing a name', async () => {
  const cheerio=await import('cheerio');
  const $=cheerio.load(`<div class="contacts"><div class="tab-text"><span class="company">Actual Firm</span></div><div class="contact"><span class="company">Actual Firm</span></div></div>`);
  for (const result of [cbreDealflowCardContacts($,$('div.contacts').parent()),colliersContactsFromCard($,$('div.contacts').parent())]) {
    assert.equal(result.length,1);
    assert.equal(result[0].company,'Actual Firm');
    assert.equal(result[0].name,undefined);
  }
});
