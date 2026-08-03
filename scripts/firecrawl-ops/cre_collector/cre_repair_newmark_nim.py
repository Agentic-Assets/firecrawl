#!/usr/bin/env python3
"""Repair the bounded Newmark NIM identity/unit migration from one failed run.

The 2026-07-30 Newmark checkpoint correctly failed its final quality gate after
an additive ingest exposed canonical-URL/slug identity collisions and NIM unit
semantics. This utility is deliberately tied to that immutable artifact and
database target. It defaults to a rollback-only preflight whose only writes are
transaction-local temporary tables. ``--apply`` additionally requires an
owner-only preimage path and performs one serializable transaction.
``--verify-apply-rollback`` exercises the forward path without persistence;
``--verify-rollback-roundtrip`` additionally proves the preimage restoration.
``--rollback-preimage`` is the explicit persistent reverse path and refuses
newer inventory or logical-queue drift. Apply is bound to the captured
reviewed-state SHA-256; persistent rollback additionally requires the exact
preimage-file and applied-postimage SHA-256 values printed by apply.
PostgreSQL's audit trigger advances ``updated_at`` during rollback; every other
selected business field is restored exactly and that timestamp disposition is
verified and reported.

This is not a generic deduplication tool. Any drift from the reviewed
35-identity / 27-collision / 8-rename shape aborts before mutation.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlparse

from cre_checkpoint_refresh import SharedLock, canonical_shared_lock_dir
from cre_ingest import (
    database_target_fingerprint_from_url,
    find_psql,
    load_db_url,
    psql_connection_args,
    psql_connection_env,
    sql_lit,
)

EXPECTED_ARTIFACT_SHA256 = (
    "b18d5f5bc5a8f8e24b79cba29e36e51857d49c6547764c230ef6a1802447118c"
)
EXPECTED_DB_TARGET_SHA256 = (
    "faf5d034d1f085ce09dd7afd0cc013dcbf474a81a73dc60fafa6c8884bfdf9ee"
)
EXPECTED_GENERATION = "2026-07-30T031805Z"
EXPECTED_OBSERVED_MIN = "2026-07-30T03:19:18.014Z"
EXPECTED_OBSERVED_MAX = "2026-07-30T03:21:13.958Z"
EXPECTED_LISTINGS = 4_581
EXPECTED_IDENTITIES = 35
EXPECTED_COLLISIONS = 27
EXPECTED_RENAMES = 8
EXPECTED_REJECTED_PRICES = 3
EXPECTED_UNITS = {
    "Sq. Ft.": 3_670,
    "Units": 382,
    "Acres": 338,
    "Hectares": 15,
    "Sq. Meters": 176,
}
PREIMAGE_SCHEMA_VERSION = 3
INTERNAL_POSTIMAGE_FROM_APPLY = "__nim_postimage_from_apply__"
KNOWN_FALLBACK_HOSTS = {"my.rcm1.com", "properties.nmrk.com"}
CANONICAL_HOSTS = {"www.nmrk.com", "nmrk.com"}
IDENTITY_RE = re.compile(r"^[A-Za-z0-9._~-]+$")
DEFAULT_ARTIFACT = Path(__file__).resolve().parent / (
    "out/checkpoint-refresh/2026-07-30T031805Z/sources/newmark.json"
)
DEFAULT_LOCK = canonical_shared_lock_dir()
ADVISORY_LOCK_KEY = 734_251_907_300_318_050


@dataclass(frozen=True)
class PlanRow:
    provider_id: str
    old_id: str
    old_url: str
    canonical_id: str
    canonical_url: str
    transaction_mode: str
    unit: str
    size_sf: float | None
    lot_size_sf: float | None
    units: int | None
    rejected_price: bool

    def as_dict(self) -> dict:
        return {
            "provider_id": self.provider_id,
            "old_id": self.old_id,
            "old_url": self.old_url,
            "canonical_id": self.canonical_id,
            "canonical_url": self.canonical_url,
            "transaction_mode": self.transaction_mode,
            "unit": self.unit,
            "size_sf": self.size_sf,
            "lot_size_sf": self.lot_size_sf,
            "units": self.units,
            "rejected_price": self.rejected_price,
        }


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_identity(value: object, field: str) -> str:
    if not isinstance(value, str) or not IDENTITY_RE.fullmatch(value):
        raise ValueError(f"{field} is not a safe Newmark identity")
    if value in {".", ".."}:
        raise ValueError(f"{field} is not a safe Newmark identity")
    return value


def canonical_identity(record: dict) -> tuple[str, str]:
    slug = safe_identity(record.get("slug"), "slug")
    raw_url = record.get("externalWebsiteUrl")
    if not isinstance(raw_url, str) or not raw_url.strip():
        return slug, f"https://www.nmrk.com/properties/{slug}"
    parsed = urlparse(raw_url.strip())
    host = (parsed.hostname or "").lower()
    if (
        parsed.scheme != "https"
        or parsed.username
        or parsed.password
        or parsed.port is not None
    ):
        raise ValueError("externalWebsiteUrl is not a safe HTTPS URL")
    if host in KNOWN_FALLBACK_HOSTS:
        return slug, f"https://www.nmrk.com/properties/{slug}"
    if host not in CANONICAL_HOSTS:
        raise ValueError(f"unsupported Newmark NIM host: {host or '<missing>'}")
    match = re.fullmatch(r"/properties/([^/]+)/?", parsed.path)
    if not match:
        raise ValueError("unexpected Newmark NIM canonical URL path")
    identity = safe_identity(unquote(match.group(1)), "canonical URL identity")
    return identity, f"https://www.nmrk.com/properties/{match.group(1)}"


def selected_us_property(record: dict) -> dict:
    for prop in record.get("properties") or []:
        if not isinstance(prop, dict):
            continue
        country = str(prop.get("countryCode") or "").strip().upper()
        state = str(prop.get("stateAbbreviation") or "").strip().upper()
        zipcode = str(prop.get("zip") or "").strip()
        if country == "US" or (
            not country
            and re.fullmatch(r"[A-Z]{2}", state)
            and re.match(r"^\d{5}(?:-\d{4})?$", zipcode)
        ):
            return prop
    raise ValueError(f"provider record {record.get('id')} has no admitted US property")


def positive_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    return numeric if numeric > 0 else None


def normalized_measurement(prop: dict) -> tuple[str, float | None, float | None, int | None]:
    unit = str(prop.get("unitOfMeasurement") or "").strip()
    size = positive_number(prop.get("size"))
    if unit == "Sq. Ft.":
        return unit, size, None, None
    if unit == "Sq. Meters":
        # NIM's `size` is already normalized square feet; `sizeSf` is the
        # display-unit value despite its misleading name.
        return unit, size, None, None
    if unit == "Units":
        if size is not None and not size.is_integer():
            raise ValueError("NIM Units size must be integral")
        return unit, None, None, int(size) if size is not None else None
    if unit == "Acres":
        return unit, None, size * 43_560 if size is not None else None, None
    if unit == "Hectares":
        acres = size * 2.47105381 if size is not None else None
        return unit, None, acres * 43_560 if acres is not None else None, None
    raise ValueError(f"unsupported NIM unit: {unit or '<missing>'}")


def parsed_sale_price(value: object) -> float | None:
    if not isinstance(value, str) or "subject to offer" in value.lower():
        return None
    match = re.search(r"\$\s*([0-9][0-9,]*(?:\.[0-9]+)?)", value)
    if not match:
        return None
    amount = float(match.group(1).replace(",", ""))
    return amount if amount > 0 else None


def load_plan(path: Path) -> list[PlanRow]:
    if sha256_file(path) != EXPECTED_ARTIFACT_SHA256:
        raise ValueError("Newmark repair artifact SHA-256 does not match the reviewed run")
    payload = json.loads(path.read_text())
    listings = payload.get("listings")
    if not isinstance(listings, list) or len(listings) != EXPECTED_LISTINGS:
        raise ValueError(
            f"expected {EXPECTED_LISTINGS} Newmark listings, found "
            f"{len(listings) if isinstance(listings, list) else 'invalid'}"
        )
    generation = (
        payload.get("runMeta", {}).get("freshness", {}).get("generationId")
    )
    if generation != EXPECTED_GENERATION:
        raise ValueError(f"unexpected Newmark generation: {generation!r}")

    rows: list[PlanRow] = []
    for listing in listings:
        if not isinstance(listing, dict):
            raise ValueError("artifact listing is not an object")
        record = listing.get("rawNewmarkNimRecord")
        if not isinstance(record, dict):
            raise ValueError("artifact listing lacks rawNewmarkNimRecord")
        old_id = safe_identity(record.get("slug"), "slug")
        canonical_id, canonical_url = canonical_identity(record)
        old_url = listing.get("source_url") or listing.get("url")
        if not isinstance(old_url, str) or not old_url.startswith("https://"):
            raise ValueError("artifact listing lacks its admitted HTTPS source URL")
        prop = selected_us_property(record)
        unit, size_sf, lot_size_sf, units = normalized_measurement(prop)
        mode = str(listing.get("transactionMode") or "")
        if mode not in {"sale", "lease"}:
            raise ValueError(f"unexpected transaction mode: {mode!r}")
        price = parsed_sale_price(record.get("priceSummary")) if mode == "sale" else None
        rejected = bool(
            price is not None
            and size_sf is not None
            and size_sf > 100
            and price / size_sf > 10_000
        )
        rows.append(
            PlanRow(
                provider_id=str(record.get("id") or ""),
                old_id=old_id,
                old_url=old_url,
                canonical_id=canonical_id,
                canonical_url=canonical_url,
                transaction_mode=mode,
                unit=unit,
                size_sf=size_sf,
                lot_size_sf=lot_size_sf,
                units=units,
                rejected_price=rejected,
            )
        )

    if any(not row.provider_id for row in rows):
        raise ValueError("one or more provider IDs are empty")
    if len({row.old_id for row in rows}) != EXPECTED_LISTINGS:
        raise ValueError("artifact slugs are not unique")
    if len({row.canonical_id for row in rows}) != EXPECTED_LISTINGS:
        raise ValueError("corrected canonical IDs are not unique")
    if len({row.canonical_url for row in rows}) != EXPECTED_LISTINGS:
        raise ValueError("corrected canonical URLs are not unique")
    identities = [row for row in rows if row.old_id != row.canonical_id]
    if len(identities) != EXPECTED_IDENTITIES:
        raise ValueError(
            f"expected {EXPECTED_IDENTITIES} identity changes, found {len(identities)}"
        )
    units = {key: 0 for key in EXPECTED_UNITS}
    for row in rows:
        units[row.unit] = units.get(row.unit, 0) + 1
    if units != EXPECTED_UNITS:
        raise ValueError(f"unexpected NIM unit distribution: {units!r}")
    rejected = sum(row.rejected_price for row in rows)
    if rejected != EXPECTED_REJECTED_PRICES:
        raise ValueError(
            f"expected {EXPECTED_REJECTED_PRICES} rejected prices, found {rejected}"
        )
    return rows


def plan_json(rows: list[PlanRow]) -> str:
    return json.dumps([row.as_dict() for row in rows], separators=(",", ":"))


def generation_expr(alias: str) -> str:
    return f"""COALESCE(
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,secondary_pass,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{primary,freshnessProvenance,generationId}}',''),
      NULLIF({alias}.raw_data #>> '{{secondary_pass,freshnessProvenance,generationId}}','')
    )"""


def inventory_observed_expr(alias: str) -> str:
    return f"""COALESCE(
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,inventoryObservedAt}}','')::timestamptz,
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,primary,inventoryObservedAt}}','')::timestamptz,
      NULLIF({alias}.raw_data #>> '{{latestInventoryObservation,secondary_pass,inventoryObservedAt}}','')::timestamptz,
      NULLIF({alias}.raw_data->>'inventoryObservedAt','')::timestamptz,
      NULLIF({alias}.raw_data #>> '{{primary,inventoryObservedAt}}','')::timestamptz,
      NULLIF({alias}.raw_data #>> '{{secondary_pass,inventoryObservedAt}}','')::timestamptz
    )"""


def stage_sql(rows: list[PlanRow]) -> str:
    value = sql_lit(plan_json(rows))
    return f"""
CREATE TEMP TABLE _nim_plan ON COMMIT DROP AS
SELECT *
FROM jsonb_to_recordset({value}::jsonb) AS p(
  provider_id text,
  old_id text,
  old_url text,
  canonical_id text,
  canonical_url text,
  transaction_mode text,
  unit text,
  size_sf numeric,
  lot_size_sf numeric,
  units integer,
  rejected_price boolean
);

CREATE UNIQUE INDEX ON _nim_plan(old_id);
CREATE UNIQUE INDEX ON _nim_plan(canonical_id);
CREATE UNIQUE INDEX ON _nim_plan(canonical_url);

CREATE TEMP TABLE _nim_identity ON COMMIT DROP AS
SELECT * FROM _nim_plan WHERE old_id <> canonical_id;
"""


def invariant_sql() -> str:
    return f"""
DO $repair$
DECLARE
  brokerage uuid;
  current_rows integer;
  identity_rows integer;
  collisions integer;
  renames integer;
  bad_occupants integer;
  newer_rows integer;
  om_rows integer;
  claimed_queue_rows integer;
  fk_tables text[];
BEGIN
  SELECT id INTO brokerage
  FROM credeals.cre_brokerages
  WHERE slug = 'newmark';
  IF brokerage IS NULL THEN
    RAISE EXCEPTION 'Newmark brokerage is absent';
  END IF;

  SELECT count(*) INTO current_rows
  FROM _nim_plan p
  JOIN credeals.cre_listings l
    ON l.brokerage_id = brokerage
   AND l.external_id = p.old_id
   AND l.source_url = p.old_url
   AND {generation_expr("l")} = {sql_lit(EXPECTED_GENERATION)}
   AND l.deleted_at IS NULL;
  IF current_rows <> {EXPECTED_LISTINGS} THEN
    RAISE EXCEPTION 'expected {EXPECTED_LISTINGS} current-generation rows, found %',
      current_rows;
  END IF;

  SELECT count(*) INTO identity_rows
  FROM _nim_identity p
  JOIN credeals.cre_listings a
    ON a.brokerage_id = brokerage
   AND a.external_id = p.old_id
   AND a.source_url = p.old_url
   AND {generation_expr("a")} = {sql_lit(EXPECTED_GENERATION)}
   AND a.deleted_at IS NULL;
  IF identity_rows <> {EXPECTED_IDENTITIES} THEN
    RAISE EXCEPTION 'expected {EXPECTED_IDENTITIES} identity aliases, found %',
      identity_rows;
  END IF;

  SELECT count(*) INTO collisions
  FROM _nim_identity p
  JOIN credeals.cre_listings a
    ON a.brokerage_id = brokerage
   AND a.external_id = p.old_id
   AND a.source_url = p.old_url
   AND {generation_expr("a")} = {sql_lit(EXPECTED_GENERATION)}
   AND a.deleted_at IS NULL
  JOIN credeals.cre_listings s
    ON s.brokerage_id = brokerage
   AND s.external_id = p.canonical_id
   AND s.source_url = p.canonical_url
   AND s.id <> a.id;
  renames := {EXPECTED_IDENTITIES} - collisions;
  IF collisions <> {EXPECTED_COLLISIONS} OR renames <> {EXPECTED_RENAMES} THEN
    RAISE EXCEPTION 'identity shape drift: collisions=%, renames=%',
      collisions, renames;
  END IF;

  SELECT count(*) INTO bad_occupants
  FROM _nim_identity p
  JOIN credeals.cre_listings occupant
    ON occupant.brokerage_id = brokerage
   AND occupant.external_id = p.canonical_id
   AND occupant.source_url <> p.canonical_url
  WHERE NOT EXISTS (
    SELECT 1
    FROM _nim_identity other
    WHERE other.old_id = occupant.external_id
      AND other.old_url = occupant.source_url
      AND {generation_expr("occupant")} = {sql_lit(EXPECTED_GENERATION)}
      AND occupant.deleted_at IS NULL
  );
  IF bad_occupants <> 0 THEN
    RAISE EXCEPTION 'unexpected canonical-ID occupant rows: %', bad_occupants;
  END IF;

  SELECT count(*) INTO newer_rows
  FROM credeals.cre_listings l
  WHERE l.brokerage_id = brokerage
    AND l.deleted_at IS NULL
    AND {generation_expr("l")} IS NOT NULL
    AND {generation_expr("l")} <> {sql_lit(EXPECTED_GENERATION)}
    AND {inventory_observed_expr("l")}
        > '2026-07-30T03:21:13.989Z'::timestamptz;
  IF newer_rows <> 0 THEN
    RAISE EXCEPTION 'a newer Newmark generation is already active';
  END IF;

  SELECT count(*) INTO om_rows
  FROM credeals.cre_listing_om_facts f
  JOIN credeals.cre_listings l ON l.id = f.listing_id
  WHERE l.brokerage_id = brokerage
    AND EXISTS (
      SELECT 1 FROM _nim_identity p
      WHERE l.external_id IN (p.old_id, p.canonical_id)
    );
  IF om_rows <> 0 THEN
    RAISE EXCEPTION 'identity repair candidates own % OM-facts rows', om_rows;
  END IF;

  SELECT count(*) INTO claimed_queue_rows
  FROM credeals.cre_enrichment_queue q
  WHERE q.brokerage_id = brokerage
    AND q.claimed_at IS NOT NULL
    AND q.done_at IS NULL
    AND EXISTS (
      SELECT 1 FROM _nim_identity p
      WHERE q.external_id IN (p.old_id, p.canonical_id)
    );
  IF claimed_queue_rows <> 0 THEN
    RAISE EXCEPTION 'identity repair candidates have % claimed queue rows',
      claimed_queue_rows;
  END IF;

  SELECT array_agg(
           format('%I.%I', n.nspname, c.relname)
           ORDER BY format('%I.%I', n.nspname, c.relname)
         )
    INTO fk_tables
  FROM pg_constraint con
  JOIN pg_class c ON c.oid = con.conrelid
  JOIN pg_namespace n ON n.oid = c.relnamespace
  WHERE con.contype = 'f'
    AND con.confrelid = 'credeals.cre_listings'::regclass;
  IF fk_tables IS DISTINCT FROM ARRAY[
    'credeals.cre_listing_contacts',
    'credeals.cre_listing_documents',
    'credeals.cre_listing_events',
    'credeals.cre_listing_images',
    'credeals.cre_listing_links',
    'credeals.cre_listing_media',
    'credeals.cre_listing_om_facts',
    'credeals.cre_listing_price_history',
    'credeals.cre_scrape_log'
  ]::text[] THEN
    RAISE EXCEPTION 'unreviewed cre_listings FK surface: %', fk_tables;
  END IF;
END
$repair$;
"""


def preflight_summary_sql() -> str:
    return f"""
WITH b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug = 'newmark'
), aliases AS (
  SELECT p.*, a.id AS alias_id,
         s.id AS survivor_id
  FROM _nim_identity p
  CROSS JOIN b
  JOIN credeals.cre_listings a
    ON a.brokerage_id=b.id
   AND a.external_id=p.old_id
   AND a.source_url=p.old_url
   AND {generation_expr("a")}={sql_lit(EXPECTED_GENERATION)}
   AND a.deleted_at IS NULL
  LEFT JOIN credeals.cre_listings s
    ON s.brokerage_id=b.id
   AND s.external_id=p.canonical_id
   AND s.source_url=p.canonical_url
   AND s.id<>a.id
)
SELECT jsonb_build_object(
  'ok', true,
  'mode', 'rollback_only_preflight',
  'generation', {sql_lit(EXPECTED_GENERATION)},
  'artifactListings', (SELECT count(*) FROM _nim_plan),
  'identityChanges', (SELECT count(*) FROM aliases),
  'collisions', (SELECT count(*) FROM aliases WHERE survivor_id IS NOT NULL),
  'renames', (SELECT count(*) FROM aliases WHERE survivor_id IS NULL),
  'unitCounts', (
    SELECT jsonb_object_agg(unit, count)
    FROM (SELECT unit, count(*) AS count FROM _nim_plan GROUP BY unit ORDER BY unit) q
  ),
  'rejectedPrices', (SELECT count(*) FROM _nim_plan WHERE rejected_price),
  'operationalChildrenOnAliases', (
    SELECT jsonb_build_object(
      'contacts', (SELECT count(*) FROM aliases a JOIN credeals.cre_listing_contacts c ON c.listing_id=a.alias_id),
      'documents', (SELECT count(*) FROM aliases a JOIN credeals.cre_listing_documents d ON d.listing_id=a.alias_id),
      'images', (SELECT count(*) FROM aliases a JOIN credeals.cre_listing_images i ON i.listing_id=a.alias_id),
      'media', (SELECT count(*) FROM aliases a JOIN credeals.cre_listing_media m ON m.listing_id=a.alias_id),
      'links', (SELECT count(*) FROM aliases a JOIN credeals.cre_listing_links l ON l.listing_id=a.alias_id)
    )
  ),
  'historyRetainedOnOriginalUuids', (
    SELECT jsonb_build_object(
      'events', (SELECT count(*) FROM aliases a JOIN credeals.cre_listing_events e ON e.listing_id=a.alias_id),
      'priceHistory', (SELECT count(*) FROM aliases a JOIN credeals.cre_listing_price_history h ON h.listing_id=a.alias_id),
      'scrapeLogs', (SELECT count(*) FROM aliases a JOIN credeals.cre_scrape_log s ON s.listing_id=a.alias_id)
    )
  )
)::text;
"""


def build_preflight_sql(rows: list[PlanRow]) -> str:
    return (
        "BEGIN ISOLATION LEVEL REPEATABLE READ;\n"
        "SET LOCAL statement_timeout = '90s';\n"
        + stage_sql(rows)
        + invariant_sql()
        + preflight_summary_sql()
        + "\nROLLBACK;\n"
    )


def deterministic_uuid_sql(namespace: str, expression: str) -> str:
    return f"md5({sql_lit(namespace)} || ':' || ({expression})::text)::uuid"


def sha256_jsonb_sql(expression: str) -> str:
    return (
        "encode(digest(convert_to(("
        + expression
        + ")::text,'UTF8'),'sha256'),'hex')"
    )


def reviewed_state_ctes_sql() -> str:
    return f"""
b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug='newmark'
), identity_rows AS (
  SELECT DISTINCT l.id
  FROM credeals.cre_listings l
  CROSS JOIN b
  WHERE l.brokerage_id=b.id
    AND EXISTS (
      SELECT 1 FROM _nim_identity p
      WHERE l.external_id IN (p.old_id,p.canonical_id)
    )
), dq AS (
  SELECT l.id, l.size_sf, l.lot_size_sf, l.units,
         l.sale_price_usd, l.sale_price_per_sf, l.updated_at
  FROM _nim_plan p
  CROSS JOIN b
  JOIN credeals.cre_listings l
   ON l.brokerage_id=b.id
   AND l.external_id=p.old_id
   AND l.source_url=p.old_url
   AND {generation_expr("l")}={sql_lit(EXPECTED_GENERATION)}
   AND l.deleted_at IS NULL
), identity_map AS (
  SELECT p.provider_id, p.old_id, p.old_url,
         p.canonical_id, p.canonical_url,
         a.id AS alias_id, s.id AS survivor_id,
         'nim-migration:' || p.provider_id || ':' ||
           substring(md5(p.old_id || ':' || p.canonical_url),1,12) AS temp_id,
         'nim-superseded:' || p.provider_id || ':' ||
           substring(md5(p.old_id || ':' || p.canonical_url),1,12)
             AS superseded_id,
         {generation_expr("a")} AS alias_generation,
         {inventory_observed_expr("a")} AS alias_inventory_observed_at
  FROM _nim_identity p
  CROSS JOIN b
  JOIN credeals.cre_listings a
    ON a.brokerage_id=b.id
   AND a.external_id=p.old_id
   AND a.source_url=p.old_url
   AND {generation_expr("a")}={sql_lit(EXPECTED_GENERATION)}
   AND a.deleted_at IS NULL
  LEFT JOIN credeals.cre_listings s
    ON s.brokerage_id=b.id
   AND s.external_id=p.canonical_id
   AND s.source_url=p.canonical_url
   AND s.id<>a.id
)
"""


def reviewed_state_expr() -> str:
    return """
