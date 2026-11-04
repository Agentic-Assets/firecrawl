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
let body=fs.readFileSync(prefix+'.html','utf8');
const photoCase=JSON.parse(fs.readFileSync(0,'utf8'));
const roles=JSON.parse(fs.readFileSync('./tests/fixtures/raw_data/avison-young-property-photos-17440.json','utf8'));
const headerNamedGallery=roles.gallery_role_counterexample.url;
if(photoCase==='gallery-role')body=body.replace('</body>',`<section id="gallery"><div class="gallery__carousel-images"><a data-fancybox="gallery-carousel" href="${roles.gallery_role_counterexample.linked_url}"><img src="${headerNamedGallery}" alt="Gallery Image 1"></a></div></section></body>`);
if(photoCase==='retained'){
  evidence.feed_row.image_path=roles.feed_image_path;
  body=body.replace('</body>',[...roles.property_urls.slice(0,18),...roles.broker_avatars,roles.tracking_url]
    .map(url=>`<img src="${url}">`).join('')+'</body>');
}
const base=avisonYoungBaseListing(evidence.feed_row,new Map());
if(photoCase==='noise')base.photos=[...roles.broker_avatars,roles.tracking_url];
const index=indexAvisonYoungOfficialFeedUrls([evidence.feed_row]);
const context={matchingExternalUrlRowIds:index.get(avisonYoungOfficialUrlKey(base.externalUrl))??[]};
process.env.AVISON_YOUNG_DETAIL_TRANSPORT='direct';
globalThis.fetch=async()=>{throw Error('offline staging forbids network')};
const listing=await enrichAvisonYoungListing({...base,sharpLaunchUrl:undefined},false,
  async url=>{
    const doc=await fetchAvisonYoungDirectDoc(url,30000,async()=>['93.184.216.34'],async()=>({status:200,location:null,body}));
    if(photoCase==='doc-only')doc.images=[roles.property_urls[0],'//cdn.sharplaunch.com/v2/client-10/retained/relative-only.jpg',...roles.broker_avatars,roles.tracking_url];
    if(photoCase==='noise')doc.images=[...roles.broker_avatars,roles.tracking_url];
    return doc;
  },context);
if(listing.detailError)throw Error(listing.detailError);
console.log(JSON.stringify(projectAvisonAskingPrice({...listing,sourceKey:'avison-young',transactionMode:'lease'})));
"""


@pytest.mark.parametrize("photo_case", ["none", "retained", "doc-only", "noise", "gallery-role"])
def test_named_association_preserves_feed_address_and_offered_facts_in_staging(photo_case):
    if not shutil.which("node") or not (COLLECTOR / "node_modules/tsx").is_dir():
        pytest.skip("requires documented isolated Node/tsx prerequisites")
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", DRIVER],
        cwd=COLLECTOR, input=json.dumps(photo_case), capture_output=True,
        text=True, timeout=30, check=True,
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
    if photo_case != "none":
        roles = json.loads((COLLECTOR / "tests/fixtures/raw_data/avison-young-property-photos-17440.json").read_text())
        expected = set(roles["property_urls"]) if photo_case == "retained" else set()
        if photo_case == "doc-only":
            expected = {roles["property_urls"][0], "https://cdn.sharplaunch.com/v2/client-10/retained/relative-only.jpg"}
        if photo_case == "gallery-role":
            expected = {roles["gallery_role_counterexample"]["url"], roles["gallery_role_counterexample"]["linked_url"]}
        assert set(listing.get("photos", [])) == expected
        assert {item["url"] for item in row["images"]} == expected
        assert listing["photoCount"] == listing["detailScrape"]["photoCount"] == len(row["images"]) == len(expected)
