"""Native PostgreSQL contract for 021_cre_market_summary_structure_gate.sql (AGENTIC-5129).

Starts a throwaway local cluster (no network, no Docker), loads minimal
stand-ins for ``credeals.cre_listings``, GetCREdata's
``credeals.v_cre_listing_structure_current`` and refresh function, the live
GetCREdata leg gate ``cre_listing_market_evidence_exclusion_v1`` (verbatim copy
in ``fixtures/``), the live pre-021 view and the shape of EQUIRE's
``credeals.get_cre_market_summary(jsonb)`` (RETURNS SETOF the view,
``RETURN QUERY SELECT m.*``), then applies 021, its rollback and 021 again.
Skips when no local PostgreSQL server binaries are installed, and fails instead
when FIRECRAWL_REQUIRE_PG=1. On macOS put the PostgreSQL 17 server first on PATH
(``PATH=/opt/homebrew/opt/postgresql@17/bin:$PATH``): Homebrew's default
``initdb`` comes from libpq, which has no server.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import unittest
import uuid
from pathlib import Path

SQL_DIR = Path(__file__).resolve().parents[1] / "sql"
MIGRATION = SQL_DIR / "021_cre_market_summary_structure_gate.sql"
READBACK = SQL_DIR / "021_cre_market_summary_structure_gate_readback.sql"
ROLLBACK = SQL_DIR / "021_cre_market_summary_structure_gate_rollback.sql"
VIEWS = SQL_DIR / "005_cre_views.sql"
RUNNER = SQL_DIR / "000_run_all.sql"
LEG_GATE = Path(__file__).resolve().parent / "fixtures" / "live_cre_listing_market_evidence_exclusion_v1.sql"
LEG_GATE_LIVE_MD5 = "a7e0b5a8ac36e8bdc513845b7348fcab"
APPROVAL = {
    "CRE_MARKET_SUMMARY_APPROVE": "1",
    "CRE_MARKET_SUMMARY_OPERATOR": "cayman",
    "CRE_MARKET_SUMMARY_APPROVAL_REF": "AGENTIC-5129",
    "CRE_MARKET_SUMMARY_CONFIRM": "APPLY 021_cre_market_summary_structure_gate",
}
ROLLBACK_APPROVAL = {**APPROVAL, "CRE_MARKET_SUMMARY_CONFIRM": "ROLLBACK 021_cre_market_summary_structure_gate"}

ROLES = """
do $$ begin
    create role anon nologin; create role authenticated nologin; create role service_role nologin;
end $$;
"""


def _leg_gate_definition() -> str:
    text = LEG_GATE.read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines(keepends=False) if not line.startswith("-- "))
    return body.strip().removesuffix(";").strip()


def _schema(refresh_version: str = "lsc-2026.11.1") -> str:
    return f"""
create schema credeals;
grant usage on schema credeals to service_role;
create table credeals.cre_listings (
    id uuid primary key,
    city text, state character(2), property_type text, transaction_type text,
    status text not null default 'active', deleted_at timestamptz,
    sale_price_usd numeric, sale_price_per_sf numeric, cap_rate numeric,
    size_sf numeric, occupancy_rate numeric, scraped_at timestamptz default now());
-- Stand-in for GetCREdata's view: one current row per classified listing.
create table credeals.v_cre_listing_structure_current (
    listing_id uuid primary key, classifier_version text not null, review_state text not null,
    interest_kind text not null, asset_scope text not null, price_basis text not null, size_basis text not null,
    structure_tags text[] not null, structure_method jsonb not null,
    is_property_price boolean not null, psf_allowed boolean not null);
grant select on credeals.cre_listings, credeals.v_cre_listing_structure_current to service_role;
-- Stand-in for GetCREdata's refresh: 021 reads only its classifier version constant.
create function credeals.refresh_cre_listing_structure_classifications(
    p_dry_run boolean default false, p_trigger text default 'manual', p_max_rows integer default null)
