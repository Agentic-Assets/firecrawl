import copy

import pytest
from run_franklin_site_ingest_postgres import binding, item

import cre_franklin_site_sql as partial


def valid():
    return item("12345678-1234-5123-8123-123456789abc", "a", "2026-11-04T12:00:01Z", "raw")


@pytest.mark.parametrize("mutation", [
    lambda x: x.update(id="123"),
    lambda x: x.update(url="https://attacker.example/properties/for-sale/a"),
    lambda x: x.update(transactionMode="lease"),
    lambda x: x.update(franklinSiteGenerationId=""),
    lambda x: x["franklinSiteObservation"].update(observedAt="bad"),
    lambda x: x["franklinSiteObservation"]["card"]["attributes"].update({"data-offer-type": "FOR_LEASE"}),
])
def test_staging_rejects_invalid_projection(mutation):
    value = valid()
    mutation(value)
    with pytest.raises(ValueError):
        partial.stage([value])


def test_staging_rejects_duplicate_and_mixed_generation():
    first = valid()
    with pytest.raises(ValueError):
        partial.stage([first, copy.deepcopy(first)])
    second = item("87654321-4321-5432-8432-abcdefabcdef", "b", "2026-11-04T12:00:02Z", "raw")
    second["franklinSiteGenerationId"] = "other"
    with pytest.raises(ValueError):
        partial.stage([first, second])


def test_readback_rejects_unbound_held_ids():
    with pytest.raises(ValueError):
        partial.readback_sql([valid()], ["foreign"], binding([valid()]))