jsonb_build_object(
  'identityMap', (
    SELECT COALESCE(
      jsonb_agg(to_jsonb(identity_map) ORDER BY old_id),
      '[]'::jsonb
    )
    FROM identity_map
  ),
  'identityListings', (
    SELECT COALESCE(jsonb_agg(to_jsonb(l) ORDER BY l.id), '[]'::jsonb)
    FROM credeals.cre_listings l JOIN identity_rows r USING(id)
  ),
  'dqColumns', (
    SELECT COALESCE(jsonb_agg(to_jsonb(dq) ORDER BY id), '[]'::jsonb) FROM dq
  ),
  'contacts', (
    SELECT COALESCE(jsonb_agg(to_jsonb(c) ORDER BY c.id), '[]'::jsonb)
    FROM credeals.cre_listing_contacts c JOIN identity_rows r ON r.id=c.listing_id
  ),
  'documents', (
    SELECT COALESCE(jsonb_agg(to_jsonb(d) ORDER BY d.id), '[]'::jsonb)
    FROM credeals.cre_listing_documents d JOIN identity_rows r ON r.id=d.listing_id
  ),
  'images', (
    SELECT COALESCE(jsonb_agg(to_jsonb(i) ORDER BY i.id), '[]'::jsonb)
    FROM credeals.cre_listing_images i JOIN identity_rows r ON r.id=i.listing_id
  ),
  'media', (
    SELECT COALESCE(jsonb_agg(to_jsonb(m) ORDER BY m.id), '[]'::jsonb)
    FROM credeals.cre_listing_media m JOIN identity_rows r ON r.id=m.listing_id
  ),
  'links', (
    SELECT COALESCE(jsonb_agg(to_jsonb(k) ORDER BY k.id), '[]'::jsonb)
    FROM credeals.cre_listing_links k JOIN identity_rows r ON r.id=k.listing_id
  ),
  'sourceIndex', (
    SELECT COALESCE(
      jsonb_agg(to_jsonb(si) ORDER BY si.brokerage_id,si.external_id),
      '[]'::jsonb
    )
    FROM credeals.cre_source_index si CROSS JOIN b
    WHERE si.brokerage_id=b.id
      AND EXISTS (
        SELECT 1 FROM _nim_identity p
        WHERE si.external_id IN (p.old_id,p.canonical_id)
      )
  ),
  'queue', (
    SELECT COALESCE(
      jsonb_agg(
        to_jsonb(q)
        ORDER BY q.brokerage_id,q.external_id,q.reason,q.id
      ),
      '[]'::jsonb
    )
    FROM credeals.cre_enrichment_queue q CROSS JOIN b
    WHERE q.brokerage_id=b.id
      AND EXISTS (
        SELECT 1 FROM _nim_identity p
        WHERE q.external_id IN (p.old_id,p.canonical_id)
      )
  ),
  'retainedHistoryCounts', jsonb_build_object(
    'events', (SELECT count(*) FROM credeals.cre_listing_events e JOIN identity_rows r ON r.id=e.listing_id),
    'priceHistory', (SELECT count(*) FROM credeals.cre_listing_price_history h JOIN identity_rows r ON r.id=h.listing_id),
    'scrapeLogs', (SELECT count(*) FROM credeals.cre_scrape_log s JOIN identity_rows r ON r.id=s.listing_id)
  )
)
"""


def reviewed_state_guard_sql(preimage: dict) -> str:
    expected_sha256 = str(preimage.get("reviewedStateSha256", ""))
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("reviewed-state SHA-256 must be lowercase hex")
    preimage_value = sql_lit(json.dumps(preimage, separators=(",", ":")))
    return f"""
