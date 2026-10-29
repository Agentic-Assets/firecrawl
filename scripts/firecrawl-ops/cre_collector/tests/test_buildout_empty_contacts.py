"""Buildout empty contact observations must not erase prior contacts."""

import re

import cre_ingest as ingest

STAMP = "2026-10-29T00:00:00Z"


def test_staging_empty_missing_and_current_contacts():
    base = {"sourceKey": "leibsohn", "id": "native", "url": "http://www.leibsohn.com/properties/?propertyId=fixture-sale"}
    missing = ingest.to_row(base, {}, STAMP)
    empty = ingest.to_row({**base, "contactsDetailed": []}, {}, STAMP)
    current = ingest.to_row({**base, "contactsDetailed": [{"name": "Synthetic Contact"}]}, {}, STAMP)
    assert missing["contacts"] == []
    assert empty["contacts"] == []
    assert len(current["contacts"]) == 1


def test_generated_contact_only_gate_uses_exact_family_prior_and_empty_set():
    sql = ingest.build_sql([], [], STAMP, set())
    contact = sql[sql.index("CREATE TEMP TABLE _contact_preserve"):sql.index("CREATE TEMP TABLE _contact_refresh")]
    buildout = contact[contact.index("-- An empty Buildout detail contact set"):]
    assert "JOIN _prior_vals p" in buildout
    assert "s.contacts = '[]'::jsonb" in buildout
    assert all(f"'{key}'" in buildout for key in ingest.BUILDOUT_SOURCE_KEYS)
    assert "preserveContactCollections" not in buildout
    assert "DELETE FROM credeals.cre_listing_contacts  WHERE listing_id IN (SELECT id FROM _contact_refresh);" in sql
    for child in ("documents", "images", "media", "links"):
        assert re.search(rf"DELETE FROM credeals\.cre_listing_{child}\s+WHERE listing_id IN \(SELECT id FROM _child_refresh\);", sql)
