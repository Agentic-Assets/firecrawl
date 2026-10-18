"""Actual accepted detail mapper to staging, with only the HTTP seam replaced."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from cre_ingest import to_row

COLLECTOR = Path(__file__).resolve().parents[1]
DRIVER = r"""
import fs from 'node:fs';
import {avisonYoungBaseListing, enrichAvisonYoungListing, fetchAvisonYoungDirectDoc} from './sources/avison-young.ts';
import {projectAvisonAskingPrice} from './sources/pure/avison-price-visibility.ts';
const fixtures='./tests/fixtures/raw_data/avison-young-identity-role-';
const feeds=JSON.parse(fs.readFileSync(fixtures+'feed.json','utf8'));
process.env.AVISON_YOUNG_DETAIL_TRANSPORT='direct';
const output=[];
for(const [id, first, tx] of [[18005,1,'sale'],[17384,3,'lease']]) {
  const mapped=[];
  for(const control of [undefined,1]) {
    const feed=feeds.find(row=>row.id===id);
    const base=avisonYoungBaseListing(control===undefined?feed:{...feed,hide_price:control},new Map());
    const urls=[base.sharpLaunchUrl,base.externalUrl];
    const enriched=await enrichAvisonYoungListing({...base,preserveChildCollections:undefined},false,async url=>{
      const body=fs.readFileSync(fixtures+(first+urls.indexOf(url))+'.html','utf8');
      return fetchAvisonYoungDirectDoc(url,30000,async()=>['93.184.216.34'],async()=>({status:200,body}));
    });
    mapped.push(projectAvisonAskingPrice({...enriched,sourceKey:'avison-young',transactionMode:tx}));
  }
  output.push({listing:mapped[0],hidden:mapped[1]});
}
console.log(JSON.stringify(output));
"""


def test_accepted_identity_roles_retain_page_and_feed_financials_in_actual_staging():
    if not shutil.which("node") or not (COLLECTOR / "node_modules/tsx").is_dir():
        pytest.skip("requires documented collector Node/tsx prerequisites")
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", DRIVER],
        cwd=COLLECTOR, capture_output=True, text=True, timeout=30, check=True,
    )
    sale, lease = json.loads(result.stdout)
    for item in (sale, lease):
        listing = item["listing"]
        row = to_row(listing, {}, "2026-10-18T00:00:00Z")
        assert row is not None
        assert row["raw_data"]["markdown"] == listing["markdown"]
        assert row["raw_data"]["rawSharpLaunch"] == listing["rawSharpLaunch"]
        hidden = to_row(item["hidden"], {}, "2026-10-18T00:00:00Z")
        assert hidden is not None
        assert hidden["sale_price_usd"] is None
        assert hidden["lease_rate_min"] == row["lease_rate_min"]
    assert sale["listing"]["salePriceUsd"] == 14500000
    assert sale["listing"]["rawSharpLaunch"]["sale_price"] == 14500000
    assert "14,000,000" in sale["listing"]["markdown"]
    # Typed rates now require the attributed detail facts at Python staging.
    # Feed 19.5 has no period/basis; the differing page text remains available.
    assert lease["listing"].get("leaseRateMin") is None
    assert lease["listing"]["rawSharpLaunch"]["availabilities_max_rent"] == 19.5
    assert "19.50-$20.50" in lease["listing"]["markdown"]
    assert len(lease["listing"]["contactsDetailed"]) == 2