CREATE TEMP TABLE _nim_expected_reviewed_payload(payload jsonb) ON COMMIT DROP;
INSERT INTO _nim_expected_reviewed_payload VALUES ({preimage_value}::jsonb);

CREATE TEMP TABLE _nim_live_reviewed_state ON COMMIT DROP AS
WITH {reviewed_state_ctes_sql()},
reviewed_state AS (
  SELECT {reviewed_state_expr()} AS payload
)
SELECT payload, {sha256_jsonb_sql("payload")} AS sha256
FROM reviewed_state;

DO $reviewed_state_guard$
DECLARE
  actual_sha256 text;
  embedded_sha256 text;
BEGIN
  SELECT {sha256_jsonb_sql(
      "payload - ARRAY["
      "'schemaVersion','capturedAt','generation','artifactSha256',"
      "'databaseTargetSha256','reviewedStateSha256'"
      "]::text[]"
  )}
    INTO embedded_sha256
  FROM _nim_expected_reviewed_payload;
  IF embedded_sha256 IS DISTINCT FROM {sql_lit(expected_sha256)} THEN
    RAISE EXCEPTION
      'reviewed Newmark preimage payload digest is invalid: expected %, got %',
      {sql_lit(expected_sha256)}, embedded_sha256;
  END IF;
  SELECT sha256 INTO actual_sha256 FROM _nim_live_reviewed_state;
  IF actual_sha256 IS DISTINCT FROM {sql_lit(expected_sha256)} THEN
    RAISE EXCEPTION
      'reviewed Newmark preimage state drifted: expected %, got %',
      {sql_lit(expected_sha256)}, actual_sha256;
  END IF;
END
$reviewed_state_guard$;
"""


def postimage_state_sql(aliases_table: str, state_table: str) -> str:
    for identifier in (aliases_table, state_table):
        if not re.fullmatch(r"_[a-z0-9_]+", identifier):
            raise ValueError("unsafe postimage state-table identifier")
    return f"""
CREATE TEMP TABLE {state_table} ON COMMIT DROP AS
WITH affected_ids AS (
  SELECT alias_id AS id FROM {aliases_table}
  UNION
  SELECT survivor_id AS id
  FROM {aliases_table}
  WHERE survivor_id IS NOT NULL
), dq AS (
  SELECT l.id, l.size_sf, l.lot_size_sf, l.units,
         l.sale_price_usd, l.sale_price_per_sf, l.updated_at
  FROM _nim_plan p
  CROSS JOIN (
    SELECT DISTINCT brokerage_id FROM {aliases_table}
  ) b
  JOIN credeals.cre_listings l
    ON l.brokerage_id=b.brokerage_id
   AND l.external_id=p.canonical_id
   AND l.source_url=p.canonical_url
   AND l.deleted_at IS NULL
), postimage AS (
  SELECT jsonb_build_object(
    'identityMap', (
      SELECT COALESCE(jsonb_agg(to_jsonb(a) ORDER BY a.old_id),'[]'::jsonb)
      FROM {aliases_table} a
    ),
    'identityListings', (
      SELECT COALESCE(jsonb_agg(to_jsonb(l) ORDER BY l.id),'[]'::jsonb)
      FROM credeals.cre_listings l JOIN affected_ids i USING(id)
    ),
    'dqColumns', (
      SELECT COALESCE(jsonb_agg(to_jsonb(dq) ORDER BY id),'[]'::jsonb) FROM dq
    ),
    'contacts', (
      SELECT COALESCE(jsonb_agg(to_jsonb(c) ORDER BY c.id),'[]'::jsonb)
      FROM credeals.cre_listing_contacts c JOIN affected_ids i ON i.id=c.listing_id
    ),
    'documents', (
      SELECT COALESCE(jsonb_agg(to_jsonb(d) ORDER BY d.id),'[]'::jsonb)
      FROM credeals.cre_listing_documents d JOIN affected_ids i ON i.id=d.listing_id
    ),
    'images', (
      SELECT COALESCE(jsonb_agg(to_jsonb(img) ORDER BY img.id),'[]'::jsonb)
      FROM credeals.cre_listing_images img JOIN affected_ids i ON i.id=img.listing_id
    ),
    'media', (
      SELECT COALESCE(jsonb_agg(to_jsonb(m) ORDER BY m.id),'[]'::jsonb)
      FROM credeals.cre_listing_media m JOIN affected_ids i ON i.id=m.listing_id
    ),
    'links', (
      SELECT COALESCE(jsonb_agg(to_jsonb(k) ORDER BY k.id),'[]'::jsonb)
      FROM credeals.cre_listing_links k JOIN affected_ids i ON i.id=k.listing_id
    ),
    'sourceIndex', (
      SELECT COALESCE(
        jsonb_agg(to_jsonb(si) ORDER BY si.brokerage_id,si.external_id),
        '[]'::jsonb
      )
      FROM credeals.cre_source_index si
      WHERE EXISTS (
        SELECT 1 FROM {aliases_table} a
        WHERE si.brokerage_id=a.brokerage_id
          AND si.external_id IN (
            a.old_id,a.canonical_id,a.temp_id,a.superseded_id
          )
      )
    ),
    'queue', (
      SELECT COALESCE(
        jsonb_agg(
          to_jsonb(q)
          ORDER BY q.brokerage_id,q.external_id,q.reason,q.id
        ),
        '[]'::jsonb
      )
      FROM credeals.cre_enrichment_queue q
      WHERE EXISTS (
        SELECT 1 FROM {aliases_table} a
        WHERE q.brokerage_id=a.brokerage_id
          AND q.external_id IN (
            a.old_id,a.canonical_id,a.temp_id,a.superseded_id
          )
      )
    ),
    'retainedHistoryCounts', jsonb_build_object(
      'events', (SELECT count(*) FROM credeals.cre_listing_events e JOIN affected_ids i ON i.id=e.listing_id),
      'priceHistory', (SELECT count(*) FROM credeals.cre_listing_price_history h JOIN affected_ids i ON i.id=h.listing_id),
      'scrapeLogs', (SELECT count(*) FROM credeals.cre_scrape_log s JOIN affected_ids i ON i.id=s.listing_id)
    )
  ) AS payload
)
SELECT payload, {sha256_jsonb_sql("payload")} AS sha256 FROM postimage;
"""


def build_apply_sql(rows: list[PlanRow], preimage: dict) -> str:
    validate_preimage(preimage)
    contact_id = deterministic_uuid_sql(
        "newmark-nim-contact-v1", "a.survivor_id::text || ':' || c.id::text"
    )
    document_id = deterministic_uuid_sql(
        "newmark-nim-document-v1", "a.survivor_id::text || ':' || d.id::text"
    )
    image_id = deterministic_uuid_sql(
        "newmark-nim-image-v1", "a.survivor_id::text || ':' || i.id::text"
    )
    media_id = deterministic_uuid_sql(
        "newmark-nim-media-v1", "a.survivor_id::text || ':' || m.id::text"
    )
    link_id = deterministic_uuid_sql(
        "newmark-nim-link-v1", "a.survivor_id::text || ':' || k.id::text"
    )
    return f"""
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL statement_timeout = '5min';
SET LOCAL lock_timeout = '15s';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{stage_sql(rows)}
{invariant_sql()}

CREATE TEMP TABLE _nim_aliases ON COMMIT DROP AS
WITH b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug='newmark'
)
SELECT p.*,
       b.id AS brokerage_id,
       a.id AS alias_id,
       s.id AS survivor_id,
       'nim-migration:' || p.provider_id || ':' ||
         substring(md5(p.old_id || ':' || p.canonical_url), 1, 12) AS temp_id,
       'nim-superseded:' || p.provider_id || ':' ||
         substring(md5(p.old_id || ':' || p.canonical_url), 1, 12) AS superseded_id
FROM _nim_identity p
CROSS JOIN b
JOIN credeals.cre_listings a
  ON a.brokerage_id=b.id
 AND a.external_id=p.old_id
 AND a.source_url=p.old_url
 AND {generation_expr("a")}={sql_lit(EXPECTED_GENERATION)}
 AND a.deleted_at IS NULL
LEFT JOIN credeals.cre_listings s
  ON s.brokerage_id=b.id
 AND s.external_id=p.canonical_id
 AND s.source_url=p.canonical_url
 AND s.id<>a.id;

