"""Actual Essex producer through artifact admission and unchanged ingest keys."""

import json
import os
import subprocess
from pathlib import Path

import pytest

import cre_ingest as ingest

HERE = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def artifact():
    script = r"""
import {readFileSync} from 'node:fs';
import {srcEssexRealty} from './sources/essex-realty.ts';
import {observationNow} from './lib/observation-clock.ts';
const startedAt=observationNow();
process.env.CRE_REQUIRE_FRESH_DETAILS='1';
process.env.CRE_REQUIRE_FRESH_PROPERTY_DETAILS='1';
process.env.CRE_REFRESH_GENERATION='essex-offline-identity';
process.env.CRE_REFRESH_STARTED_AT=startedAt;
const base='https://essexrealtygroup.com/properties/';
const slugs=['EXISTING_PROPERTY','NEW_PROPERTY'];
const template=readFileSync('tests/fixtures/essex-detail.html','utf8');
const current='<a class="allposts">ALL [2]</a>'+slugs.map(slug=>
 `<a class="property-card" href="${base}${slug}/"><span class="property-card__label">ON MARKET</span><span class="property-info__address--address-1">Synthetic property</span></a>`).join('');
const closed='<a class="allposts">ALL [1]</a><a class="property-card" href="'+base+'CLOSED_PROPERTY/"><span class="property-card__label">CLOSED</span><span class="property-info__address--address-1">Synthetic closed property</span></a>';
const bodies=new Map([[base,current],[base+'?sale_deal_status_id=3',closed],
 ['https://essexrealtygroup.com/properties-sitemap.xml','<urlset>'+[...slugs,'CLOSED_PROPERTY'].map(slug=>`<url><loc>${base}${slug}/</loc></url>`).join('')+'</urlset>'],
 ...slugs.map((slug,index)=>[base+slug+'/',template.replaceAll('EXISTING_PROPERTY',slug).replace('1090741',String(1090741+index)).replace('postid-15709','postid-'+String(15709+index))])]);
const transport={async getText(url){if(!bodies.has(url))throw Error('unexpected offline request');return bodies.get(url);}};
const sale=await srcEssexRealty('sale',Infinity,false,transport);
const lease=await srcEssexRealty('lease',Infinity,false,transport);
const listings=sale.listings.map(row=>({...row,sourceKey:'essex-realty',sourceCompany:sale.company,transactionMode:'sale'}));
console.log(JSON.stringify({runMeta:{mode:'full',startedAt,finishedAt:observationNow(),transactions:['sale','lease'],freshness:{generationId:'essex-offline-identity',generationStartedAt:startedAt,requireFreshDetails:true,requireFreshPropertyDetails:true}},
 sources:[{...sale,sourceKey:'essex-realty',transaction:'sale'},{...lease,sourceKey:'essex-realty',transaction:'lease'}],listings,totalListings:listings.length}));
"""
    child = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", script],
        cwd=HERE,
        env={
            key: os.environ[key]
            for key in ("PATH", "HOME", "LANG")
            if key in os.environ
        },
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert child.returncode == 0, child.stderr
    return json.loads(child.stdout)


def test_actual_producer_artifact_reuses_path_keys_and_retains_provider_provenance(
    artifact,
):
    ingest.validate_strict_artifact_freshness(artifact)
    rows = [
        ingest.to_row(listing, {}, artifact["runMeta"]["finishedAt"])
        for listing in artifact["listings"]
    ]
    assert [row["external_id"] for row in rows] == [
        "properties/EXISTING_PROPERTY",
        "properties/NEW_PROPERTY",
    ]
    assert [row["raw_data"]["essexPropertyId"] for row in rows] == [
        "1090741",
        "1090742",
    ]
    assert len({row["external_id"] for row in rows}) == 2
    assert all(row["slug"] == "essex-realty" for row in rows)
    assert all(len(row["images"]) == len(row["contacts"]) == 1 for row in rows)
    assert artifact["sources"][0]["totalAvailable"] == 2
    assert artifact["sources"][0]["truncated"] is False
    assert artifact["sources"][1]["listings"] == []
    assert artifact["sources"][1]["totalAvailable"] == 0