returns jsonb language plpgsql as $fn$
declare
    c_classifier_version constant text := '{refresh_version}';
begin
    return jsonb_build_object('classifier_version', c_classifier_version);
end;
$fn$;
{_leg_gate_definition()};
grant execute on function credeals.cre_listing_market_evidence_exclusion_v1(text, text, text, text, text, text, text[], text, text)
    to service_role;
-- The live definition read on 2026-11-02 (pg_get_viewdef), before 021.
create view credeals.v_cre_market_summary with (security_invoker = true) as
select city, state, property_type,
    count(*) as listing_count,
    count(*) filter (where transaction_type = any (array['sale', 'sale_or_lease'])) as for_sale_count,
    count(*) filter (where transaction_type = any (array['lease', 'sale_or_lease'])) as for_lease_count,
    round(avg(sale_price_usd) filter (where sale_price_usd is not null)) as avg_price,
    round(avg(sale_price_per_sf) filter (where sale_price_per_sf is not null), 2) as avg_price_per_sf,
    round(percentile_cont(0.5) within group (order by cap_rate::double precision)
          filter (where cap_rate is not null)::numeric, 4) as median_cap_rate,
    round(avg(size_sf) filter (where size_sf is not null)) as avg_size_sf,
    round(avg(occupancy_rate) filter (where occupancy_rate is not null), 4) as avg_occupancy_rate,
    max(scraped_at) as last_scraped_at
  from credeals.cre_listings l
 where deleted_at is null and status = 'active' and city is not null and state is not null
 group by city, state, property_type;
grant select on credeals.v_cre_market_summary to service_role;
-- The shape of EQUIRE supabase/migrations/20260618090000_cre_listings_board_rpc.sql:325.
create function credeals.get_cre_market_summary(filters jsonb)
returns setof credeals.v_cre_market_summary
language plpgsql stable security definer set search_path = '' as $fn$
begin
    return query select m.* from credeals.v_cre_market_summary m
     where (filters->>'city' is null or m.city = filters->>'city')
     order by m.listing_count desc limit 200;