SELECT 1
FROM credeals.cre_listings l
WHERE l.id IN (
  SELECT alias_id FROM _nim_aliases
  UNION
  SELECT survivor_id FROM _nim_aliases WHERE survivor_id IS NOT NULL
)
FOR UPDATE;

SELECT 1
FROM credeals.cre_source_index si
WHERE EXISTS (
  SELECT 1 FROM _nim_aliases a
  WHERE si.brokerage_id=a.brokerage_id
    AND si.external_id IN (a.old_id,a.canonical_id)
)
FOR UPDATE;

SELECT 1
FROM credeals.cre_enrichment_queue q
WHERE EXISTS (
  SELECT 1 FROM _nim_aliases a
  WHERE q.brokerage_id=a.brokerage_id
    AND q.external_id IN (a.old_id,a.canonical_id)
)
FOR UPDATE;

{reviewed_state_guard_sql(preimage)}

-- Snapshot logical identity rows before their overlapping keys are moved.
CREATE TEMP TABLE _nim_si_old ON COMMIT DROP AS
SELECT si.*, a.canonical_id, a.temp_id
FROM credeals.cre_source_index si
JOIN _nim_aliases a
  ON si.brokerage_id=a.brokerage_id AND si.external_id=a.old_id;

CREATE TEMP TABLE _nim_queue_old ON COMMIT DROP AS
SELECT q.*, a.canonical_id, a.temp_id
FROM credeals.cre_enrichment_queue q
JOIN _nim_aliases a
  ON q.brokerage_id=a.brokerage_id AND q.external_id=a.old_id;

UPDATE credeals.cre_source_index si
SET external_id=a.temp_id
FROM _nim_aliases a
WHERE si.brokerage_id=a.brokerage_id AND si.external_id=a.old_id;

UPDATE credeals.cre_enrichment_queue q
SET external_id=a.temp_id
FROM _nim_aliases a
WHERE q.brokerage_id=a.brokerage_id AND q.external_id=a.old_id;

-- Move every alias out of the unique-key namespace first. This handles the
-- overlapping 4-Terrace chain without relying on row update order.
UPDATE credeals.cre_listings l
SET external_id=a.temp_id
FROM _nim_aliases a
WHERE l.id=a.alias_id;

-- Copy only missing current operational children to true collision survivors.
-- Append-only events, price history, and scrape logs intentionally remain on
-- their original UUIDs for auditability.
INSERT INTO credeals.cre_listing_contacts (
  id, listing_id, name, title, license, email, phone, brokerage_name,
  profile_url, avatar_url, vcard_url, is_primary, created_at
)
SELECT {contact_id}, a.survivor_id, c.name, c.title, c.license, c.email,
       c.phone, c.brokerage_name, c.profile_url, c.avatar_url, c.vcard_url,
       false, c.created_at
FROM _nim_aliases a
JOIN credeals.cre_listing_contacts c ON c.listing_id=a.alias_id
WHERE a.survivor_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM credeals.cre_listing_contacts existing
    WHERE existing.listing_id=a.survivor_id
      AND (
        (NULLIF(lower(trim(c.email)), '') IS NOT NULL
         AND lower(trim(existing.email))=lower(trim(c.email)))
        OR (NULLIF(trim(c.profile_url), '') IS NOT NULL
            AND existing.profile_url=c.profile_url)
        OR (
          NULLIF(lower(regexp_replace(trim(c.name), '\\s+', ' ', 'g')), '') IS NOT NULL
          AND lower(regexp_replace(trim(existing.name), '\\s+', ' ', 'g'))
              = lower(regexp_replace(trim(c.name), '\\s+', ' ', 'g'))
          AND COALESCE(regexp_replace(existing.phone, '\\D', '', 'g'), '')
              = COALESCE(regexp_replace(c.phone, '\\D', '', 'g'), '')
        )
      )
  )
ON CONFLICT (id) DO NOTHING;

INSERT INTO credeals.cre_listing_documents (
  id, listing_id, doc_type, title, url, file_size_bytes, scraped_at
)
SELECT {document_id}, a.survivor_id, d.doc_type, d.title, d.url,
       d.file_size_bytes, d.scraped_at
FROM _nim_aliases a
JOIN credeals.cre_listing_documents d ON d.listing_id=a.alias_id
WHERE a.survivor_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM credeals.cre_listing_documents existing
    WHERE existing.listing_id=a.survivor_id AND existing.url=d.url
  )
ON CONFLICT (id) DO NOTHING;

INSERT INTO credeals.cre_listing_images (
  id, listing_id, url, alt_text, is_primary, display_order
)
SELECT {image_id}, a.survivor_id, i.url, i.alt_text, false, i.display_order
FROM _nim_aliases a
JOIN credeals.cre_listing_images i ON i.listing_id=a.alias_id
WHERE a.survivor_id IS NOT NULL
  AND NOT EXISTS (
    SELECT 1 FROM credeals.cre_listing_images existing
    WHERE existing.listing_id=a.survivor_id AND existing.url=i.url
  )
ON CONFLICT (id) DO NOTHING;

INSERT INTO credeals.cre_listing_media (
  id, listing_id, media_type, provider, url, embed_url, title, created_at
)
SELECT {media_id}, a.survivor_id, m.media_type, m.provider, m.url,
       m.embed_url, m.title, m.created_at
FROM _nim_aliases a
JOIN credeals.cre_listing_media m ON m.listing_id=a.alias_id
WHERE a.survivor_id IS NOT NULL
ON CONFLICT (listing_id, media_type, url) DO NOTHING;

INSERT INTO credeals.cre_listing_links (
  id, listing_id, link_type, url, rel, created_at
)
SELECT {link_id}, a.survivor_id, k.link_type, k.url, k.rel, k.created_at
FROM _nim_aliases a
JOIN credeals.cre_listing_links k ON k.listing_id=a.alias_id
WHERE a.survivor_id IS NOT NULL
ON CONFLICT (listing_id, link_type, url) DO NOTHING;

-- A collision survivor may predate the current NIM observation. Promote the
-- alias's current provider fields using the same non-destructive rules as the
-- normal ingest upsert, and preserve the survivor's richer detail evidence
-- while attaching the current inventory observation. This makes the repair
-- independently fresh even if the compensating collector rerun later fails.
UPDATE credeals.cre_listings s
SET transaction_type = CASE
      WHEN s.transaction_type='sale_or_lease' THEN 'sale_or_lease'
      WHEN alias.transaction_type='sale_or_lease' THEN 'sale_or_lease'
      WHEN s.transaction_type IS NULL THEN alias.transaction_type
      WHEN alias.transaction_type IS NOT NULL
       AND alias.transaction_type IS DISTINCT FROM s.transaction_type
        THEN 'sale_or_lease'
      ELSE s.transaction_type
    END,
    property_type=COALESCE(alias.property_type,s.property_type),
    title=COALESCE(alias.title,s.title),
    address=COALESCE(alias.address,s.address),
    city=COALESCE(alias.city,s.city),
    state=COALESCE(alias.state,s.state),
    zip=COALESCE(alias.zip,s.zip),
    lat=COALESCE(alias.lat,s.lat),
    lng=COALESCE(alias.lng,s.lng),
    size_sf=COALESCE(alias.size_sf,s.size_sf),
    lot_size_sf=COALESCE(alias.lot_size_sf,s.lot_size_sf),
    units=COALESCE(alias.units,s.units),
    sale_price_usd=COALESCE(alias.sale_price_usd,s.sale_price_usd),
    sale_price_per_sf=COALESCE(alias.sale_price_per_sf,s.sale_price_per_sf),
    updated_date=COALESCE(alias.updated_date,s.updated_date),
    scraped_at=CASE
      WHEN jsonb_path_exists(alias.raw_data,'$.**.detailError')
        OR (
          jsonb_path_exists(
            alias.raw_data,
            '$.**.preserveChildCollections ? (@ == true || @ == "true")'
          )
          AND NOT jsonb_path_exists(
            alias.raw_data,
            '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
          )
        )
      THEN CASE
        WHEN jsonb_path_exists(s.raw_data,'$.detailError')
          OR jsonb_path_exists(s.raw_data,'$.detailUnavailable')
          OR jsonb_path_exists(s.raw_data,'$.primary.detailError')
          OR jsonb_path_exists(s.raw_data,'$.secondary_pass.detailError')
          OR (
            jsonb_path_exists(
              s.raw_data,
              '$.**.preserveChildCollections ? (@ == true || @ == "true")'
            )
            AND NOT jsonb_path_exists(
              s.raw_data,
              '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
            )
          )
        THEN NULL
        ELSE s.scraped_at
      END
      ELSE alias.scraped_at
    END,
    raw_data=CASE
      WHEN jsonb_path_exists(alias.raw_data,'$.**.detailError')
        OR (
          jsonb_path_exists(
            alias.raw_data,
            '$.**.preserveChildCollections ? (@ == true || @ == "true")'
          )
          AND NOT jsonb_path_exists(
            alias.raw_data,
            '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
          )
        )
      THEN (
        CASE
          WHEN jsonb_path_exists(s.raw_data,'$.detailError')
            OR jsonb_path_exists(s.raw_data,'$.detailUnavailable')
            OR jsonb_path_exists(s.raw_data,'$.primary.detailError')
            OR jsonb_path_exists(s.raw_data,'$.secondary_pass.detailError')
            OR (
              jsonb_path_exists(
                s.raw_data,
                '$.**.preserveChildCollections ? (@ == true || @ == "true")'
              )
              AND NOT jsonb_path_exists(
                s.raw_data,
                '$.**.detailObservedWithChildPreservation ? (@ == true || @ == "true")'
              )
            )
          THEN '{{}}'::jsonb
          ELSE COALESCE(s.raw_data,'{{}}'::jsonb)
        END
      ) || jsonb_build_object(
        'sourceKey',COALESCE(
          alias.raw_data->'sourceKey',
          alias.raw_data#>'{{primary,sourceKey}}',
          alias.raw_data#>'{{secondary_pass,sourceKey}}',
          s.raw_data->'sourceKey',
          s.raw_data#>'{{primary,sourceKey}}',
          s.raw_data#>'{{secondary_pass,sourceKey}}'
        ),
        'latestInventoryObservation',COALESCE(
          alias.raw_data->'latestInventoryObservation',
          alias.raw_data
        ),
        'inventoryObservedAt',COALESCE(
          alias.raw_data->'inventoryObservedAt',
          to_jsonb(alias.scraped_at)
        )
      )
      ELSE alias.raw_data
    END,
    source_lastmod=COALESCE(alias.source_lastmod,s.source_lastmod),
    canonical_key=COALESCE(alias.canonical_key,s.canonical_key),
    updated_at=clock_timestamp()
FROM _nim_aliases a
JOIN credeals.cre_listings alias ON alias.id=a.alias_id
WHERE a.survivor_id IS NOT NULL
  AND s.id=a.survivor_id;

-- Collision aliases retain their original UUID/history but leave the active
-- identity namespace. Rename-only rows keep their UUID and become canonical.
UPDATE credeals.cre_listings l
SET external_id = CASE
      WHEN a.survivor_id IS NULL THEN a.canonical_id
      ELSE a.superseded_id
    END,
    deleted_at = CASE
      WHEN a.survivor_id IS NULL THEN l.deleted_at
      ELSE clock_timestamp()
    END,
    raw_data = CASE
      WHEN a.survivor_id IS NULL THEN l.raw_data
      ELSE COALESCE(l.raw_data, '{{}}'::jsonb) || jsonb_build_object(
        'newmarkNimIdentityRepair',
        jsonb_build_object(
          'generationId', {sql_lit(EXPECTED_GENERATION)},
          'oldExternalId', a.old_id,
          'canonicalExternalId', a.canonical_id,
          'canonicalListingId', a.survivor_id,
          'disposition', 'superseded_duplicate'
        )
      )
    END,
    source_url = CASE
      WHEN a.survivor_id IS NULL THEN a.canonical_url
      ELSE l.source_url
    END,
    canonical_url = CASE
      WHEN a.survivor_id IS NULL THEN a.canonical_url
      ELSE l.canonical_url
    END
