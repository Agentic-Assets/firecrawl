"""Operational scope and sparse price/SF contracts, using synthetic records only."""

import re
import sys

import cre_ingest
import cre_validate
import pytest


def financial_assignment(column, *, transition=False):
    sql = cre_ingest.build_sql(
        [],
        [],
        "2026-10-12T00:00:00Z",
        set(),
        colliers_first_party_transition=transition,
    )
    return (
        re.search(rf"        {column}\s*= (.*?)\n        [a-z_]+\s*=", sql, re.DOTALL)
        .group(1)
        .rstrip(",")
    )


def psf_assignment():
    return financial_assignment("sale_price_per_sf")


def test_primary_conflicts_only_gate_active_parents():
    sql = cre_validate.QUERIES["primary_child_conflicts"]
    assert sql.count("JOIN credeals.cre_listings l ON l.id = c.listing_id") == 2
    assert sql.count("l.deleted_at IS NULL") == 2
    historical = cre_validate.HISTORICAL_PRIMARY_CHILD_CONFLICTS_QUERY
    assert historical.count("l.deleted_at IS NOT NULL") == 2
    assert "historical_primary_child_conflicts" not in cre_validate.QUERIES


@pytest.mark.parametrize(
    "column,limit",
    [
        ("sale_price_per_sf", 10000),
        ("lease_rate_min", 500),
        ("lease_rate_max", 500),
    ],
)
def test_sparse_financial_fallback_uses_existing_forward_bounds(column, limit):
    assignment = " ".join(financial_assignment(column).split())
    assert (
        f"CASE WHEN t.{column} > 0 AND t.{column} <= {limit} "
        f"THEN t.{column} END" in assignment
    )
    assert "jllPriceWithheld" in assignment
    assert "first_party_detail_api" in assignment


@pytest.mark.parametrize(
    "field,column,limit",
    [
        ("salePricePerSf", "sale_price_per_sf", 10000),
        ("leaseRateMin", "lease_rate_min", 500),
        ("leaseRateMax", "lease_rate_max", 500),
    ],
)
@pytest.mark.parametrize("kind", ["zero", "negative", "valid", "boundary", "over"])
def test_fallback_bounds_match_real_forward_normalizer(field, column, limit, kind):
    value = {
        "zero": 0,
        "negative": -1,
        "valid": 25,
        "boundary": limit,
        "over": limit + 0.01,
    }[kind]
    row = cre_ingest.to_row(
        {
            "id": "bounds-fixture",
            "sourceKey": "cbre",
            "url": "https://example.com/bounds-fixture",
            "title": "Synthetic",
            field: value,
        },
        {},
        "2026-10-12T00:00:00Z",
    )
    assert row[column] == (value if kind in {"valid", "boundary"} else None)


def financial_fallback_contract_sql():
    # Execute exact generated assignments, including existing privacy/transition
    # guards. Every value is synthetic; no historical production row is changed.
    columns = ("sale_price_per_sf", "lease_rate_min", "lease_rate_max")
    cases = [
        ("missing", None, None, None),
        ("zero", 0, None, None),
        ("negative", -1, None, None),
        ("valid", 25, None, 25),
        ("boundary", "limit", None, "limit"),
        ("above", "over", None, None),
        ("replacement", "over", 30, 30),
        ("withheld", 25, None, None),
        ("marker-collision", 25, None, 25),
        ("colliers-transition", 25, None, None),
        ("colliers-ordinary", 25, None, 25),
    ]
    sql = ["INSERT INTO credeals.cre_brokerages VALUES (3, 'colliers');"]
    for column in columns:
        limit = 10000 if column == "sale_price_per_sf" else 500

        def numeric(value, limit=limit):
            if value is None:
                return "NULL"
            return str({"limit": limit, "over": limit + 0.01}.get(value, value))

        sql.append(f"""CREATE TEMP TABLE financial_cases (
 id integer PRIMARY KEY, brokerage_id integer, external_id text,
 raw_data jsonb, {column} numeric);
CREATE TEMP TABLE expected_cases (id integer PRIMARY KEY, value numeric);""")
        prior_rows, new_rows, expected_rows = [], [], []
        for identity, (name, prior, new, expected) in enumerate(cases, start=100):
            brokerage = (
                2 if name == "withheld" else 3 if name.startswith("colliers") else 1
            )
            external = "main:fixture" if name.startswith("colliers") else name
            raw = (
                '{"jllPriceWithheld":true}'
                if name in {"withheld", "marker-collision"}
                else '{"sourceKey":"colliers-main","freshnessProvenance":{"detailScope":"first_party_detail_api"}}'
                if name == "colliers-transition"
                else "{}"
            )
            prior_rows.append(
                f"({identity},{brokerage},'{external}','{{}}',{numeric(prior)})"
            )
            new_rows.append(
                f"({identity},{brokerage},'{external}','{raw}',{numeric(new)})"
            )
            expected_rows.append(f"({identity},{numeric(expected)})")
        sql.append("INSERT INTO financial_cases VALUES " + ",".join(prior_rows) + ";")
        sql.append("INSERT INTO expected_cases VALUES " + ",".join(expected_rows) + ";")
        sql.append(
            "INSERT INTO financial_cases AS t VALUES "
            + ",".join(new_rows)
            + f" ON CONFLICT (id) DO UPDATE SET {column} = "
            + financial_assignment(column, transition=True)
            + ";"
        )
        sql.append(f"""DO $$ BEGIN
 IF EXISTS (SELECT 1 FROM financial_cases l JOIN expected_cases e USING(id)
 WHERE l.{column} IS DISTINCT FROM e.value) THEN
 RAISE EXCEPTION 'bounded {column} fallback/privacy/transition contract failed'; END IF;
 END $$;
DROP TABLE financial_cases, expected_cases;""")
    return "\n".join(sql)


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
 JOIN (VALUES (10,NULL::numeric),(11,NULL),(12,225),(13,NULL),
 (14,300),(15,NULL),(16,NULL),(17,225)) expected(id,psf) USING(id)
 WHERE l.sale_price_per_sf IS DISTINCT FROM expected.psf) THEN
 RAISE EXCEPTION 'sparse price/SF preservation contract failed'; END IF;
 END $$;
ROLLBACK;
SELECT 'quality consistency PostgreSQL contract passed';
""")
    statements.insert(-1, financial_fallback_contract_sql())
    if psf_only:
        psf_sql = "\n".join(statements).split("TRUNCATE credeals.cre_listings;", 1)[1]
        return statements[0] + "\nTRUNCATE credeals.cre_listings;" + psf_sql
    return "\n".join(statements)


if __name__ == "__main__":
    print(postgres_contract_sql(psf_only="--psf-only" in sys.argv))
