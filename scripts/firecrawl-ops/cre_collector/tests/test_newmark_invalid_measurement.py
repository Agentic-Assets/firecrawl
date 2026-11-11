"""Check field-local Newmark measurement failures at the ingest boundary."""

import json
import subprocess

import cre_ingest


def test_invalid_measurements_keep_identity_and_price_evidence_without_bad_dimensions():
    script = r'''
import { mapNewmarkNimListing } from './sources/newmark.js';
const units = [['Square Furlongs', 1], ['Acres', -1], ['Units', 12.5], ['Sq. Ft.', 5000]];
const listings = units.map(([unitOfMeasurement,size], index) => ({sourceKey:'newmark',
  transactionMode:'sale', ...mapNewmarkNimListing({id:`provider-${index}`,slug:`property-${index}`,
  priceSummary:'$5,000,000',properties:[{countryCode:'US',stateAbbreviation:'OK',zip:'74103',
  unitOfMeasurement,size}]}, 'sale', '2026-11-11T12:00:00Z', true)}));
for (const [index,priceSummary] of ['$500/unit','$500/acre','$500/month','$1M–$2M',
  '$100-$500/SF','$10,000 million','Subject to Offer ($500/SF)'].entries()) {
  listings.push({sourceKey:'newmark',transactionMode:'sale', ...mapNewmarkNimListing({id:`other-${index}`,
    slug:`other-${index}`,priceSummary,properties:[{countryCode:'US',stateAbbreviation:'OK',zip:'74103',
    unitOfMeasurement:'Square Furlongs',size:1}]},'sale','2026-11-11T12:00:00Z',true)});
}
process.stdout.write(JSON.stringify(listings));
'''
    result = subprocess.run(
        ["node", "--import", "tsx", "--input-type=module", "-e", script],
        capture_output=True, text=True, check=True, timeout=15,
    )
    listings = json.loads(result.stdout)
    rows = [cre_ingest.to_row(listing, {}, "2026-11-11T12:00:00Z", allow_newmark_nim=True)
            for listing in listings]
    assert all(rows)
    for index, reason in enumerate(("unsupported_unit", "negative_size", "fractional_units")):
        row = rows[index]
        assert row["external_id"] == f"property-{index}"
        assert row["size_sf"] is None and row["lot_size_sf"] is None and row["units"] is None
        assert row["sale_price_usd"] == 5_000_000
        assert row["raw_data"]["salePriceText"] == "$5,000,000"
        assert row["raw_data"]["newmarkNimMeasurement"]["invalidReason"] == reason
        assert row["raw_data"]["preserveChildCollections"] is True
    assert rows[3]["size_sf"] == 5000
    assert rows[3]["sale_price_usd"] == 5_000_000
    for row in rows[4:]:
        assert row["sale_price_usd"] is None
        assert row["sale_price_per_sf"] is None
        assert row["raw_data"]["newmarkNimPriceRejected"] == "ambiguous_price_basis"