FROM _nim_aliases a
WHERE l.id=a.alias_id;

-- Reconcile source-index rows into canonical identities. It is a mutable
-- current-source snapshot, not append-only history.
INSERT INTO credeals.cre_source_index AS target (
  brokerage_id, external_id, source_key, url, source_lastmod, fingerprint,
  soft_deleted, observed_status, first_seen, last_seen, last_enumerated_at,
  prior_sale_price, prior_lease_rate, prior_status
)
SELECT old.brokerage_id, old.canonical_id, old.source_key,
       regexp_replace(old.url, '/properties/[^/?#]+', '/properties/' || old.canonical_id),
       old.source_lastmod, old.fingerprint, false, old.observed_status,
       old.first_seen, old.last_seen, old.last_enumerated_at,
       old.prior_sale_price, old.prior_lease_rate, old.prior_status
FROM _nim_si_old old
ON CONFLICT (brokerage_id, external_id) DO UPDATE SET
  source_key=EXCLUDED.source_key,
  url=CASE WHEN EXCLUDED.last_seen >= target.last_seen THEN EXCLUDED.url ELSE target.url END,
  source_lastmod=GREATEST(target.source_lastmod, EXCLUDED.source_lastmod),
  fingerprint=CASE WHEN EXCLUDED.last_seen >= target.last_seen THEN EXCLUDED.fingerprint ELSE target.fingerprint END,
  soft_deleted=false,
  observed_status=CASE WHEN EXCLUDED.last_seen >= target.last_seen THEN EXCLUDED.observed_status ELSE target.observed_status END,
  first_seen=LEAST(target.first_seen, EXCLUDED.first_seen),
  last_seen=GREATEST(target.last_seen, EXCLUDED.last_seen),
  last_enumerated_at=GREATEST(target.last_enumerated_at, EXCLUDED.last_enumerated_at),
  prior_sale_price=COALESCE(target.prior_sale_price, EXCLUDED.prior_sale_price),
  prior_lease_rate=COALESCE(target.prior_lease_rate, EXCLUDED.prior_lease_rate),
  prior_status=COALESCE(target.prior_status, EXCLUDED.prior_status);

DELETE FROM credeals.cre_source_index si
USING _nim_si_old old
WHERE si.brokerage_id=old.brokerage_id AND si.external_id=old.temp_id;

-- Queue identities are logical, not FKs. Merge unclaimed work per reason
-- without resetting attempts or retry history.
INSERT INTO credeals.cre_enrichment_queue AS target (
  id, brokerage_id, source_key, external_id, url, reason, priority,
  enqueued_at, claimed_at, done_at, attempts, last_error
)
SELECT md5('newmark-nim-queue-v1:' || old.id::text || ':' || old.canonical_id)::uuid,
       old.brokerage_id, old.source_key, old.canonical_id,
       regexp_replace(old.url, '/properties/[^/?#]+', '/properties/' || old.canonical_id),
       old.reason, old.priority, old.enqueued_at, NULL, old.done_at,
       old.attempts, old.last_error
FROM _nim_queue_old old
ON CONFLICT (brokerage_id, external_id, reason) DO UPDATE SET
  priority=GREATEST(target.priority, EXCLUDED.priority),
  enqueued_at=LEAST(target.enqueued_at, EXCLUDED.enqueued_at),
  done_at=CASE
    WHEN target.done_at IS NULL OR EXCLUDED.done_at IS NULL THEN NULL
    ELSE GREATEST(target.done_at, EXCLUDED.done_at)
  END,
  attempts=GREATEST(target.attempts, EXCLUDED.attempts),
  last_error=COALESCE(target.last_error, EXCLUDED.last_error),
  url=EXCLUDED.url;

DELETE FROM credeals.cre_enrichment_queue q
USING _nim_queue_old old
WHERE q.brokerage_id=old.brokerage_id AND q.external_id=old.temp_id;

-- Explicitly rewrite dimensions. COALESCE-based ingest cannot clear the bad
-- size_sf values previously written for Units/Acres/Hectares.
WITH b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug='newmark'
)
UPDATE credeals.cre_listings l
SET size_sf = CASE
      WHEN p.unit IN ('Sq. Ft.','Sq. Meters') AND p.size_sf IS NOT NULL
        THEN p.size_sf
      WHEN p.unit='Units' AND p.units IS NOT NULL THEN NULL
      WHEN p.unit IN ('Acres','Hectares') AND p.lot_size_sf IS NOT NULL
        THEN NULL
      ELSE l.size_sf
    END,
    lot_size_sf = CASE
      WHEN p.unit IN ('Acres','Hectares') AND p.lot_size_sf IS NOT NULL
        THEN p.lot_size_sf
      ELSE l.lot_size_sf
    END,
    units = CASE
      WHEN p.unit='Units' AND p.units IS NOT NULL THEN p.units
      ELSE l.units
    END,
    sale_price_usd = CASE WHEN p.rejected_price THEN NULL ELSE l.sale_price_usd END,
    sale_price_per_sf = CASE
      WHEN p.rejected_price THEN NULL
      WHEN p.unit='Units' AND p.units IS NOT NULL THEN NULL
      WHEN p.unit IN ('Acres','Hectares') AND p.lot_size_sf IS NOT NULL
        THEN NULL
      WHEN p.transaction_mode='sale' AND p.size_sf IS NOT NULL
           AND l.sale_price_usd IS NOT NULL
        THEN l.sale_price_usd / NULLIF(p.size_sf,0)
      ELSE l.sale_price_per_sf
    END,
    updated_at=clock_timestamp()
FROM _nim_plan p
CROSS JOIN b
WHERE l.brokerage_id=b.id
  AND l.external_id=p.canonical_id
  AND l.source_url=p.canonical_url
  AND l.deleted_at IS NULL;

-- Exact transaction-local readback.
DO $post$
DECLARE
  brokerage uuid;
  current_rows integer;
  old_pairs integer;
  active_duplicate_groups integer;
  superseded_rows integer;
  primary_conflicts integer;
  missing_child_union integer;
  stale_logical_identities integer;
  rejected_prices_remaining integer;
  bad_dimensions integer;
  current_generation_rows integer;
  current_observation_rows integer;
  collision_observation_mismatches integer;
BEGIN
  SELECT id INTO brokerage FROM credeals.cre_brokerages WHERE slug='newmark';

  SELECT count(*) INTO current_rows
  FROM _nim_plan p
  JOIN credeals.cre_listings l
    ON l.brokerage_id=brokerage
   AND l.external_id=p.canonical_id
   AND l.source_url=p.canonical_url
   AND l.deleted_at IS NULL;
  IF current_rows <> {EXPECTED_LISTINGS} THEN
    RAISE EXCEPTION 'postcondition current canonical rows: %', current_rows;
  END IF;

  SELECT count(*) INTO current_generation_rows
  FROM _nim_plan p
  JOIN credeals.cre_listings l
    ON l.brokerage_id=brokerage
   AND l.external_id=p.canonical_id
   AND l.source_url=p.canonical_url
   AND l.deleted_at IS NULL
   AND {generation_expr("l")}={sql_lit(EXPECTED_GENERATION)};
  IF current_generation_rows <> {EXPECTED_LISTINGS} THEN
    RAISE EXCEPTION 'postcondition current-generation canonical rows: %',
      current_generation_rows;
  END IF;

  SELECT count(*) INTO current_observation_rows
  FROM _nim_plan p
  JOIN credeals.cre_listings l
    ON l.brokerage_id=brokerage
   AND l.external_id=p.canonical_id
   AND l.source_url=p.canonical_url
   AND l.deleted_at IS NULL
  WHERE {inventory_observed_expr("l")}
    BETWEEN {sql_lit(EXPECTED_OBSERVED_MIN)}::timestamptz
        AND {sql_lit(EXPECTED_OBSERVED_MAX)}::timestamptz;
  IF current_observation_rows <> {EXPECTED_LISTINGS} THEN
    RAISE EXCEPTION 'postcondition current-observation canonical rows: %',
      current_observation_rows;
  END IF;

  SELECT count(*) INTO collision_observation_mismatches
  FROM _nim_aliases a
  JOIN credeals.cre_listings alias ON alias.id=a.alias_id
  JOIN credeals.cre_listings survivor ON survivor.id=a.survivor_id
  WHERE a.survivor_id IS NOT NULL
    AND {inventory_observed_expr("survivor")}
        IS DISTINCT FROM {inventory_observed_expr("alias")};
  IF collision_observation_mismatches <> 0 THEN
    RAISE EXCEPTION 'postcondition collision observation mismatches: %',
      collision_observation_mismatches;
  END IF;

  SELECT count(*) INTO old_pairs
  FROM _nim_identity p
  JOIN credeals.cre_listings l
    ON l.brokerage_id=brokerage
   AND l.external_id=p.old_id
   AND l.source_url=p.old_url
   AND l.deleted_at IS NULL;
  IF old_pairs <> 0 THEN
    RAISE EXCEPTION 'postcondition active old identity pairs: %', old_pairs;
  END IF;

  SELECT count(*) INTO active_duplicate_groups
  FROM (
    SELECT source_url
    FROM credeals.cre_listings
    WHERE brokerage_id=brokerage AND deleted_at IS NULL
    GROUP BY source_url HAVING count(*) > 1
  ) duplicates;
  IF active_duplicate_groups <> 0 THEN
    RAISE EXCEPTION 'postcondition active duplicate URL groups: %',
      active_duplicate_groups;
  END IF;

  SELECT count(*) INTO superseded_rows
  FROM _nim_aliases a
  JOIN credeals.cre_listings l ON l.id=a.alias_id
  WHERE a.survivor_id IS NOT NULL
    AND l.deleted_at IS NOT NULL
    AND l.external_id=a.superseded_id;
  IF superseded_rows <> {EXPECTED_COLLISIONS} THEN
    RAISE EXCEPTION 'postcondition superseded aliases: %', superseded_rows;
  END IF;

  SELECT count(*) INTO primary_conflicts
  FROM (
    SELECT listing_id FROM credeals.cre_listing_contacts
    GROUP BY listing_id HAVING count(*) FILTER (WHERE is_primary)>1
    UNION ALL
    SELECT listing_id FROM credeals.cre_listing_images
    GROUP BY listing_id HAVING count(*) FILTER (WHERE is_primary)>1
  ) conflicts
  WHERE listing_id IN (
    SELECT survivor_id FROM _nim_aliases WHERE survivor_id IS NOT NULL
  );
  IF primary_conflicts <> 0 THEN
    RAISE EXCEPTION 'postcondition primary child conflicts: %', primary_conflicts;
  END IF;

  SELECT count(*) INTO missing_child_union
  FROM (
    SELECT c.id
    FROM _nim_aliases a
    JOIN credeals.cre_listing_contacts c ON c.listing_id=a.alias_id
    WHERE a.survivor_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM credeals.cre_listing_contacts existing
        WHERE existing.listing_id=a.survivor_id
          AND (
            (NULLIF(lower(trim(c.email)), '') IS NOT NULL
             AND lower(trim(existing.email))=lower(trim(c.email)))
            OR (NULLIF(trim(c.profile_url), '') IS NOT NULL
                AND existing.profile_url=c.profile_url)
            OR (
              NULLIF(lower(regexp_replace(trim(c.name), '\\s+', ' ', 'g')), '') IS NOT NULL
              AND lower(regexp_replace(trim(existing.name), '\\s+', ' ', 'g'))
                  = lower(regexp_replace(trim(c.name), '\\s+', ' ', 'g'))
              AND COALESCE(regexp_replace(existing.phone, '\\D', '', 'g'), '')
                  = COALESCE(regexp_replace(c.phone, '\\D', '', 'g'), '')
            )
          )
      )
    UNION ALL
    SELECT d.id
    FROM _nim_aliases a
    JOIN credeals.cre_listing_documents d ON d.listing_id=a.alias_id
    WHERE a.survivor_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM credeals.cre_listing_documents existing
        WHERE existing.listing_id=a.survivor_id AND existing.url=d.url
      )
    UNION ALL
    SELECT i.id
    FROM _nim_aliases a
    JOIN credeals.cre_listing_images i ON i.listing_id=a.alias_id
    WHERE a.survivor_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM credeals.cre_listing_images existing
        WHERE existing.listing_id=a.survivor_id AND existing.url=i.url
      )
    UNION ALL
    SELECT m.id
    FROM _nim_aliases a
    JOIN credeals.cre_listing_media m ON m.listing_id=a.alias_id
    WHERE a.survivor_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM credeals.cre_listing_media existing
        WHERE existing.listing_id=a.survivor_id
          AND existing.media_type=m.media_type
          AND existing.url=m.url
      )
    UNION ALL
    SELECT k.id
    FROM _nim_aliases a
    JOIN credeals.cre_listing_links k ON k.listing_id=a.alias_id
    WHERE a.survivor_id IS NOT NULL
      AND NOT EXISTS (
        SELECT 1 FROM credeals.cre_listing_links existing
        WHERE existing.listing_id=a.survivor_id
          AND existing.link_type=k.link_type
          AND existing.url=k.url
      )
  ) missing;
  IF missing_child_union <> 0 THEN
    RAISE EXCEPTION 'postcondition missing child-union rows: %',
      missing_child_union;
  END IF;

  SELECT count(*) INTO stale_logical_identities
  FROM (
    SELECT si.external_id
    FROM credeals.cre_source_index si
    JOIN _nim_aliases a ON si.brokerage_id=a.brokerage_id
    WHERE si.external_id=a.temp_id
       OR (si.external_id=a.old_id AND si.url=a.old_url)
    UNION ALL
    SELECT q.external_id
    FROM credeals.cre_enrichment_queue q
    JOIN _nim_aliases a ON q.brokerage_id=a.brokerage_id
    WHERE q.external_id=a.temp_id
       OR (q.external_id=a.old_id AND q.url=a.old_url)
  ) stale;
  IF stale_logical_identities <> 0 THEN
    RAISE EXCEPTION 'postcondition stale source-index/queue identities: %',
      stale_logical_identities;
  END IF;

  SELECT count(*) INTO rejected_prices_remaining
  FROM _nim_plan p
  JOIN credeals.cre_listings l
    ON l.brokerage_id=brokerage
   AND l.external_id=p.canonical_id
   AND l.source_url=p.canonical_url
   AND l.deleted_at IS NULL
  WHERE p.rejected_price
    AND (l.sale_price_usd IS NOT NULL OR l.sale_price_per_sf IS NOT NULL);
  IF rejected_prices_remaining <> 0 THEN
    RAISE EXCEPTION 'postcondition rejected prices remain: %',
      rejected_prices_remaining;
  END IF;

  SELECT count(*) INTO bad_dimensions
  FROM _nim_plan p
  JOIN credeals.cre_listings l
    ON l.brokerage_id=brokerage
   AND l.external_id=p.canonical_id
   AND l.source_url=p.canonical_url
   AND l.deleted_at IS NULL
  WHERE (p.unit IN ('Sq. Ft.','Sq. Meters') AND p.size_sf IS NOT NULL
         AND l.size_sf IS DISTINCT FROM p.size_sf)
     OR (p.unit='Units' AND p.units IS NOT NULL
         AND (l.size_sf IS NOT NULL OR l.units IS DISTINCT FROM p.units))
     OR (p.unit IN ('Acres','Hectares') AND p.lot_size_sf IS NOT NULL
         AND (l.size_sf IS NOT NULL OR l.lot_size_sf IS DISTINCT FROM p.lot_size_sf));
  IF bad_dimensions <> 0 THEN
    RAISE EXCEPTION 'postcondition dimension mismatches: %', bad_dimensions;
  END IF;
