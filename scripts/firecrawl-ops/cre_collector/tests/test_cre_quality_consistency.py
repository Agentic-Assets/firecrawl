"""Operational scope and sparse price/SF contracts, using synthetic records only."""

import re
import sys

import cre_ingest
import cre_validate


def psf_assignment():
    sql = cre_ingest.build_sql([], [], "2026-10-12T00:00:00Z", set())
    return (
        re.search(
            r"        sale_price_per_sf = (.*?)\n        cap_rate", sql, re.DOTALL
        )
        .group(1)
        .rstrip(",")
    )


def test_primary_conflicts_only_gate_active_parents():
    sql = cre_validate.QUERIES["primary_child_conflicts"]
    assert sql.count("JOIN credeals.cre_listings l ON l.id = c.listing_id") == 2
    assert sql.count("l.deleted_at IS NULL") == 2
    historical = cre_validate.HISTORICAL_PRIMARY_CHILD_CONFLICTS_QUERY
    assert historical.count("l.deleted_at IS NOT NULL") == 2
    assert "historical_primary_child_conflicts" not in cre_validate.QUERIES


def test_sparse_psf_fallback_preserves_only_positive_prior_value():
    assignment = psf_assignment()
    assert (
        "CASE WHEN t.sale_price_per_sf > 0 THEN t.sale_price_per_sf END" in assignment
    )
    assert "jllPriceWithheld" in assignment
    assert "first_party_detail_api" in assignment


def postgres_contract_sql(*, psf_only=False):
    statements = [
        """
BEGIN;
SET LOCAL statement_timeout = '10s';
CREATE SCHEMA credeals;
CREATE TABLE credeals.cre_listings (
 id integer PRIMARY KEY, deleted_at timestamptz,
 brokerage_id integer, external_id text, raw_data jsonb, sale_price_per_sf numeric
);
CREATE TABLE credeals.cre_brokerages (id integer PRIMARY KEY, slug text);
INSERT INTO credeals.cre_brokerages VALUES (1, 'cbre'), (2, 'jll');
CREATE TABLE credeals.cre_listing_contacts (listing_id integer, is_primary boolean);
CREATE TABLE credeals.cre_listing_images (listing_id integer, is_primary boolean);
INSERT INTO credeals.cre_listings (id, deleted_at) VALUES (1,NULL),(2,now());
INSERT INTO credeals.cre_listing_contacts VALUES (1,true),(1,true),(2,true),(2,true),(999,true),(999,true);
INSERT INTO credeals.cre_listing_images SELECT * FROM credeals.cre_listing_contacts;
CREATE TEMP TABLE before_contacts AS SELECT * FROM credeals.cre_listing_contacts;
CREATE TEMP TABLE before_images AS SELECT * FROM credeals.cre_listing_images;
"""
    ]
    for name, query in (
        ("active_conflicts", cre_validate.QUERIES["primary_child_conflicts"]),
        (
            "historical_conflicts",
            cre_validate.HISTORICAL_PRIMARY_CHILD_CONFLICTS_QUERY,
        ),
    ):
        statements.append(f"CREATE TEMP TABLE {name} AS {query}")
        statements.append(f"""DO $$ BEGIN
 IF (SELECT count(*) FROM {name} WHERE listings = '1') <> 2 THEN
 RAISE EXCEPTION 'active/historical conflict scope failed'; END IF;
 END $$;""")
    statements.append("""
UPDATE credeals.cre_listings SET deleted_at=now() WHERE id=1;
""")
    statements.append(
        "CREATE TEMP TABLE after_conflicts AS "
        + cre_validate.QUERIES["primary_child_conflicts"]
    )
    statements.append("""DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM after_conflicts) THEN
 RAISE EXCEPTION 'deleted parents still gate operational conflicts'; END IF;
 IF EXISTS ((SELECT * FROM before_contacts EXCEPT ALL SELECT * FROM credeals.cre_listing_contacts)
 UNION ALL (SELECT * FROM credeals.cre_listing_contacts EXCEPT ALL SELECT * FROM before_contacts)) THEN
 RAISE EXCEPTION 'validation mutated historical contact rows'; END IF;
 IF EXISTS ((SELECT * FROM before_images EXCEPT ALL SELECT * FROM credeals.cre_listing_images)
 UNION ALL (SELECT * FROM credeals.cre_listing_images EXCEPT ALL SELECT * FROM before_images)) THEN
 RAISE EXCEPTION 'validation mutated historical image rows'; END IF;
 END $$;
TRUNCATE credeals.cre_listings;
INSERT INTO credeals.cre_listings (id, brokerage_id, external_id, raw_data, sale_price_per_sf) VALUES
 (10,1,'zero','{}',0), (11,1,'negative','{}',-5),
 (12,1,'positive','{}',225), (13,1,'high-positive','{}',20000),
 (14,1,'replacement','{}',0), (15,1,'missing','{}',NULL),
 (16,2,'withheld','{}',225), (17,1,'marker-collision','{}',225);
""")
    statements.append(
        """INSERT INTO credeals.cre_listings AS t
 (id, brokerage_id, external_id, raw_data, sale_price_per_sf) VALUES
 (10,1,'zero','{}',NULL), (11,1,'negative','{}',NULL),
 (12,1,'positive','{}',NULL), (13,1,'high-positive','{}',NULL),
 (14,1,'replacement','{}',300), (15,1,'missing','{}',NULL),
 (16,2,'withheld','{"jllPriceWithheld":true}',NULL),
 (17,1,'marker-collision','{"jllPriceWithheld":true}',NULL)
 ON CONFLICT (id) DO UPDATE SET sale_price_per_sf = """
        + psf_assignment()
        + ";"
    )
    statements.append("""DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM credeals.cre_listings l
 JOIN (VALUES (10,NULL::numeric),(11,NULL),(12,225),(13,20000),
 (14,300),(15,NULL),(16,NULL),(17,225)) expected(id,psf) USING(id)
 WHERE l.sale_price_per_sf IS DISTINCT FROM expected.psf) THEN
 RAISE EXCEPTION 'sparse price/SF preservation contract failed'; END IF;
 END $$;
ROLLBACK;
SELECT 'quality consistency PostgreSQL contract passed';
""")
    if psf_only:
        psf_sql = "\n".join(statements).split("TRUNCATE credeals.cre_listings;", 1)[1]
        return statements[0] + "\nTRUNCATE credeals.cre_listings;" + psf_sql
    return "\n".join(statements)


if __name__ == "__main__":
    print(postgres_contract_sql(psf_only="--psf-only" in sys.argv))
