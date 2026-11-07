"""Validation boundaries for the disconnected Matthews fill prototype."""
import copy
import unittest

from matthews_partial.fill import stage_observations


def valid():
    url = "https://www.matthews.com/properties/example"
    return {"schema": "matthews-completed-parse-observation/v1", "sourceKey": "matthews", "observation": {
        "id": "example", "url": url, "canonicalUrl": url, "name": "Example", "buildingSizeSqft": 4000,
        "lotSizeAcres": 2, "yearBuilt": 2000, "capRatePct": 6.5, "salePriceUsd": 5000000,
        "inventoryObservedAt": "2026-11-07T00:00:00Z", "detailObservedAt": "2026-11-07T00:01:00Z",
        "freshnessProvenance": {"generationId": "fixture", "method": "matthews_canonical_detail",
             "identityMethod": "canonical_property_url", "cacheDisposition": "live", "detailScope": "detail_page"}}}


def stage(items):
    return stage_observations(items, "fixture", "2026-11-06T23:59:00Z", "2026-11-07T00:02:00Z")


class MatthewsFillTest(unittest.TestCase):
    def test_maps_source_backed_fields_and_holds_transaction_dependent_price(self):
        row = stage([valid()])[0]
        self.assertEqual(row["size_sf"], 4000)
        self.assertEqual(row["lot_size_sf"], 87120)
        self.assertEqual(row["year_built"], 2000)
        self.assertEqual(row["cap_rate"], .065)
        self.assertNotIn("sale_price_usd", row)

    def test_rejects_mismatched_identity_generation_or_clock(self):
        for mutate in (
            lambda x: x["observation"].update(id="different"),
            lambda x: x["observation"].update(canonicalUrl="https://www.matthews.com/properties/other"),
            lambda x: x["observation"]["freshnessProvenance"].update(generationId="other"),
            lambda x: x["observation"].update(detailObservedAt="2026-11-08T00:01:00Z"),
        ):
            item = copy.deepcopy(valid())
            mutate(item)
            with self.subTest(item=item), self.assertRaises(ValueError):
                stage([item])

    def test_rejects_duplicate_identity(self):
        with self.assertRaises(ValueError):
            stage([valid(), valid()])


if __name__ == "__main__":
    unittest.main()