END
$post$;

{postimage_state_sql("_nim_aliases", "_nim_postimage_state")}

SELECT jsonb_build_object(
  'ok', true,
  'mode', 'applied',
  'generation', {sql_lit(EXPECTED_GENERATION)},
  'canonicalRows', {EXPECTED_LISTINGS},
  'currentGenerationRows', {EXPECTED_LISTINGS},
  'currentObservationRows', {EXPECTED_LISTINGS},
  'collisionsSuperseded', {EXPECTED_COLLISIONS},
  'rowsRenamed', {EXPECTED_RENAMES},
  'rejectedPricesCleared', {EXPECTED_REJECTED_PRICES},
  'postimageSha256', (SELECT sha256 FROM _nim_postimage_state)
)::text;
COMMIT;
"""


def build_preimage_sql(rows: list[PlanRow]) -> str:
    return f"""
BEGIN ISOLATION LEVEL REPEATABLE READ;
SET LOCAL statement_timeout='2min';
{stage_sql(rows)}
{invariant_sql()}
WITH {reviewed_state_ctes_sql()},
reviewed_state AS (
  SELECT {reviewed_state_expr()} AS payload
)
SELECT (
  jsonb_build_object(
  'schemaVersion', {PREIMAGE_SCHEMA_VERSION},
  'capturedAt', to_jsonb(clock_timestamp()),
  'generation', {sql_lit(EXPECTED_GENERATION)},
  'artifactSha256', {sql_lit(EXPECTED_ARTIFACT_SHA256)},
  'databaseTargetSha256', {sql_lit(EXPECTED_DB_TARGET_SHA256)},
  'reviewedStateSha256', {sha256_jsonb_sql("payload")}
  ) || payload
)::text
FROM reviewed_state;
ROLLBACK;
"""


def validate_preimage(payload: dict) -> None:
    if not isinstance(payload, dict):
        raise ValueError("rollback preimage is not an object")
    expected = {
        "schemaVersion": PREIMAGE_SCHEMA_VERSION,
        "generation": EXPECTED_GENERATION,
        "artifactSha256": EXPECTED_ARTIFACT_SHA256,
        "databaseTargetSha256": EXPECTED_DB_TARGET_SHA256,
    }
    for key, value in expected.items():
        if payload.get(key) != value:
            raise ValueError(f"rollback preimage has unexpected {key}")
    required_lists = (
        "identityMap",
        "identityListings",
        "dqColumns",
        "contacts",
        "documents",
        "images",
        "media",
        "links",
        "sourceIndex",
        "queue",
    )
    for key in required_lists:
        if not isinstance(payload.get(key), list):
            raise ValueError(f"rollback preimage lacks list field {key}")
    if not re.fullmatch(
        r"[0-9a-f]{64}",
        str(payload.get("reviewedStateSha256", "")),
    ):
        raise ValueError("rollback preimage reviewed-state SHA-256 is invalid")
    captured_at = payload.get("capturedAt")
    if not isinstance(captured_at, str):
        raise ValueError("rollback preimage lacks capturedAt")
    try:
        parsed_captured_at = datetime.fromisoformat(captured_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("rollback preimage has invalid capturedAt") from exc
    if parsed_captured_at.tzinfo is None:
        raise ValueError("rollback preimage capturedAt lacks a timezone")
    identity_map = payload["identityMap"]
    if len(identity_map) != EXPECTED_IDENTITIES:
        raise ValueError("rollback preimage identity-map count drifted")
    if (
        sum(row.get("survivor_id") is not None for row in identity_map)
        != EXPECTED_COLLISIONS
        or sum(row.get("survivor_id") is None for row in identity_map)
        != EXPECTED_RENAMES
    ):
        raise ValueError("rollback preimage collision/rename shape drifted")
    if any(
        row.get("alias_generation") != EXPECTED_GENERATION
        for row in identity_map
    ):
        raise ValueError("rollback preimage alias generation drifted")
    observed_min = datetime.fromisoformat(
        EXPECTED_OBSERVED_MIN.replace("Z", "+00:00")
    )
    observed_max = datetime.fromisoformat(
        EXPECTED_OBSERVED_MAX.replace("Z", "+00:00")
    )
    try:
        observed_values = [
            datetime.fromisoformat(
                row["alias_inventory_observed_at"].replace("Z", "+00:00")
            )
            for row in identity_map
        ]
    except (AttributeError, KeyError, ValueError) as exc:
        raise ValueError("rollback preimage alias observation is invalid") from exc
    if any(value < observed_min or value > observed_max for value in observed_values):
        raise ValueError("rollback preimage alias observation drifted")
    if len({row.get("alias_id") for row in identity_map}) != EXPECTED_IDENTITIES:
        raise ValueError("rollback preimage alias identities are not unique")
    if len(payload["identityListings"]) != 62:
        raise ValueError("rollback preimage identity-listing count drifted")
    if len(payload["dqColumns"]) != EXPECTED_LISTINGS:
        raise ValueError("rollback preimage DQ count drifted")


def build_rollback_sql(
    rows: list[PlanRow],
    preimage: dict,
    expected_postimage_sha256: str,
) -> str:
    validate_preimage(preimage)
    if expected_postimage_sha256 == INTERNAL_POSTIMAGE_FROM_APPLY:
        expected_postimage_sql = "(SELECT sha256 FROM _nim_postimage_state)"
    else:
        if not re.fullmatch(r"[0-9a-f]{64}", expected_postimage_sha256):
            raise ValueError("expected postimage SHA-256 must be lowercase hex")
        expected_postimage_sql = sql_lit(expected_postimage_sha256)
    preimage_value = sql_lit(json.dumps(preimage, separators=(",", ":")))
    contact_id = deterministic_uuid_sql(
        "newmark-nim-contact-v1", "a.survivor_id::text || ':' || c.id::text"
    )
    document_id = deterministic_uuid_sql(
        "newmark-nim-document-v1", "a.survivor_id::text || ':' || d.id::text"
    )
    image_id = deterministic_uuid_sql(
        "newmark-nim-image-v1", "a.survivor_id::text || ':' || i.id::text"
    )
    media_id = deterministic_uuid_sql(
        "newmark-nim-media-v1", "a.survivor_id::text || ':' || m.id::text"
    )
    link_id = deterministic_uuid_sql(
        "newmark-nim-link-v1", "a.survivor_id::text || ':' || k.id::text"
    )
    return f"""
BEGIN ISOLATION LEVEL SERIALIZABLE;
SET LOCAL statement_timeout='5min';
SET LOCAL lock_timeout='15s';
SELECT pg_advisory_xact_lock({ADVISORY_LOCK_KEY});
{stage_sql(rows)}

CREATE TEMP TABLE _rollback_payload(payload jsonb) ON COMMIT DROP;
INSERT INTO _rollback_payload VALUES ({preimage_value}::jsonb);

DO $preimage_digest_guard$
DECLARE
  actual_sha256 text;
  expected_sha256 text;
BEGIN
  SELECT
    {sha256_jsonb_sql(
        "payload - ARRAY["
        "'schemaVersion','capturedAt','generation','artifactSha256',"
        "'databaseTargetSha256','reviewedStateSha256'"
        "]::text[]"
    )},
    payload->>'reviewedStateSha256'
    INTO actual_sha256, expected_sha256
  FROM _rollback_payload;
  IF actual_sha256 IS DISTINCT FROM expected_sha256 THEN
    RAISE EXCEPTION
      'rollback preimage reviewed-state SHA-256 is invalid: expected %, got %',
      expected_sha256, actual_sha256;
  END IF;
END
$preimage_digest_guard$;

CREATE TEMP TABLE _pre_identity ON COMMIT DROP AS
SELECT *
FROM jsonb_to_recordset(
  (SELECT payload->'identityListings' FROM _rollback_payload)
) AS x(
  id uuid,
  external_id text,
  source_url text,
  canonical_url text,
  transaction_type text,
  property_type text,
  title text,
  address text,
  city text,
  state text,
  zip text,
  lat double precision,
  lng double precision,
  market text,
  submarket text,
  size_sf numeric,
  lot_size_sf numeric,
  units integer,
  sale_price_usd numeric,
  sale_price_per_sf numeric,
  updated_date timestamptz,
  scraped_at timestamptz,
  deleted_at timestamptz,
  raw_data jsonb,
  source_lastmod timestamptz,
  canonical_key text,
  updated_at timestamptz
);

CREATE TEMP TABLE _pre_dq ON COMMIT DROP AS
SELECT *
FROM jsonb_to_recordset(
  (SELECT payload->'dqColumns' FROM _rollback_payload)
) AS x(
  id uuid,
  size_sf numeric,
  lot_size_sf numeric,
  units integer,
  sale_price_usd numeric,
  sale_price_per_sf numeric,
  updated_at timestamptz
);

