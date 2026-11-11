"""Exercise Buildout contact recovery through the real mapper and ingest staging."""

import json
import subprocess

import cre_ingest


def test_malformed_contacts_reach_ingest_with_valid_peers_and_raw_evidence():
    script = r'''
import { allBuildoutFeeds } from './sources/buildout-feeds/index.js';
import { buildoutCache, buildoutQueryFingerprint, srcBuildout } from './sources/buildout.js';
import { brokers } from './lib/broker.js';
globalThis.fetch = async () => { throw Error('unexpected network request'); };
const feed = allBuildoutFeeds.find(f => f.sourceKey === 'bradford-allen');
const opts = {cacheSlug:feed.feedKey};
const raw = [
  {id:42,sale:true,show_link:'https://broker.example/show?propertyId=42-sale',
   broker_contacts:[null,{name:'Valid Broker',email:'valid@example.test'}]},
  {id:43,sale:true,show_link:'https://broker.example/show?propertyId=43-sale',
   broker_contacts:{unexpected:'shape'}},
  {id:44,sale:true,show_link:'https://broker.example/show?propertyId=44-sale',
   broker_contacts:Array.from({length:1001}, (_, i) => ({name:`Broker ${i}`}))},
];
buildoutCache.set(`${feed.pluginKey}\u0000${buildoutQueryFingerprint(opts)}`, {items:raw,total:3});
const result = await srcBuildout(feed.company,feed.pluginKey,feed.listingsPage,'sale',10,false,opts);
process.stdout.write(JSON.stringify({listings:result.listings.map(l =>
  ({sourceKey:feed.sourceKey,transactionMode:'sale',...l})),brokers}));
'''
    mapped = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True, timeout=15,
    )
    result = json.loads(mapped.stdout)
    assert len(result["listings"]) == 3
    rows = [cre_ingest.to_row(listing, dict(enumerate(result["brokers"])), None)
            for listing in result["listings"]]
    assert all(row is not None for row in rows)
    assert rows[0]["contacts"][0]["name"] == "Valid Broker"
    assert rows[1]["contacts"] == []
    assert len(rows[2]["contacts"]) == 1001
    assert rows[2]["contacts"][-1]["name"] == "Broker 1000"
    for row in rows[:2]:
        assert row["raw_data"]["preserveChildCollections"] is True
        assert row["raw_data"]["buildoutContactMapping"]
        assert row["raw_data"]["rawBuildout"]["broker_contacts"] is not None