end;
$fn$;
"""


def _row(city, state, ptype, tx="sale", price=None, size=None, cap=None, stored_psf=None, status="active",
         deleted=False, structure=None):
    return {"id": str(uuid.uuid4()), "city": city, "state": state, "property_type": ptype, "transaction_type": tx,
            "status": status, "deleted": deleted, "sale_price_usd": price, "size_sf": size, "cap_rate": cap,
            "sale_price_per_sf": stored_psf, "structure": structure}


# Structure rows as GetCREdata classifies them (gates consistent with the bases).
PRICE = {"classifier_version": "lsc-2026.11.1", "review_state": "auto_accepted", "interest_kind": "unstated",
         "asset_scope": "unstated", "price_basis": "property_price", "size_basis": "building_area", "structure_tags": [],
         "structure_method": {"price_basis": "no_signal", "size_basis": "no_signal"},
         "is_property_price": True, "psf_allowed": True}
NO_SIZE_BASIS = {**PRICE, "size_basis": "unresolved", "psf_allowed": False,
                 "structure_method": {"price_basis": "no_signal", "size_basis": "rule"}}
PLACEHOLDER = {**PRICE, "price_basis": "not_applicable", "structure_tags": ["price_withheld"], "is_property_price": False,
               "psf_allowed": False, "structure_method": {"price_basis": "rule", "size_basis": "no_signal"}}
NO_PSF = {**PRICE, "size_basis": "not_applicable", "psf_allowed": False}
UNPRICED = {**PRICE, "price_basis": "not_applicable", "is_property_price": False, "psf_allowed": False}
LEASED_FEE = {**PRICE, "interest_kind": "leased_fee", "size_basis": "tenant_improvement_area", "psf_allowed": False,
              "structure_method": {"price_basis": "rule", "size_basis": "rule"}}
CONDO_UNIT = {**PRICE, "asset_scope": "unit_within_property", "size_basis": "unit_area",
              "structure_method": {"price_basis": "no_signal", "size_basis": "rule"}}
LAND = {**PRICE, "size_basis": "land_area", "structure_method": {"price_basis": "no_signal", "size_basis": "source_declared"}}
REVIEWED_ON_PRIOR = {**PRICE, "classifier_version": "lsc-2026.11.0", "review_state": "reviewed_confirmed"}

ROWS = [
    # Washington DC "other" (the live D-8 anchor): the $9.9e37 placeholder, one banded ratio
    # ($78.43), one ratio outside the band, an unpriced listing's cap rate (counts), a leased-fee
    # cap rate (excluded), an unclassified sale row and a lease row.
    _row("Washington", "DC", "other", price=9.9e37, size=1, stored_psf=9.9e37, structure=PLACEHOLDER),
    _row("Washington", "DC", "other", price=1000000, size=12750, cap=0.05, structure=PRICE),
    _row("Washington", "DC", "other", price=5000000, size=100, cap=0.07, structure=PRICE),
    _row("Washington", "DC", "other", cap=0.06, structure=UNPRICED),
    _row("Washington", "DC", "other", price=2000000, size=1000, cap=0.50, structure=LEASED_FEE),
    _row("Washington", "DC", "other", price=3000000, size=1000, cap=0.09),
    _row("Washington", "DC", "other", tx="lease", size=5000),
    # Islandia NY industrial: one psf_allowed ratio far outside the band, a stored junk ratio.
    _row("Islandia", "NY", "industrial", price=98000000000, size=1858, stored_psf=52742048, structure=PRICE),
    # Band edges are inclusive: $5 and $3,000 count, $4.99 and $3,000.01 do not; psf_allowed false never
    # counts, even where the leg gate admits the row. Exactly three banded ratios ($5, $100, $3,000):
    # the floor publishes, and median <> mean.
    _row("Austin", "TX", "office", price=50000, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=30000000, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=1000000, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=49900, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=30000100, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=1000000, size=1000, structure=NO_SIZE_BASIS),
    _row("Austin", "TX", "office", price=2000000, size=10000, structure=NO_PSF),
    # Below the floor: two banded ratios and two cap rates publish n and no figure.
    _row("Boise", "ID", "office", price=1000000, size=10000, cap=0.06, structure=PRICE),
    _row("Boise", "ID", "office", price=2000000, size=10000, cap=0.07, structure=PRICE),
    # The leg gate: a condo unit's ratio and a leased-fee cap rate are excluded, the fee-simple rows count.
    _row("Denver", "CO", "industrial", price=1000000, size=10000, cap=0.05, structure=PRICE),
    _row("Denver", "CO", "industrial", price=2000000, size=10000, cap=0.06, structure=PRICE),
    _row("Denver", "CO", "industrial", price=3000000, size=10000, cap=0.07, structure=PRICE),
    _row("Denver", "CO", "industrial", price=10000000, size=10000, structure=CONDO_UNIT),
    _row("Denver", "CO", "industrial", price=4000000, size=10000, cap=0.20, structure=LEASED_FEE),
    # The size leg is for-sale building, portfolio or land area only: lease available space, a
    # tenant improvement area and an unresolved size basis are out, a sale_or_lease row counts,
    # and the median ignores a 635M SF outlier.
    _row("Austin", "TX", "retail", tx="lease", size=5000),
    # A lease listing that still carries a structure row (it moved from sale to lease) is out too.
    _row("Austin", "TX", "retail", tx="lease", size=5500, structure=PRICE),
    _row("Austin", "TX", "retail", price=3000000, size=6000, structure=PRICE),
    _row("Austin", "TX", "retail", price=2000000, size=7000, structure=PRICE),
    _row("Austin", "TX", "retail", tx="sale_or_lease", price=3000000, size=8000, structure=PRICE),
    _row("Austin", "TX", "retail", price=2074285, size=635279040, structure=PRICE),
    _row("Austin", "TX", "retail", price=1500000, size=9999999, structure=NO_SIZE_BASIS),
    _row("Austin", "TX", "retail", price=4000000, size=9000, structure=LEASED_FEE),
    # A market with no property type: the readback must still recount it.
    _row("Reno", "NV", None, price=1000000, size=10000, cap=0.05, structure=PRICE),
    # Land band: property_type land, or a land_area size basis on another type.
    _row("Mesa", "AZ", "land", price=100000, size=1000000, structure=LAND),
    _row("Mesa", "AZ", "land", price=500000, size=1000000, structure=LAND),
    _row("Mesa", "AZ", "land", price=1000000000, size=1000000, structure=LAND),
    _row("Mesa", "AZ", "land", price=50000, size=1000000, structure=LAND),
    _row("Mesa", "AZ", "land", price=1000010000, size=1000000, structure=LAND),
    _row("Mesa", "AZ", "retail", price=4000, size=4000, structure=LAND),
    _row("Mesa", "AZ", "retail", price=8000, size=4000, structure=LAND),
    _row("Mesa", "AZ", "retail", price=12000, size=4000, structure=LAND),
    _row("Mesa", "AZ", "retail", price=8000000, size=4000, structure=LAND),
    # On-market predicate: under_contract and pending count; sold and deleted rows do not.
    # A reviewed row left on the prior classifier version does not block the D-8 guard.
    _row("Tulsa", "OK", "retail", price=1000000, size=10000, structure=REVIEWED_ON_PRIOR),
    _row("Tulsa", "OK", "retail", price=3000000, size=10000, status="under_contract", structure=PRICE),
    _row("Tulsa", "OK", "retail", price=2000000, size=10000, status="pending", structure=PRICE),
    _row("Tulsa", "OK", "retail", price=9000000, size=10000, status="sold", structure=PRICE),
    _row("Tulsa", "OK", "retail", price=9000000, size=10000, deleted=True, structure=PRICE),
]
MARKETS = 10

COLUMNS_BEFORE = [
    ("city", "text"), ("state", "character(2)"), ("property_type", "text"), ("listing_count", "bigint"),
    ("for_sale_count", "bigint"), ("for_lease_count", "bigint"), ("avg_price", "numeric"),
    ("avg_price_per_sf", "numeric"), ("median_cap_rate", "numeric"), ("avg_size_sf", "numeric"),
    ("avg_occupancy_rate", "numeric"), ("last_scraped_at", "timestamp with time zone"),
]
COLUMNS_APPENDED = [("median_price_per_sf", "numeric"), ("price_per_sf_n", "bigint"), ("cap_rate_n", "bigint"),
                    ("median_size_sf", "numeric"), ("size_sf_n", "bigint")]


def _has_local_postgres_server() -> bool:
    initdb = shutil.which("initdb")
    return bool(initdb and shutil.which("pg_ctl") and shutil.which("psql")
                and (Path(initdb).resolve().parent / "postgres").is_file())


_NO_SERVER = ("local PostgreSQL server binaries are not installed "
              "(put /opt/homebrew/opt/postgresql@17/bin first on PATH; libpq's initdb has no server)")
if os.environ.get("FIRECRAWL_REQUIRE_PG") == "1" and not _has_local_postgres_server():
    raise RuntimeError(f"FIRECRAWL_REQUIRE_PG=1 but {_NO_SERVER}")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _num(value):
    return None if value is None else float(value)


@unittest.skipUnless(_has_local_postgres_server(), _NO_SERVER)
class MarketSummaryStructureGateTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.base = Path(tempfile.mkdtemp(prefix="mkt-summary-pg-"))
        cls.env = {**os.environ, "LC_ALL": "C", "PGHOST": str(cls.base), "PGPORT": str(_free_port()),
                   "PGUSER": "postgres"}
        subprocess.run(["initdb", "-D", str(cls.base / "data"), "-A", "trust", "-U", "postgres", "--no-locale",
                        "-E", "UTF8"], check=True, capture_output=True, env=cls.env)
        subprocess.run(["pg_ctl", "-D", str(cls.base / "data"), "-w", "-l", str(cls.base / "log"), "-o",
                        f"-c listen_addresses='' -k {cls.base} -p {cls.env['PGPORT']}", "start"],
                       check=True, capture_output=True, env=cls.env)
        cls.psql(ROLES)
        cls.psql(_schema())
        cls._load()

    @classmethod
    def tearDownClass(cls) -> None:
        subprocess.run(["pg_ctl", "-D", str(cls.base / "data"), "-m", "immediate", "stop"], check=False,
                       capture_output=True, env=cls.env)
        shutil.rmtree(cls.base, ignore_errors=True)

    @classmethod
    def psql(cls, sql: str = "", *, file: Path | None = None, variables: dict | None = None, check: bool = True,
             database: str = "postgres") -> subprocess.CompletedProcess[str]:
        command = ["psql", "-X", "-q", "-At", "-v", "ON_ERROR_STOP=1", "-d", database]
        for name, value in (variables or {}).items():
            command += ["-v", f"{name}={value}"]
        if file is not None:
            command += ["-f", str(file)]
        result = subprocess.run(command, input=sql, text=True, capture_output=True, env=cls.env, check=False)
        if check and result.returncode != 0:
            raise AssertionError(f"psql failed: {result.stderr}")
        return result

    @classmethod
    def value(cls, sql: str, database: str = "postgres"):
        out = cls.psql(sql, database=database).stdout.strip()
        return json.loads(out) if out else None

    @classmethod
    def _load(cls, rows: list[dict] | None = None, database: str = "postgres") -> None:
        rows = json.dumps(ROWS if rows is None else rows)
        cls.psql(f"""
            insert into credeals.cre_listings (id, city, state, property_type, transaction_type, status, deleted_at,
                                               sale_price_usd, size_sf, cap_rate, sale_price_per_sf)
            select (r->>'id')::uuid, r->>'city', r->>'state', r->>'property_type', r->>'transaction_type', r->>'status',
                   case when (r->>'deleted')::boolean then now() end, (r->>'sale_price_usd')::numeric,
                   (r->>'size_sf')::numeric, (r->>'cap_rate')::numeric, (r->>'sale_price_per_sf')::numeric
              from jsonb_array_elements($fx${rows}$fx$::jsonb) r;
            insert into credeals.v_cre_listing_structure_current
            select (r->>'id')::uuid, s->>'classifier_version', s->>'review_state', s->>'interest_kind',
                   s->>'asset_scope', s->>'price_basis', s->>'size_basis',
                   array(select jsonb_array_elements_text(s->'structure_tags')), s->'structure_method',
                   (s->>'is_property_price')::boolean, (s->>'psf_allowed')::boolean
              from jsonb_array_elements($fx${rows}$fx$::jsonb) r cross join lateral (select r->'structure' as s) x
             where jsonb_typeof(r->'structure') = 'object';
        """, database=database)

    def _apply(self, path: Path = MIGRATION, variables: dict | None = None) -> None:
        self.psql(file=path, variables=APPROVAL if variables is None else variables)

    def _market(self, city: str, ptype: str) -> dict:
        return self.value(f"""select to_jsonb(m) from credeals.v_cre_market_summary m
                               where m.city = '{city}' and m.property_type = '{ptype}';""")

    def _columns(self) -> list[tuple[str, str]]:
        rows = self.value("""select jsonb_agg(jsonb_build_array(attname, format_type(atttypid, atttypmod)) order by attnum)
                               from pg_attribute where attrelid = 'credeals.v_cre_market_summary'::regclass
                                and attnum > 0 and not attisdropped;""")
        return [tuple(row) for row in rows]

    def test_01_refuses_without_approval_and_keeps_the_view(self):
        before = self._market("Washington", "other")
        result = self.psql(file=MIGRATION, check=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("021 requires operator, issue and exact confirmation", result.stderr)
        self.assertEqual(self._market("Washington", "other"), before)
        self.assertEqual(self._columns(), COLUMNS_BEFORE)

    def test_02_apply_keeps_columns_and_the_rpc_in_one_session(self):
        self.assertGreater(float(self._market("Washington", "other")["avg_price"]), 1e36)
        # One session: the RPC's cached plan predates the view change.
        script = (f"select count(*) from credeals.get_cre_market_summary('{{}}'::jsonb);\n"
                  f"\\i {MIGRATION}\n"
                  "select row_to_json(r) from credeals.get_cre_market_summary('{\"city\": \"Washington\"}'::jsonb) r;\n")
        out = self.psql(script, variables=APPROVAL).stdout.strip().splitlines()
        self.assertEqual(out[0], str(MARKETS))
        rpc_row = json.loads(out[-1])
        self.assertEqual(list(rpc_row), [name for name, _ in COLUMNS_BEFORE + COLUMNS_APPENDED])
        self.assertEqual(self._columns(), COLUMNS_BEFORE + COLUMNS_APPENDED)
        self._apply()  # re-apply is idempotent
        self.assertEqual(self._columns(), COLUMNS_BEFORE + COLUMNS_APPENDED)

    def test_03_price_legs_are_the_producer_market_evidence(self):
        dc = self._market("Washington", "other")
        self.assertEqual((dc["listing_count"], dc["for_sale_count"], dc["for_lease_count"]), (7, 6, 1))
        self.assertEqual(float(dc["avg_price"]), 2666667)  # 1M, 5M, 2M: no placeholder, no unclassified row
        # One banded ratio: n is published, the figure is not.
        self.assertEqual((dc["price_per_sf_n"], dc["avg_price_per_sf"], dc["median_price_per_sf"]), (1, None, None))
        # 0.05, 0.07 and the unpriced listing's 0.06 count; the leased fee 0.50 and the unclassified 0.09 do not.
        self.assertEqual((dc["cap_rate_n"], _num(dc["median_cap_rate"])), (3, 0.06))
        islandia = self._market("Islandia", "industrial")
        self.assertEqual((islandia["price_per_sf_n"], islandia["median_price_per_sf"]), (0, None))
        self.assertIsNone(islandia["avg_price"])  # one property price is below the floor
        austin = self._market("Austin", "office")
        self.assertEqual(austin["price_per_sf_n"], 3)  # $5, $100 and $3,000 only
        self.assertEqual((_num(austin["avg_price_per_sf"]), _num(austin["median_price_per_sf"])), (1035, 100))
        boise = self._market("Boise", "office")
        self.assertEqual((boise["price_per_sf_n"], boise["cap_rate_n"]), (2, 2))
        self.assertEqual((boise["avg_price_per_sf"], boise["median_price_per_sf"], boise["median_cap_rate"]),
                         (None, None, None))
        denver = self._market("Denver", "industrial")
        self.assertEqual((denver["price_per_sf_n"], _num(denver["median_price_per_sf"])), (3, 200))  # no condo unit
        self.assertEqual((denver["cap_rate_n"], _num(denver["median_cap_rate"])), (3, 0.06))  # no leased fee
        land = self._market("Mesa", "land")
        self.assertEqual((land["price_per_sf_n"], _num(land["median_price_per_sf"])), (3, 0.50))  # $0.05 and $1,000.01 out
        retail_on_land = self._market("Mesa", "retail")
        # A land_area basis takes the land band on any type: $1, $2 and $3/SF count, $2,000/SF does not.
        self.assertEqual((retail_on_land["price_per_sf_n"], _num(retail_on_land["median_price_per_sf"])), (3, 2))
        tulsa = self._market("Tulsa", "retail")
        self.assertEqual(tulsa["listing_count"], 3)
        self.assertEqual((tulsa["price_per_sf_n"], _num(tulsa["median_price_per_sf"])), (3, 200))

    def test_03b_size_leg_median_ignores_outliers(self):
        retail = self._market("Austin", "retail")
        self.assertEqual(retail["size_sf_n"], 4)  # 6,000, 7,000, 8,000 and the outlier; no lease or tenant area
        self.assertEqual(_num(retail["median_size_sf"]), 7500)
        self.assertGreater(_num(retail["avg_size_sf"]), 50000000)  # the legacy mean keeps the outlier
        self.assertEqual(self._market("Boise", "office")["median_size_sf"], None)  # n = 2

    def test_04_access_and_readback(self):
        grants = self.value("""select jsonb_build_object(
            'anon', has_table_privilege('anon', 'credeals.v_cre_market_summary', 'select'),
            'authenticated', has_table_privilege('authenticated', 'credeals.v_cre_market_summary', 'select'),
            'service_role', has_table_privilege('service_role', 'credeals.v_cre_market_summary', 'select'),
            'invoker', (select 'security_invoker=true' = any(reloptions) from pg_class
                         where oid = 'credeals.v_cre_market_summary'::regclass));""")
        self.assertEqual(grants, {"anon": False, "authenticated": False, "service_role": True, "invoker": True})
        self.assertEqual(self.value("set role service_role; select count(*) from credeals.v_cre_market_summary;"), MARKETS)
        readback = self.value(READBACK.read_text(encoding="utf-8"))
        self.assertTrue(readback["reads_structure"] and readback["reads_leg_gate"] and readback["rpc_has_n_columns"])
        self.assertEqual(readback["medians_below_floor"], 0)
        self.assertEqual(readback["n_mismatch"], {"price_per_sf": 0, "cap_rate": 0, "size_sf": 0})
        self.assertEqual(readback["rpc_rows"], readback["markets"])
        self.assertGreater(readback["ratios_outside_band"], 0)
        self.assertEqual(readback["psf_allowed_but_gated"], 1)  # the condo unit
        self.assertIsNone(readback["anchors"]["Washington DC other"]["median_price_per_sf"])
        self.assertEqual(readback["anchors"]["Islandia NY industrial"]["price_per_sf_n"], 0)
        self.assertEqual(_num(readback["anchors"]["Austin TX retail"]["median_size_sf"]), 7500)

    def test_05_rollback_restores_the_prior_values_and_reapply_restores_the_gate(self):
        self._apply(ROLLBACK, ROLLBACK_APPROVAL)
        self.assertEqual(self._columns(), COLUMNS_BEFORE + COLUMNS_APPENDED)
        dc = self._market("Washington", "other")
        self.assertGreater(float(dc["avg_price"]), 1e36)
        for name, _ in COLUMNS_APPENDED:
            self.assertIsNone(dc[name], name)
        self.assertEqual(float(self._market("Islandia", "industrial")["avg_price_per_sf"]), 52742048)
        self.assertEqual(self.value("select count(*) from credeals.get_cre_market_summary('{}'::jsonb);"), MARKETS)
        self.assertEqual(self._market("Tulsa", "retail")["listing_count"], 1)  # the prior status = 'active' predicate
        self._apply()
        self.assertEqual(self._market("Washington", "other")["cap_rate_n"], 3)

    def test_06_refuses_without_the_structure_view(self):
        self.psql("create database bare;")
        self.psql("create schema credeals; create table credeals.cre_listings (id uuid);", database="bare")
        result = self.psql(file=MIGRATION, variables=APPROVAL, check=False, database="bare")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("021 needs credeals.v_cre_listing_structure_current", result.stderr)

    def test_07_refuses_before_d8_is_applied_and_backfilled(self):
        self.psql("create database pre_d8;")
        self.psql(_schema(refresh_version="lsc-2026.11.0"), database="pre_d8")
        result = self.psql(file=MIGRATION, variables=APPROVAL, check=False, database="pre_d8")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("the refresh does not name lsc-2026.11.1", result.stderr)

        self.psql("create database mid_backfill;")
        self.psql(_schema(), database="mid_backfill")
        pending = {**PRICE, "classifier_version": "lsc-2026.11.0"}
        self._load([_row("Tulsa", "OK", "retail", price=1000000, size=10000, structure=pending)], database="mid_backfill")
        result = self.psql(file=MIGRATION, variables=APPROVAL, check=False, database="mid_backfill")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("unreviewed current rows remain on an earlier classifier version", result.stderr)
        self.assertEqual(self.value("select count(*) from pg_attribute where attrelid = "
                                    "'credeals.v_cre_market_summary'::regclass and attname = 'price_per_sf_n';",
                                    database="mid_backfill"), 0)

    def test_08_readback_flags_a_figure_below_the_floor_and_a_wrong_count_on_an_untyped_market(self):
        # A stand-in view: one market per leg publishing a figure from 2 values, and a market with no
        # property type whose $/SF count (2) disagrees with its three base rows.
        self.psql("create database below_floor;")
        self.psql(_schema(), database="below_floor")
        self._load([_row("D", "TX", None, price=1000000, size=10000, structure=PRICE) for _ in range(3)],
                   database="below_floor")
        self.psql("""
            drop function credeals.get_cre_market_summary(jsonb);
            drop view credeals.v_cre_market_summary;
            create view credeals.v_cre_market_summary with (security_invoker = true) as
            select v.city, 'TX'::character(2) as state, v.property_type, 2::bigint as listing_count,
                   2::bigint as for_sale_count, 0::bigint as for_lease_count, null::numeric as avg_price,
                   null::numeric as avg_price_per_sf, v.median_cap_rate, null::numeric as avg_size_sf,
                   null::numeric as avg_occupancy_rate, now() as last_scraped_at, v.median_price_per_sf,
                   v.price_per_sf_n, v.cap_rate_n, v.median_size_sf, v.size_sf_n
              from (values ('A', 'office', 100::numeric, 2::bigint, null::numeric, 2::bigint, null::numeric, 2::bigint),
                           ('B', 'office', null, 2, 0.06, 2, null, 2),
                           ('C', 'office', null, 2, null, 2, 5000, 2),
                           ('D', null, null, 2, null, 0, null, 3))
                   v(city, property_type, median_price_per_sf, price_per_sf_n, median_cap_rate, cap_rate_n,
                     median_size_sf, size_sf_n);
            create function credeals.get_cre_market_summary(filters jsonb)
            returns setof credeals.v_cre_market_summary language sql stable as
            $fn$ select m.* from credeals.v_cre_market_summary m $fn$;
        """, database="below_floor")
        readback = self.value(READBACK.read_text(encoding="utf-8"), database="below_floor")
        self.assertEqual(readback["medians_below_floor"], 3)
        self.assertEqual(readback["n_mismatch"], {"price_per_sf": 1, "cap_rate": 0, "size_sf": 0})


class MarketSummaryHomeTest(unittest.TestCase):
    def test_021_is_the_only_definition_and_not_in_the_generic_runner(self):
        views = VIEWS.read_text(encoding="utf-8")
        self.assertIsNone(re.search(r"create\s+(or\s+replace\s+)?view\s+credeals\.v_cre_market_summary", views, re.IGNORECASE))
        self.assertNotIn("credeals.v_cre_market_summary,", views)
        self.assertNotIn("021_", RUNNER.read_text(encoding="utf-8"))
        migration = MIGRATION.read_text(encoding="utf-8")
        self.assertEqual(len(re.findall(r"create or replace view credeals\.v_cre_market_summary", migration, re.IGNORECASE)), 1)
        view = migration[migration.index("CREATE OR REPLACE VIEW"):migration.index("GROUP BY")]
        self.assertNotIn("sale_price_per_sf", view)  # the stored ratio is never read

    def test_leg_gate_fixture_is_the_live_definition(self):
        # md5(pg_get_functiondef(...)) on the live project; its output ends with a newline.
        self.assertEqual(hashlib.md5((_leg_gate_definition() + "\n").encode("utf-8")).hexdigest(), LEG_GATE_LIVE_MD5)


if __name__ == "__main__":
    unittest.main()