CREATE TEMP TABLE _pre_contacts ON COMMIT DROP AS
SELECT *
FROM jsonb_populate_recordset(
  NULL::credeals.cre_listing_contacts,
  (SELECT payload->'contacts' FROM _rollback_payload)
);
CREATE TEMP TABLE _pre_documents ON COMMIT DROP AS
SELECT *
FROM jsonb_populate_recordset(
  NULL::credeals.cre_listing_documents,
  (SELECT payload->'documents' FROM _rollback_payload)
);
CREATE TEMP TABLE _pre_images ON COMMIT DROP AS
SELECT *
FROM jsonb_populate_recordset(
  NULL::credeals.cre_listing_images,
  (SELECT payload->'images' FROM _rollback_payload)
);
CREATE TEMP TABLE _pre_media ON COMMIT DROP AS
SELECT *
FROM jsonb_populate_recordset(
  NULL::credeals.cre_listing_media,
  (SELECT payload->'media' FROM _rollback_payload)
);
CREATE TEMP TABLE _pre_links ON COMMIT DROP AS
SELECT *
FROM jsonb_populate_recordset(
  NULL::credeals.cre_listing_links,
  (SELECT payload->'links' FROM _rollback_payload)
);

CREATE TEMP TABLE _rollback_aliases ON COMMIT DROP AS
WITH b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug='newmark'
)
SELECT p.*,
       b.id AS brokerage_id,
       a.id AS alias_id,
       s.id AS survivor_id,
       'nim-migration:' || p.provider_id || ':' ||
         substring(md5(p.old_id || ':' || p.canonical_url),1,12) AS temp_id,
       'nim-superseded:' || p.provider_id || ':' ||
         substring(md5(p.old_id || ':' || p.canonical_url),1,12) AS superseded_id
FROM _nim_identity p
CROSS JOIN b
JOIN _pre_identity a
  ON a.external_id=p.old_id AND a.source_url=p.old_url
LEFT JOIN _pre_identity s
  ON s.external_id=p.canonical_id
 AND s.source_url=p.canonical_url
 AND s.id<>a.id;

{postimage_state_sql("_rollback_aliases", "_nim_live_postimage_state")}

DO $postimage_digest_guard$
DECLARE
  actual_sha256 text;
BEGIN
  SELECT sha256 INTO actual_sha256 FROM _nim_live_postimage_state;
  IF actual_sha256 IS DISTINCT FROM {expected_postimage_sql} THEN
    RAISE EXCEPTION
      'rollback refused: Newmark postimage SHA-256 drifted, expected %, got %',
      {expected_postimage_sql}, actual_sha256;
  END IF;
END
$postimage_digest_guard$;

DO $guard$
DECLARE
  brokerage uuid;
  superseded integer;
  renamed integer;
  newer integer;
  logical_drift integer;
  captured timestamptz;
BEGIN
  SELECT id INTO brokerage FROM credeals.cre_brokerages WHERE slug='newmark';
  IF brokerage IS NULL THEN RAISE EXCEPTION 'Newmark brokerage is absent'; END IF;
  SELECT (payload->>'capturedAt')::timestamptz INTO captured
  FROM _rollback_payload;

  SELECT count(*) INTO superseded
  FROM _rollback_aliases a
  JOIN credeals.cre_listings l ON l.id=a.alias_id
  WHERE a.survivor_id IS NOT NULL
    AND l.external_id=a.superseded_id
    AND l.deleted_at IS NOT NULL;
  SELECT count(*) INTO renamed
  FROM _rollback_aliases a
  JOIN credeals.cre_listings l ON l.id=a.alias_id
  WHERE a.survivor_id IS NULL
    AND l.external_id=a.canonical_id
    AND l.source_url=a.canonical_url
    AND l.deleted_at IS NULL;
  IF superseded<>{EXPECTED_COLLISIONS} OR renamed<>{EXPECTED_RENAMES} THEN
    RAISE EXCEPTION 'rollback repair-state drift: superseded=%, renamed=%',
      superseded, renamed;
  END IF;

  SELECT count(*) INTO newer
  FROM credeals.cre_listings l
  WHERE l.brokerage_id=brokerage
    AND l.deleted_at IS NULL
    AND {generation_expr("l")} IS NOT NULL
    AND {generation_expr("l")} <> {sql_lit(EXPECTED_GENERATION)}
    AND {inventory_observed_expr("l")}
        > '2026-07-30T03:21:13.989Z'::timestamptz;
  IF newer<>0 THEN
    RAISE EXCEPTION 'rollback refused after a newer Newmark generation';
  END IF;

  SELECT count(*) INTO logical_drift
  FROM (
    SELECT si.external_id
    FROM credeals.cre_source_index si
    WHERE si.brokerage_id=brokerage
      AND EXISTS (
        SELECT 1 FROM _rollback_aliases a
        WHERE si.external_id IN (
          a.old_id,a.canonical_id,a.temp_id,a.superseded_id
        )
      )
      AND (
        si.last_seen > captured
        OR si.last_enumerated_at > captured
      )
    UNION ALL
    SELECT q.external_id
    FROM credeals.cre_enrichment_queue q
    WHERE q.brokerage_id=brokerage
      AND EXISTS (
        SELECT 1 FROM _rollback_aliases a
        WHERE q.external_id IN (
          a.old_id,a.canonical_id,a.temp_id,a.superseded_id
        )
      )
      AND (
        q.enqueued_at > captured
        OR q.claimed_at IS NOT NULL
        OR q.done_at > captured
      )
  ) drift;
  IF logical_drift<>0 THEN
    RAISE EXCEPTION 'rollback refused after source-index/queue drift: %',
      logical_drift;
  END IF;
END
$guard$;

SELECT 1 FROM credeals.cre_listings l
WHERE l.id IN (SELECT id FROM _pre_identity)
FOR UPDATE;

-- Remove only deterministic child copies created by this repair.
DELETE FROM credeals.cre_listing_contacts target
USING _rollback_aliases a, _pre_contacts c
WHERE a.survivor_id IS NOT NULL
  AND c.listing_id=a.alias_id
  AND target.id={contact_id};
DELETE FROM credeals.cre_listing_documents target
USING _rollback_aliases a, _pre_documents d
WHERE a.survivor_id IS NOT NULL
  AND d.listing_id=a.alias_id
  AND target.id={document_id};
DELETE FROM credeals.cre_listing_images target
USING _rollback_aliases a, _pre_images i
WHERE a.survivor_id IS NOT NULL
  AND i.listing_id=a.alias_id
  AND target.id={image_id};
DELETE FROM credeals.cre_listing_media target
USING _rollback_aliases a, _pre_media m
WHERE a.survivor_id IS NOT NULL
  AND m.listing_id=a.alias_id
  AND target.id={media_id};
DELETE FROM credeals.cre_listing_links target
USING _rollback_aliases a, _pre_links k
WHERE a.survivor_id IS NOT NULL
  AND k.listing_id=a.alias_id
  AND target.id={link_id};

-- The partial unique index on (brokerage_id, external_id) is not deferrable.
-- Move every affected row out of the overlapping namespace before restoring
-- the preimage, so the 4-Terrace identity chain cannot fail by update order.
UPDATE credeals.cre_listings l
SET external_id='nim-rollback:' || md5(l.id::text)
FROM _pre_identity p
WHERE l.id=p.id;

UPDATE credeals.cre_listings l
SET external_id=p.external_id,
    source_url=p.source_url,
    canonical_url=p.canonical_url,
    transaction_type=p.transaction_type,
    property_type=p.property_type,
    title=p.title,
    address=p.address,
    city=p.city,
    state=p.state,
    zip=p.zip,
    lat=p.lat,
    lng=p.lng,
    market=p.market,
    submarket=p.submarket,
    size_sf=p.size_sf,
    lot_size_sf=p.lot_size_sf,
    units=p.units,
    sale_price_usd=p.sale_price_usd,
    sale_price_per_sf=p.sale_price_per_sf,
    updated_date=p.updated_date,
    scraped_at=p.scraped_at,
    deleted_at=p.deleted_at,
    raw_data=p.raw_data,
    source_lastmod=p.source_lastmod,
    canonical_key=p.canonical_key,
    updated_at=clock_timestamp()
FROM _pre_identity p
WHERE l.id=p.id;

UPDATE credeals.cre_listings l
SET size_sf=p.size_sf,
    lot_size_sf=p.lot_size_sf,
    units=p.units,
    sale_price_usd=p.sale_price_usd,
    sale_price_per_sf=p.sale_price_per_sf,
    updated_at=clock_timestamp()
FROM _pre_dq p
WHERE l.id=p.id;

WITH b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug='newmark'
)
DELETE FROM credeals.cre_source_index si
USING b
WHERE si.brokerage_id=b.id
  AND EXISTS (
    SELECT 1 FROM _rollback_aliases a
    WHERE si.external_id IN (
      a.old_id,a.canonical_id,a.temp_id,a.superseded_id
    )
  );
INSERT INTO credeals.cre_source_index
SELECT *
FROM jsonb_populate_recordset(
  NULL::credeals.cre_source_index,
  (SELECT payload->'sourceIndex' FROM _rollback_payload)
);

WITH b AS (
  SELECT id FROM credeals.cre_brokerages WHERE slug='newmark'
)
DELETE FROM credeals.cre_enrichment_queue q
USING b
WHERE q.brokerage_id=b.id
  AND EXISTS (
    SELECT 1 FROM _rollback_aliases a
    WHERE q.external_id IN (
      a.old_id,a.canonical_id,a.temp_id,a.superseded_id
    )
  );
INSERT INTO credeals.cre_enrichment_queue
SELECT *
FROM jsonb_populate_recordset(
  NULL::credeals.cre_enrichment_queue,
  (SELECT payload->'queue' FROM _rollback_payload)
);

DO $post$
DECLARE
  identity_mismatches integer;
  dq_mismatches integer;
  source_index_mismatches integer;
  queue_mismatches integer;
  rollback_timestamp_rows integer;
  captured timestamptz;
