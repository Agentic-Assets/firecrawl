"""Native PostgreSQL contract for 021_cre_market_summary_structure_gate.sql (AGENTIC-5129).

Starts a throwaway local cluster (no network, no Docker), loads minimal
stand-ins for ``credeals.cre_listings`` and GetCREdata's
``credeals.v_cre_listing_structure_current``, the live pre-021 view and the
shape of EQUIRE's ``credeals.get_cre_market_summary(jsonb)`` (RETURNS SETOF the
view, ``RETURN QUERY SELECT m.*``), then applies 021, its rollback and 021
again. Skips when no local PostgreSQL server binaries are installed.
"""

from __future__ import annotations

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
APPROVAL = {
    "CRE_MARKET_SUMMARY_APPROVE": "1",
    "CRE_MARKET_SUMMARY_OPERATOR": "cayman",
    "CRE_MARKET_SUMMARY_APPROVAL_REF": "AGENTIC-5129",
    "CRE_MARKET_SUMMARY_CONFIRM": "APPLY 021_cre_market_summary_structure_gate",
}
ROLLBACK_APPROVAL = {**APPROVAL, "CRE_MARKET_SUMMARY_CONFIRM": "ROLLBACK 021_cre_market_summary_structure_gate"}

BOOTSTRAP = """
do $$ begin
    create role anon nologin; create role authenticated nologin; create role service_role nologin;
end $$;
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
    listing_id uuid primary key, is_property_price boolean not null, psf_allowed boolean not null,
    cap_rate_allowed boolean not null, size_basis text not null);
grant select on credeals.cre_listings, credeals.v_cre_listing_structure_current to service_role;
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


PRICE = {"is_property_price": True, "psf_allowed": True, "cap_rate_allowed": True, "size_basis": "building_area"}
NO_PSF = {**PRICE, "psf_allowed": False}
WITHHELD = {"is_property_price": False, "psf_allowed": False, "cap_rate_allowed": False, "size_basis": "building_area"}
LAND = {**PRICE, "size_basis": "land_area"}

ROWS = [
    # Washington DC "other": the $9.9e37 placeholder (withheld by GetCREdata lsc-2026.11.1),
    # one banded ratio ($78.43), one ratio outside the band, a priced row with no size,
    # an unclassified sale row and a lease row.
    _row("Washington", "DC", "other", price=9.9e37, size=1, stored_psf=9.9e37, structure=WITHHELD),
    _row("Washington", "DC", "other", price=1000000, size=12750, cap=0.05, structure=PRICE),
    _row("Washington", "DC", "other", price=5000000, size=100, cap=0.07, structure=PRICE),
    _row("Washington", "DC", "other", price=2000000, cap=0.50, structure={**PRICE, "cap_rate_allowed": False}),
    _row("Washington", "DC", "other", price=3000000, size=1000, cap=0.09),
    _row("Washington", "DC", "other", tx="lease", size=5000),
    # Islandia NY industrial: one psf_allowed ratio far outside the band, a stored junk ratio.
    _row("Islandia", "NY", "industrial", price=98000000000, size=1858, stored_psf=52742048, structure=PRICE),
    # Band edges are inclusive: $5 and $3,000 count, $4.99 and $3,000.01 do not; psf_allowed false never counts.
    # Three banded ratios ($5, $100, $3,000) so the median differs from the mean.
    _row("Austin", "TX", "office", price=50000, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=30000000, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=1000000, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=49900, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=30000100, size=10000, structure=PRICE),
    _row("Austin", "TX", "office", price=1000000, size=1000, structure=NO_PSF),
    # Land band: property_type land, or a land_area size basis on another type.
    _row("Mesa", "AZ", "land", price=100000, size=1000000, structure=LAND),
    _row("Mesa", "AZ", "land", price=50000, size=1000000, structure=LAND),
    _row("Mesa", "AZ", "retail", price=4000, size=4000, structure=LAND),
    _row("Mesa", "AZ", "retail", price=8000000, size=4000, structure=LAND),
    # On-market predicate: under_contract counts; sold and deleted rows do not.
    _row("Tulsa", "OK", "retail", price=1000000, size=10000, structure=PRICE),
    _row("Tulsa", "OK", "retail", price=3000000, size=10000, status="under_contract", structure=PRICE),
    _row("Tulsa", "OK", "retail", price=9000000, size=10000, status="sold", structure=PRICE),
    _row("Tulsa", "OK", "retail", price=9000000, size=10000, deleted=True, structure=PRICE),
]

COLUMNS_BEFORE = [
    ("city", "text"), ("state", "character(2)"), ("property_type", "text"), ("listing_count", "bigint"),
    ("for_sale_count", "bigint"), ("for_lease_count", "bigint"), ("avg_price", "numeric"),
    ("avg_price_per_sf", "numeric"), ("median_cap_rate", "numeric"), ("avg_size_sf", "numeric"),
    ("avg_occupancy_rate", "numeric"), ("last_scraped_at", "timestamp with time zone"),
]


def _has_local_postgres_server() -> bool:
    initdb = shutil.which("initdb")
    return bool(initdb and shutil.which("pg_ctl") and shutil.which("psql")
                and (Path(initdb).resolve().parent / "postgres").is_file())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@unittest.skipUnless(_has_local_postgres_server(), "local PostgreSQL server binaries are not installed")
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
        cls.psql(BOOTSTRAP)
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
    def value(cls, sql: str):
        out = cls.psql(sql).stdout.strip()
        return json.loads(out) if out else None

    @classmethod
    def _load(cls) -> None:
        rows = json.dumps(ROWS)
        cls.psql(f"""
            insert into credeals.cre_listings (id, city, state, property_type, transaction_type, status, deleted_at,
                                               sale_price_usd, size_sf, cap_rate, sale_price_per_sf)
            select (r->>'id')::uuid, r->>'city', r->>'state', r->>'property_type', r->>'transaction_type', r->>'status',
                   case when (r->>'deleted')::boolean then now() end, (r->>'sale_price_usd')::numeric,
                   (r->>'size_sf')::numeric, (r->>'cap_rate')::numeric, (r->>'sale_price_per_sf')::numeric
              from jsonb_array_elements($fx${rows}$fx$::jsonb) r;
            insert into credeals.v_cre_listing_structure_current
            select (r->>'id')::uuid, (s->>'is_property_price')::boolean, (s->>'psf_allowed')::boolean,
                   (s->>'cap_rate_allowed')::boolean, s->>'size_basis'
              from jsonb_array_elements($fx${rows}$fx$::jsonb) r cross join lateral (select r->'structure' as s) x
             where jsonb_typeof(r->'structure') = 'object';
        """)

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
        self.assertEqual(out[0], "6")
        rpc_row = json.loads(out[-1])
        self.assertEqual(list(rpc_row), [name for name, _ in COLUMNS_BEFORE] + ["median_price_per_sf"])
        self.assertEqual(self._columns(), COLUMNS_BEFORE + [("median_price_per_sf", "numeric")])
        self._apply()  # re-apply is idempotent
        self.assertEqual(self._columns(), COLUMNS_BEFORE + [("median_price_per_sf", "numeric")])

    def test_03_price_legs_read_the_structure_gates(self):
        dc = self._market("Washington", "other")
        self.assertEqual((dc["listing_count"], dc["for_sale_count"], dc["for_lease_count"]), (6, 5, 1))
        self.assertEqual(float(dc["avg_price"]), 2666667)  # 1M, 5M, 2M: no placeholder, no unclassified row
        self.assertEqual(float(dc["avg_price_per_sf"]), 78.43)
        self.assertEqual(float(dc["median_price_per_sf"]), 78.43)
        self.assertEqual(float(dc["median_cap_rate"]), 0.06)  # 0.05 and 0.07; 0.50 is not cap_rate_allowed
        islandia = self._market("Islandia", "industrial")
        self.assertIsNone(islandia["avg_price_per_sf"])
        self.assertIsNone(islandia["median_price_per_sf"])
        austin = self._market("Austin", "office")
        self.assertEqual(float(austin["avg_price_per_sf"]), 1035)  # $5, $100 and $3,000 only
        self.assertEqual(float(austin["median_price_per_sf"]), 100)
        land = self._market("Mesa", "land")
        self.assertEqual(float(land["avg_price_per_sf"]), 0.10)  # $0.05 is below the land band
        retail_on_land = self._market("Mesa", "retail")
        # A land_area basis takes the land band on any type: $1/SF counts, $2,000/SF does not.
        self.assertEqual(float(retail_on_land["avg_price_per_sf"]), 1)
        tulsa = self._market("Tulsa", "retail")
        self.assertEqual(tulsa["listing_count"], 2)
        self.assertEqual(float(tulsa["median_price_per_sf"]), 200)

    def test_04_access_and_readback(self):
        grants = self.value("""select jsonb_build_object(
            'anon', has_table_privilege('anon', 'credeals.v_cre_market_summary', 'select'),
            'authenticated', has_table_privilege('authenticated', 'credeals.v_cre_market_summary', 'select'),
            'service_role', has_table_privilege('service_role', 'credeals.v_cre_market_summary', 'select'),
            'invoker', (select 'security_invoker=true' = any(reloptions) from pg_class
                         where oid = 'credeals.v_cre_market_summary'::regclass));""")
        self.assertEqual(grants, {"anon": False, "authenticated": False, "service_role": True, "invoker": True})
        readback = self.value(READBACK.read_text(encoding="utf-8"))
        self.assertTrue(readback["reads_structure"])
        self.assertEqual(readback["psf_outside_band"], 0)
        self.assertEqual(readback["rpc_rows"], readback["markets"])
        self.assertEqual(float(readback["dc_other"]["median_price_per_sf"]), 78.43)
        self.assertIsNone(readback["islandia_industrial"]["avg_price_per_sf"])

    def test_05_rollback_restores_the_prior_values_and_reapply_restores_the_gate(self):
        self._apply(ROLLBACK, ROLLBACK_APPROVAL)
        dc = self._market("Washington", "other")
        self.assertGreater(float(dc["avg_price"]), 1e36)
        self.assertIsNone(dc["median_price_per_sf"])
        self.assertEqual(float(self._market("Islandia", "industrial")["avg_price_per_sf"]), 52742048)
        self.assertEqual(self.value("select count(*) from credeals.get_cre_market_summary('{}'::jsonb);"), 6)
        self._apply()
        self.assertEqual(float(self._market("Washington", "other")["median_price_per_sf"]), 78.43)

    def test_06_refuses_without_the_structure_view(self):
        self.psql("create database bare;")
        self.psql("create schema credeals; create table credeals.cre_listings (id uuid);", database="bare")
        result = self.psql(file=MIGRATION, variables=APPROVAL, check=False, database="bare")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("021 needs credeals.v_cre_listing_structure_current", result.stderr)


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


if __name__ == "__main__":
    unittest.main()
