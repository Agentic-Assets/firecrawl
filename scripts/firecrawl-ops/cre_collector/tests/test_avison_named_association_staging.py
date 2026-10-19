"""Retained named-property response through the actual mapper and pure staging."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cre_ingest import to_row

COLLECTOR = Path(__file__).resolve().parents[1]
DRIVER = r"""
import fs from 'node:fs';
import {avisonYoungBaseListing, enrichAvisonYoungListing, fetchAvisonYoungDirectDoc,
  indexAvisonYoungOfficialFeedUrls, avisonYoungOfficialUrlKey} from './sources/avison-young.ts';
import {projectAvisonAskingPrice} from './sources/pure/avison-price-visibility.ts';
const prefix='./tests/fixtures/raw_data/avison-young-named-association-17440';
const evidence=JSON.parse(fs.readFileSync(prefix+'.json','utf8'));
const body=fs.readFileSync(prefix+'.html','utf8');
const base=avisonYoungBaseListing(evidence.feed_row,new Map());
const index=indexAvisonYoungOfficialFeedUrls([evidence.feed_row]);
const context={matchingExternalUrlRowIds:index.get(avisonYoungOfficialUrlKey(base.externalUrl))??[]};
process.env.AVISON_YOUNG_DETAIL_TRANSPORT='direct';
globalThis.fetch=async()=>{throw Error('offline staging forbids network')};
const listing=await enrichAvisonYoungListing({...base,sharpLaunchUrl:undefined},false,
  async url=>fetchAvisonYoungDirectDoc(url,30000,async()=>['93.184.216.34'],
    async()=>({status:200,location:null,body})),context);
if(listing.detailError)throw Error(listing.detailError);
console.log(JSON.stringify(projectAvisonAskingPrice({...listing,sourceKey:'avison-young',transactionMode:'lease'})));
"""


def test_named_association_preserves_feed_address_and_offered_facts_in_staging():
    if not shutil.which("node") or not (COLLECTOR / "node_modules/tsx").is_dir():
        pytest.skip("requires documented isolated Node/tsx prerequisites")
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", DRIVER],
        cwd=COLLECTOR, capture_output=True, text=True, timeout=30, check=True,
    )
    listing = json.loads(result.stdout)
    row = to_row(listing, {}, "2026-10-18T00:00:00Z")
    assert row is not None
    assert listing["street"] == "The Shoppes at Glen Laurel"
    assert listing["rawSharpLaunch"]["address"] == listing["street"]
    assert "148 Briarcliff Drive" in listing["markdown"]
    assert "$29.50/SF, NNN" in listing["markdown"]
    assert row["raw_data"]["rawSharpLaunch"] == listing["rawSharpLaunch"]
    assert row["raw_data"]["markdown"] == listing["markdown"]
    assert row["raw_data"]["detailJsonLd"] == listing["detailJsonLd"]
    assert [(item["url"], item.get("docType")) for item in row["documents"]] == [
        (item["url"], item.get("docType")) for item in listing["documents"]
    ]
    assert any(item["url"].endswith("The_Shoppes_at_Glen_Laurel_Retail_Brochure.pdf") for item in row["documents"])
    assert listing["buildingSizeSqft"] == 10000
    # The publisher supplied no annual period; this association grants no new
    # financial normalization or child-replacement authority.
    assert row["lease_rate_min"] is None
    assert row["raw_data"]["rawSharpLaunch"]["availabilities_min_rent"] == 29.5