BEGIN
  SELECT (payload->>'capturedAt')::timestamptz INTO captured
  FROM _rollback_payload;
  SELECT count(*) INTO identity_mismatches
  FROM _pre_identity p
  JOIN credeals.cre_listings l USING(id)
  WHERE l.external_id IS DISTINCT FROM p.external_id
     OR l.source_url IS DISTINCT FROM p.source_url
     OR l.canonical_url IS DISTINCT FROM p.canonical_url
     OR l.transaction_type IS DISTINCT FROM p.transaction_type
     OR l.property_type IS DISTINCT FROM p.property_type
     OR l.title IS DISTINCT FROM p.title
     OR l.address IS DISTINCT FROM p.address
     OR l.city IS DISTINCT FROM p.city
     OR l.state IS DISTINCT FROM p.state
     OR l.zip IS DISTINCT FROM p.zip
     OR l.lat IS DISTINCT FROM p.lat
     OR l.lng IS DISTINCT FROM p.lng
     OR l.market IS DISTINCT FROM p.market
     OR l.submarket IS DISTINCT FROM p.submarket
     OR l.size_sf IS DISTINCT FROM p.size_sf
     OR l.lot_size_sf IS DISTINCT FROM p.lot_size_sf
     OR l.units IS DISTINCT FROM p.units
     OR l.sale_price_usd IS DISTINCT FROM p.sale_price_usd
     OR l.sale_price_per_sf IS DISTINCT FROM p.sale_price_per_sf
     OR l.updated_date IS DISTINCT FROM p.updated_date
     OR l.scraped_at IS DISTINCT FROM p.scraped_at
     OR l.deleted_at IS DISTINCT FROM p.deleted_at
     OR l.raw_data IS DISTINCT FROM p.raw_data
     OR l.source_lastmod IS DISTINCT FROM p.source_lastmod
     OR l.canonical_key IS DISTINCT FROM p.canonical_key;
  SELECT count(*) INTO dq_mismatches
  FROM _pre_dq p
  JOIN credeals.cre_listings l USING(id)
  WHERE l.size_sf IS DISTINCT FROM p.size_sf
     OR l.lot_size_sf IS DISTINCT FROM p.lot_size_sf
     OR l.units IS DISTINCT FROM p.units
     OR l.sale_price_usd IS DISTINCT FROM p.sale_price_usd
     OR l.sale_price_per_sf IS DISTINCT FROM p.sale_price_per_sf;
  SELECT count(*) INTO rollback_timestamp_rows
  FROM _pre_dq p
  JOIN credeals.cre_listings l USING(id)
  WHERE l.updated_at >= captured;
  SELECT count(*) INTO source_index_mismatches
  FROM (
    (SELECT to_jsonb(si) FROM credeals.cre_source_index si
     WHERE EXISTS (
       SELECT 1 FROM _rollback_aliases a
       WHERE si.external_id IN (a.old_id,a.canonical_id)
     )
     EXCEPT
     SELECT value FROM jsonb_array_elements(
       (SELECT payload->'sourceIndex' FROM _rollback_payload)
     ))
    UNION ALL
    (SELECT value FROM jsonb_array_elements(
       (SELECT payload->'sourceIndex' FROM _rollback_payload)
     )
     EXCEPT
     SELECT to_jsonb(si) FROM credeals.cre_source_index si
     WHERE EXISTS (
       SELECT 1 FROM _rollback_aliases a
       WHERE si.external_id IN (a.old_id,a.canonical_id)
     ))
  ) delta;
  SELECT count(*) INTO queue_mismatches
  FROM (
    (SELECT to_jsonb(q) FROM credeals.cre_enrichment_queue q
     WHERE EXISTS (
       SELECT 1 FROM _rollback_aliases a
       WHERE q.external_id IN (a.old_id,a.canonical_id)
     )
     EXCEPT
     SELECT value FROM jsonb_array_elements(
       (SELECT payload->'queue' FROM _rollback_payload)
     ))
    UNION ALL
    (SELECT value FROM jsonb_array_elements(
       (SELECT payload->'queue' FROM _rollback_payload)
     )
     EXCEPT
     SELECT to_jsonb(q) FROM credeals.cre_enrichment_queue q
     WHERE EXISTS (
       SELECT 1 FROM _rollback_aliases a
       WHERE q.external_id IN (a.old_id,a.canonical_id)
     ))
  ) delta;
  IF identity_mismatches<>0 OR dq_mismatches<>0
     OR source_index_mismatches<>0 OR queue_mismatches<>0 THEN
    RAISE EXCEPTION
      'rollback readback failed: identity=%, dq=%, source_index=%, queue=%',
      identity_mismatches,dq_mismatches,source_index_mismatches,queue_mismatches;
  END IF;
  IF rollback_timestamp_rows<>{EXPECTED_LISTINGS} THEN
    RAISE EXCEPTION 'rollback updated_at audit rows: %',
      rollback_timestamp_rows;
  END IF;
END
$post$;

SELECT jsonb_build_object(
  'ok',true,
  'mode','rollback_applied',
  'generation',{sql_lit(EXPECTED_GENERATION)},
  'identityListingsRestored',(SELECT count(*) FROM _pre_identity),
  'dqRowsRestored',(SELECT count(*) FROM _pre_dq),
  'postimageSha256',{expected_postimage_sql},
  'updatedAtDisposition','advanced_by_rollback'
)::text;
COMMIT;
"""


def transaction_body(sql: str) -> str:
    lines = sql.strip().splitlines()
    if not lines or not lines[0].startswith("BEGIN "):
        raise ValueError("expected an explicit transaction")
    body = "\n".join(lines[1:])
    body, marker, trailing = body.rpartition("COMMIT;")
    if not marker or trailing.strip():
        raise ValueError("expected a terminal COMMIT")
    return body.rstrip()


def build_rollback_roundtrip_sql(rows: list[PlanRow], preimage: dict) -> str:
    apply_body = transaction_body(build_apply_sql(rows, preimage))
    rollback_body = transaction_body(
        build_rollback_sql(
            rows,
            preimage,
            INTERNAL_POSTIMAGE_FROM_APPLY,
        )
    )
    return f"""
BEGIN ISOLATION LEVEL SERIALIZABLE;
{apply_body}

-- The forward and reverse paths share staging names. Drop only the forward
-- transaction-local tables before exercising the rollback in the same outer
-- transaction, then discard the entire round trip.
DROP TABLE _nim_queue_old;
DROP TABLE _nim_si_old;
DROP TABLE _nim_aliases;
DROP TABLE _nim_live_reviewed_state;
DROP TABLE _nim_identity;
DROP TABLE _nim_plan;

{rollback_body}
ROLLBACK;
"""


def run_psql(db_url: str, sql: str) -> dict:
    proc = subprocess.run(
        [
            find_psql(),
            *psql_connection_args(db_url),
            "-q",
            "-v",
            "ON_ERROR_STOP=1",
            "-P",
            "pager=off",
            "-P",
            "footer=off",
            "-A",
            "-t",
        ],
        env=psql_connection_env(db_url),
        input=sql,
        text=True,
        capture_output=True,
    )
    if proc.returncode != 0:
        if proc.stderr:
            sys.stderr.write(proc.stderr)
        raise RuntimeError(f"psql exited {proc.returncode}")
    candidates = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
    if not candidates:
        raise RuntimeError("psql returned no JSON result")
    try:
        return json.loads(candidates[-1])
    except json.JSONDecodeError as exc:
        raise RuntimeError("psql did not return the expected JSON result") from exc


def atomic_private_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def load_private_preimage(
    path: Path,
    expected_sha256: str,
) -> tuple[dict, str]:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        raise ValueError("expected preimage SHA-256 must be lowercase hex")
    resolved = path.resolve()
    if resolved.stat().st_mode & 0o077:
        raise ValueError("rollback preimage must not be group- or world-accessible")
    raw = resolved.read_bytes()
    actual_sha256 = hashlib.sha256(raw).hexdigest()
    if actual_sha256 != expected_sha256:
        raise ValueError("rollback preimage SHA-256 does not match")
    payload = json.loads(raw)
    validate_preimage(payload)
    return payload, actual_sha256


def assert_db_target(db_url: str) -> None:
    actual = database_target_fingerprint_from_url(db_url)["value"]
    if actual != EXPECTED_DB_TARGET_SHA256:
        raise ValueError(
            "database target does not match the reviewed Newmark repair target"
        )


@contextmanager
def shared_cre_lock(lock_dir: Path):
    """Acquire the canonical directory lock, migrating our empty legacy file."""
    lock_dir.parent.mkdir(parents=True, exist_ok=True)
    if lock_dir.is_symlink():
        raise ValueError("CRE lock path must not be a symlink")
    if lock_dir.exists() and not lock_dir.is_dir():
        current = lock_dir.stat()
        if not lock_dir.is_file() or lock_dir.name != ".cre.lock" or current.st_size:
            raise ValueError("CRE lock path is not a recognized empty legacy lock")
        with lock_dir.open("a+") as legacy_handle:
            try:
                fcntl.flock(
                    legacy_handle.fileno(),
                    fcntl.LOCK_EX | fcntl.LOCK_NB,
                )
            except BlockingIOError as exc:
                raise RuntimeError("legacy CRE file lock is actively held") from exc
            opened = os.fstat(legacy_handle.fileno())
            current = lock_dir.stat()
            if (opened.st_dev, opened.st_ino) != (current.st_dev, current.st_ino):
                raise RuntimeError("legacy CRE lock changed during migration")
            lock_dir.unlink()
            lock = SharedLock(lock_dir)
            lock.acquire()
            try:
                yield lock
            finally:
                lock.release()
        return
    with SharedLock(lock_dir) as lock:
        yield lock


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, default=DEFAULT_ARTIFACT)
    parser.add_argument("--env-file", required=True)
    parser.add_argument(
        "--preimage",
        type=Path,
        help="owner-only JSON preimage path; required with --apply",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="apply the reviewed repair; default is rollback-only preflight",
    )
    parser.add_argument(
        "--rollback-preimage",
        type=Path,
        help="persistently restore an owner-only preimage produced by --apply",
    )
    parser.add_argument(
        "--expected-preimage-sha256",
        help="exact SHA-256 printed by --apply for the reviewed preimage file",
    )
    parser.add_argument(
        "--expected-postimage-sha256",
        help="exact postimage SHA-256 printed by the matching --apply",
    )
    parser.add_argument(
        "--verify-apply-rollback",
        action="store_true",
        help="execute all mutation SQL and postconditions, then force ROLLBACK",
    )
    parser.add_argument(
        "--verify-rollback-roundtrip",
        action="store_true",
        help="exercise forward repair plus exact preimage restore, then roll back",
    )
    args = parser.parse_args()
    selected_mutations = sum(
        bool(value)
        for value in (
            args.apply,
            args.verify_apply_rollback,
            args.verify_rollback_roundtrip,
            args.rollback_preimage,
        )
    )
    if selected_mutations > 1:
        parser.error(
            "--apply, --verify-apply-rollback, and --rollback-preimage "
            "(including --verify-rollback-roundtrip) are mutually exclusive"
        )
    if args.apply and args.preimage is None:
        parser.error("--apply requires --preimage")
    if args.preimage is not None and not args.apply:
        parser.error("--preimage is valid only with --apply")
    if args.rollback_preimage is not None:
        if args.expected_preimage_sha256 is None:
            parser.error(
                "--rollback-preimage requires --expected-preimage-sha256"
            )
        if args.expected_postimage_sha256 is None:
            parser.error(
                "--rollback-preimage requires --expected-postimage-sha256"
            )
    elif (
        args.expected_preimage_sha256 is not None
        or args.expected_postimage_sha256 is not None
    ):
        parser.error(
            "--expected-preimage-sha256 and --expected-postimage-sha256 "
            "are valid only with --rollback-preimage"
        )

    rows = load_plan(args.artifact.resolve())
    db_url, _ = load_db_url(args.env_file)
    assert_db_target(db_url)

    with shared_cre_lock(DEFAULT_LOCK):
        if args.rollback_preimage is not None:
            rollback_path = args.rollback_preimage.resolve()
            preimage, preimage_sha256 = load_private_preimage(
                rollback_path,
                args.expected_preimage_sha256,
            )
            rolled_back = run_psql(
                db_url,
                build_rollback_sql(
                    rows,
                    preimage,
                    args.expected_postimage_sha256,
                ),
            )
            result = {
                **rolled_back,
                "preimage": str(rollback_path),
                "preimageSha256": preimage_sha256,
            }
            print(json.dumps(result, indent=2, sort_keys=True))
            return 0

        preflight = run_psql(db_url, build_preflight_sql(rows))
        if args.verify_rollback_roundtrip:
            preimage = run_psql(db_url, build_preimage_sql(rows))
            verified = run_psql(
                db_url,
                build_rollback_roundtrip_sql(rows, preimage),
            )
            verified["mode"] = "verify_rollback_roundtrip"
            verified["persisted"] = False
            print(json.dumps(verified, indent=2, sort_keys=True))
            return 0
        if args.verify_apply_rollback:
            preimage = run_psql(db_url, build_preimage_sql(rows))
            sql = build_apply_sql(rows, preimage)
            body, marker, trailing = sql.rpartition("COMMIT;")
            if not marker or trailing.strip():
                raise RuntimeError("could not force the apply transaction to roll back")
            verified = run_psql(db_url, body + "ROLLBACK;\n")
            verified["mode"] = "verify_apply_rollback"
            verified["persisted"] = False
            print(json.dumps(verified, indent=2, sort_keys=True))
            return 0
        if not args.apply:
            print(json.dumps(preflight, indent=2, sort_keys=True))
            return 0

        preimage = run_psql(db_url, build_preimage_sql(rows))
        atomic_private_json(args.preimage.resolve(), preimage)
        applied = run_psql(db_url, build_apply_sql(rows, preimage))
        result = {
            **applied,
            "preimage": str(args.preimage.resolve()),
            "preimageSha256": sha256_file(args.preimage.resolve()),
        }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f"Newmark NIM repair refused: {exc}") from exc
