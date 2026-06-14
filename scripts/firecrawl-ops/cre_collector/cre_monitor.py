#!/usr/bin/env python3
"""cre_monitor.py: post-ingest diff / event / snapshot runner for the CRE
listing-intelligence change-tracking layer (design doc sections 6, 7, 8, 9, 12,
14). OBSERVE-ONLY.

Given a collector artifact (the same JSON cre_ingest reads: a flat
data["listings"] list each carrying sourceKey, plus data["brokers"],
data["sources"], data["runMeta"]) this tool:

  (a) computes the current enumeration (per (brokerage_slug, external_id),
      reusing cre_ingest.to_row so the key EXACTLY equals the ingest external_id),
  (b) diffs it against the persisted credeals.cre_source_index snapshot and the
      credeals.cre_listings state,
  (c) writes an append-only ledger of detected changes to cre_listing_events,
  (d) refreshes cre_source_index,
  (e) updates ONLY the neutral cre_listings columns (source_lastmod,
      canonical_key), and only when the written value actually changes (the
      UPDATE is change-guarded so it never churns cre_listings.updated_at, which
      the EQUIRE views expose). last_seen_at is intentionally NOT written:
      enumeration recency lives in cre_source_index.last_enumerated_at instead,
      so touching last_seen_at every run would needlessly bump updated_at.
  (f) enqueues cre_enrichment_queue work for new and changed listings.

It NEVER writes credeals.cre_listings.status and NEVER sets
credeals.cre_listings.deleted_at. Status activation is a separate Phase-2 path
this tool does not build.

Safety model:
  - --dry-run (the default when no write flag is given) NEVER connects to a DB.
    It assumes empty prior state (baseline seed), exercises the full transform
    plus SQL generation, prints a per-source summary, and exits 0.
  - --apply (alias --live) connects read-only to load prior state, derives the
    real deltas, then pipes ONE transaction (psql -v ON_ERROR_STOP=1) of writes.

Python stdlib only. Credentials are read at runtime via cre_ingest.load_db_url
and are never printed or persisted.

Usage:
  python3 cre_monitor.py --in ./out/run.json                  # dry-run (safe)
  python3 cre_monitor.py --in ./out/run.json --out events.json
  python3 cre_monitor.py --in ./out/run.json --apply          # connect + write
  python3 cre_monitor.py --in a.json --in b.json --apply --force-disappear
"""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections import defaultdict
from datetime import datetime, timezone

# Reuse cre_ingest verbatim so the enumeration key, status normalization, and
# COPY/SQL escaping are identical to the production ingest path. The monitor
# imports these symbols; it never re-derives ids or re-implements status logic.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cre_ingest import (  # noqa: E402
    SOURCE_TO_BROKERAGE,
    STATUS_SOURCE_PATHS,
    _STATUS_BOOL_PATHS,
    _TERMINAL_STATUSES,
    _canonical_key,
    _dig,
    copy_field,
    find_psql,
    group_source_lastmod,
    load_db_url,
    merge_rows,
    norm_status,
    parse_source_lastmod,
    sql_lit,
    to_row,
)

# Coverage gate for disappearance: a source must re-enumerate at least this
# fraction of its prior (non-soft-deleted) index population before any
# disappeared event is allowed, unless --force-disappear is passed. This stops a
# gappy or partial run from inventing false disappearances.
DISAPPEAR_COVERAGE_FRACTION = 0.7


def coverage_decision(*, in_baseline, errored, force_disappear, prior_live,
                      enum_count, fraction=DISAPPEAR_COVERAGE_FRACTION):
    """Pure per-source disappearance-coverage decision (the inventory-protecting
    gate). Returns one of:
      None  -> baseline seed (source has no prior index rows; not evaluated)
      False -> coverage gate failed; disappearance is REFUSED for this source
      True  -> coverage ok; disappearance is permitted

    Precedence (each step short-circuits):
      1. in_baseline                      -> None  (first-ever enumeration)
      2. errored                          -> False (truncated/failed pass; NOT
                                             overridable by force_disappear)
      3. force_disappear or prior_live==0 -> True  (explicit override / nothing to
                                             protect)
      4. enum_count >= fraction*prior_live (did this run re-enumerate enough of the
                                             prior live population to trust a drop?)
    """
    if in_baseline:
        return None
    if errored:
        return False
    if force_disappear or prior_live == 0:
        return True
    return enum_count >= fraction * prior_live


# ---------------------------------------------------------------------------
# Pure transform (no DB; this is the dry-run-testable core)
# ---------------------------------------------------------------------------


# parse_source_lastmod and group_source_lastmod now live in cre_ingest (imported
# above) so the daily ingest upsert and this observe-only monitor derive
# source_lastmod from one shared implementation and can never diverge.


def raw_source_status(listing):
    """The raw native status string norm_status would have read for one FLAT
    listing, or None. Reuses cre_ingest STATUS_SOURCE_PATHS + _dig so the event
    evidence matches the path norm_status actually consulted. A boolean signal
    (closed / underContract) is surfaced as its canonical token. Returns None for
    sources with no native status field (status then came only from a text scan).
    """
    if not isinstance(listing, dict):
        return None
    for path in STATUS_SOURCE_PATHS.get(listing.get("sourceKey"), []):
        raw = _dig(listing, path)
        if raw is None:
            continue
        if isinstance(raw, bool):
            if not raw:
                continue
            return _STATUS_BOOL_PATHS.get(path.split(".")[-1])
        return raw.strip() if isinstance(raw, str) else str(raw)
    return None


def group_status(flat_listings):
    """(norm_status, raw_native_status) for a group, terminal-wins.

    Dual-shape rule (design 12.5): norm_status is called on each ORIGINAL FLAT
    listing (data["listings"] entries), NEVER on a merged {primary,
    secondary_pass} raw_data dict. If any flat listing yields a terminal status
    that wins; otherwise the first non-None status is used. Every canonical
    status is terminal, so in practice this is the first non-None status, with
    its raw native value captured for evidence.
    """
    best = None
    best_raw = None
    for listing in flat_listings:
        status = norm_status(listing)
        if status is None:
            continue
        raw = raw_source_status(listing)
        if status in _TERMINAL_STATUSES:
            return status, raw
        if best is None:
            best, best_raw = status, raw
    return best, best_raw


def _first_non_none(values):
    for v in values:
        if v is not None:
            return v
    return None


def compute_fingerprint(status, sale_price_usd, sale_price_text,
                        lease_rate_min, lease_rate_max, lease_rate_text):
    """The price+status change fingerprint (matches the cre_source_index
    .fingerprint and event content_hash schema comment). sha1 over the joined
    status and price components, truncated to 32 hex chars."""
    parts = [
        status or "",
        str(sale_price_usd or ""),
        sale_price_text or "",
        str(lease_rate_min or ""),
        str(lease_rate_max or ""),
        lease_rate_text or "",
    ]
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:32]


def load_artifact_groups(paths):
    """Read one or more collector artifacts and group every usable listing by
    (slug, external_id), reusing to_row so the key equals the ingest external_id.

    Returns (groups, run_started_at, per_source_flat, skipped_no_url,
             errored_source_keys):
      groups: dict (slug, external_id) -> {slug, external_id, source_key, url,
              merged_row, flat_listings}
      per_source_flat: source_key -> count of to_row-successful flat listings
      errored_source_keys: set of source_key whose enumeration reported an error
              OR a truncated/partial pass (sources[].error set, or sources[].truncated
              true) on ANY transaction pass this run. Disappearance must be refused
              for these (a partial pass is not a safe disappearance signal), even if
              the surviving rows clear the coverage fraction.
    """
    groups = {}
    per_source_flat = defaultdict(int)
    skipped_no_url = 0
    run_started_at = None
    errored_source_keys = set()

    for path in paths:
        with open(path) as f:
            data = json.load(f)
        run_meta = data.get("runMeta") or {}
        run_started_at = run_started_at or run_meta.get("startedAt")
        scraped_at = run_meta.get("finishedAt") or datetime.now(timezone.utc).isoformat()
        for src in data.get("sources") or []:
            if src.get("error") or src.get("truncated"):
                sk = src.get("sourceKey")
                if sk:
                    errored_source_keys.add(sk)
        brokers_by_idx = {i: b for i, b in enumerate(data.get("brokers") or [])}
        for listing in data.get("listings") or []:
            row = to_row(listing, brokers_by_idx, scraped_at)
            if row is None:
                skipped_no_url += 1
                continue
            source_key = listing.get("sourceKey")
            per_source_flat[source_key] += 1
            key = (row["slug"], row["external_id"])
            existing = groups.get(key)
            if existing is None:
                groups[key] = {
                    "slug": row["slug"],
                    "external_id": row["external_id"],
                    "source_key": source_key,
                    "url": row["source_url"],
                    "merged_row": row,
                    "flat_listings": [listing],
                }
            else:
                existing["merged_row"] = merge_rows(existing["merged_row"], row)
                existing["flat_listings"].append(listing)

    return groups, run_started_at, dict(per_source_flat), skipped_no_url, errored_source_keys


def finalize_group(g):
    """Collapse one group to the derived record the monitor diffs and writes."""
    merged = g["merged_row"]
    flat = g["flat_listings"]
    status, raw_status = group_status(flat)
    sale_price_usd = merged.get("sale_price_usd")
    lease_rate_min = merged.get("lease_rate_min")
    lease_rate_max = merged.get("lease_rate_max")
    sale_price_text = _first_non_none(listing.get("salePriceText") for listing in flat)
    lease_rate_text = _first_non_none(listing.get("leaseRateText") for listing in flat)
    canonical = _first_non_none(_canonical_key(listing) for listing in flat)
    return {
        "slug": g["slug"],
        "external_id": g["external_id"],
        "source_key": g["source_key"],
        "url": g["url"],
        "norm_status": status,
        "raw_status": raw_status,
        "sale_price_usd": sale_price_usd,
        "lease_rate_min": lease_rate_min,
        "lease_rate_max": lease_rate_max,
        "sale_price_text": sale_price_text,
        "lease_rate_text": lease_rate_text,
        "source_lastmod": group_source_lastmod(flat),
        "canonical_key": canonical,
        "fingerprint": compute_fingerprint(
            status, sale_price_usd, sale_price_text,
            lease_rate_min, lease_rate_max, lease_rate_text,
        ),
    }


# ---------------------------------------------------------------------------
# Event derivation (pure; operates on resolved brokerage-keyed records)
# ---------------------------------------------------------------------------


def _price_field_and_value(g):
    """(field, new_value) for a price_change event. Prefers a sale signal, then
    falls back to a lease signal. Returns the raw text when no numeric value
    parsed (so 'Negotiable' -> 'Call for offers' is still a recordable move)."""
    if g["sale_price_usd"] is not None:
        val = g["sale_price_usd"]
        if isinstance(val, float) and val.is_integer():
            return "sale_price_usd", str(int(val))
        return "sale_price_usd", str(val)
    if g["sale_price_text"]:
        return "sale_price_usd", g["sale_price_text"]
    if g["lease_rate_min"] is not None:
        if g["lease_rate_max"] is not None:
            return "lease_rate", f"{g['lease_rate_min']}-{g['lease_rate_max']}"
        return "lease_rate", str(g["lease_rate_min"])
    return "lease_rate", g["lease_rate_text"]


def _event(listing_id, brokerage_id, source_key, event_type, **kw):
    e = {
        "listing_id": listing_id,
        "brokerage_id": brokerage_id,
        "source_key": source_key,
        "event_type": event_type,
        "field": None,
        "old_value": None,
        "new_value": None,
        "source_value": None,
        "source_status_value": None,
        "sale_price_text": None,
        "lease_rate_text": None,
        "source_url": None,
        "content_hash": None,
    }
    e.update(kw)
    return e


def derive_events(current_records, prior_index, prior_listings, soft_deleted_canon,
                  run_source_keys, baseline_source_keys, coverage_ok_by_source, run_uuid):
    """Pure diff producing the event ledger plus enqueue and soft-delete-mark
    work. OBSERVE-ONLY: never touches cre_listings.status or deleted_at.

    current_records: dict (brokerage_id, external_id) -> finalized group.
    prior_index:     dict (brokerage_id, external_id) -> {fingerprint,
                     soft_deleted, observed_status, source_key, url}.
    prior_listings:  dict (brokerage_id, external_id) -> {id, status, deleted}.
    soft_deleted_canon: dict (brokerage_id, canonical_key) -> [listing_id, ...].
    """
    events = []
    enqueue_new = {}      # (bid, source_key, eid) -> url
    enqueue_changed = {}  # (bid, source_key, eid) -> url
    disappear_marks = []  # (bid, eid) to flag soft_deleted in cre_source_index
    counts = defaultdict(lambda: defaultdict(int))

    # NEW / REAPPEARED / STATUS / PRICE / POSSIBLE_RELIST over the enumeration.
    for (bid, eid), g in current_records.items():
        sk = g["source_key"]
        if sk in baseline_source_keys:
            continue  # silent baseline seed: no events for a first-ever source
        prior = prior_index.get((bid, eid))
        listing = prior_listings.get((bid, eid))

        if prior is None:
            # NEW: not previously enumerated.
            if listing is None:
                # No cre_listings row to satisfy the events FK. Skip the event;
                # cre_source_index is still upserted from the enumeration.
                counts[sk]["enumerated_unmatched"] += 1
                continue
            events.append(_event(
                listing["id"], bid, sk, "new",
                source_status_value=g["raw_status"],
                sale_price_text=g["sale_price_text"],
                lease_rate_text=g["lease_rate_text"],
                source_url=g["url"], content_hash=g["fingerprint"],
            ))
            counts[sk]["new"] += 1
            enqueue_new[(bid, sk, eid)] = g["url"]
            ck = g["canonical_key"]
            if ck:
                matches = [lid for lid in soft_deleted_canon.get((bid, ck), [])
                           if lid != listing["id"]]
                if matches:
                    events.append(_event(
                        listing["id"], bid, sk, "possible_relist",
                        field="canonical_key", new_value=ck,
                        source_value=matches[0],
                        source_url=g["url"], content_hash=g["fingerprint"],
                    ))
                    counts[sk]["possible_relist"] += 1
            continue

        # EXISTING: present in the prior index.
        if listing is None:
            continue  # cannot satisfy the listing FK for any event

        # REAPPEARED: the monitor's OWN enumeration snapshot saw it gone and it is
        # enumerated again. Gate strictly on cre_source_index.soft_deleted (which
        # step 3 flips back to false this run) so the event fires exactly once on
        # the gone->present transition. We do NOT OR in cre_listings.deleted_at:
        # the observe-only monitor never clears deleted_at (only the full ingest
        # resurrects a row), so ORing it would re-emit 'reappeared' on every run for
        # a row that stays soft-deleted in cre_listings while being re-enumerated,
        # flooding the append-only ledger and v_cre_recent_changes.
        if prior["soft_deleted"]:
            events.append(_event(
                listing["id"], bid, sk, "reappeared",
                source_status_value=g["raw_status"],
                source_url=g["url"], content_hash=g["fingerprint"],
            ))
            counts[sk]["reappeared"] += 1

        cur_status = g["norm_status"]
        prior_obs = prior["observed_status"]
        status_moved = (prior_obs or "") != (cur_status or "")

        # STATUS_CHANGE: observed status moved vs the prior snapshot AND it
        # differs from the live cre_listings.status. Both conditions are needed:
        # the cre_listings comparison gives EQUIRE a meaningful old_value, and
        # the prior-snapshot comparison gives cross-run idempotency (the monitor
        # never updates cre_listings.status, so without it an unchanged terminal
        # status would re-fire every run). EVENT ONLY.
        if cur_status is not None and cur_status != listing["status"] and status_moved:
            events.append(_event(
                listing["id"], bid, sk, "status_change",
                field="status", old_value=listing["status"], new_value=cur_status,
                source_status_value=g["raw_status"],
                source_url=g["url"], content_hash=g["fingerprint"],
            ))
            counts[sk]["status_change"] += 1
            enqueue_changed[(bid, sk, eid)] = g["url"]

        # PRICE_CHANGE: the combined fingerprint advanced while the status
        # component held constant, so the move is in price. cre_source_index
        # persists only the combined fingerprint (no prior price column), so the
        # prior price value cannot be recovered: old_value is null and the prior
        # fingerprint is carried in source_value as evidence.
        if (prior["fingerprint"] and prior["fingerprint"] != g["fingerprint"]
                and not status_moved):
            field, new_value = _price_field_and_value(g)
            events.append(_event(
                listing["id"], bid, sk, "price_change",
                field=field, old_value=None, new_value=new_value,
                source_value=prior["fingerprint"],
                sale_price_text=g["sale_price_text"],
                lease_rate_text=g["lease_rate_text"],
                source_url=g["url"], content_hash=g["fingerprint"],
            ))
            counts[sk]["price_change"] += 1
            enqueue_changed[(bid, sk, eid)] = g["url"]

    # DISAPPEARED over the prior index, scoped to sources present in this run and
    # gated by the per-source coverage check.
    current_keys = set(current_records.keys())
    for (bid, eid), prior in prior_index.items():
        sk = prior["source_key"] or ""
        if sk not in run_source_keys or sk in baseline_source_keys:
            continue
        if prior["soft_deleted"]:
            continue  # already recorded gone
        if (bid, eid) in current_keys:
            continue  # still enumerated
        if not coverage_ok_by_source.get(sk, False):
            continue  # coverage gate failed for this source; skip (logged)
        listing = prior_listings.get((bid, eid))
        if listing is None:
            continue  # no FK target
        events.append(_event(
            listing["id"], bid, sk, "disappeared",
            source_value="enumeration_gone",
            source_status_value=prior["observed_status"] or None,
            source_url=prior["url"] or None, content_hash=prior["fingerprint"] or None,
        ))
        counts[sk]["disappeared"] += 1
        disappear_marks.append((bid, eid))

    events = _dedupe_events(events, run_uuid)
    return events, enqueue_new, enqueue_changed, disappear_marks, counts


def _dedupe_events(events, run_uuid):
    """Collapse events that share the cre_listing_events idempotency key within
    this run: (listing_id, event_type, COALESCE(field,''), COALESCE(new_value,''),
    scrape_job_id). scrape_job_id is constant per run, so dedupe on the rest. This
    keeps the multi-row INSERT ... ON CONFLICT DO NOTHING free of self-conflicts.
    """
    seen = set()
    out = []
    for e in events:
        key = (e["listing_id"], e["event_type"], e["field"] or "", e["new_value"] or "")
        if key in seen:
            continue
        seen.add(key)
        out.append(e)
    return out


# ---------------------------------------------------------------------------
# SQL generation (single transaction, piped via psql exactly like cre_ingest)
# ---------------------------------------------------------------------------

_ENUM_COLS = [
    "slug", "external_id", "source_key", "url", "source_lastmod",
    "fingerprint", "observed_status", "canonical_key",
]


def _sql_text(v):
    return "NULL" if v is None else sql_lit(v)


def _sql_uuid(v):
    return "NULL" if v is None else sql_lit(v) + "::uuid"


def build_write_sql(finalized, events, enqueue_new, enqueue_changed,
                    disappear_marks, run_uuid, started_at, notes, slugs):
    """Generate the full observe-only write transaction.

    Order matters for the FKs: the per-run cre_scrape_jobs row is inserted FIRST
    (it is the cre_listing_events.scrape_job_id target and the run scope for the
    within-run idempotency index). Events reference already-existing cre_listings
    rows. Only neutral cre_listings columns are ever written.
    """
    lines = []
    w = lines.append
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("SET LOCAL statement_timeout = '600s';")
    # Pin standard_conforming_strings before any literal-bearing INSERT below:
    # scraped event free-text (sale_price_text, source_url, new_value, ...) is
    # inlined via _sql_text -> sql_lit (quote-doubling), which is injection-safe
    # only when this GUC is ON. Self-enforce it rather than trust the default.
    w("SET LOCAL standard_conforming_strings = on;")

    # (1) per-run monitor job row FIRST (events.scrape_job_id FK target).
    if len(slugs) == 1:
        brokerage_expr = (
            "(SELECT id FROM credeals.cre_brokerages WHERE slug = "
            + sql_lit(slugs[0]) + ")"
        )
    else:
        brokerage_expr = "NULL"
    w("")
    w("-- (1) per-run monitor job row, inserted FIRST so cre_listing_events.scrape_job_id resolves.")
    w("INSERT INTO credeals.cre_scrape_jobs")
    w("    (id, brokerage_id, status, started_at, completed_at,")
    w("     listings_discovered, listings_scraped, listings_saved, errors_count, notes)")
    w("VALUES (")
    w(f"    {sql_lit(run_uuid)}::uuid,")
    w(f"    {brokerage_expr},")
    w("    'completed',")
    w(f"    {sql_lit(started_at)}::timestamptz,")
    w("    now(),")
    w(f"    {len(finalized)}, 0, 0, 0,")
    w(f"    {sql_lit(notes)}")
    w(");")

    # Current enumeration staged by slug; brokerage resolved by join (so the
    # generated SQL is identical in dry-run and apply mode).
    w("")
    w("CREATE TEMP TABLE _enum (")
    w("    slug text, external_id text, source_key text, url text,")
    w("    source_lastmod timestamptz, fingerprint text, observed_status text,")
    w("    canonical_key text")
    w(") ON COMMIT DROP;")
    w(f"COPY _enum ({', '.join(_ENUM_COLS)}) FROM stdin;")
    for g in finalized:
        record = {
            "slug": g["slug"],
            "external_id": g["external_id"],
            "source_key": g["source_key"],
            "url": g["url"],
            "source_lastmod": g["source_lastmod"],
            "fingerprint": g["fingerprint"],
            "observed_status": g["norm_status"],
            "canonical_key": g["canonical_key"],
        }
        w("\t".join(copy_field(record[c]) for c in _ENUM_COLS))
    w("\\.")

    w("")
    w("-- Fail loudly if a slug is not seeded in cre_brokerages (mirrors cre_ingest).")
    w("DO $$")
    w("DECLARE missing text;")
    w("BEGIN")
    w("    SELECT string_agg(DISTINCT s.slug, ', ') INTO missing")
    w("    FROM _enum s LEFT JOIN credeals.cre_brokerages b ON b.slug = s.slug")
    w("    WHERE b.id IS NULL;")
    w("    IF missing IS NOT NULL THEN")
    w("        RAISE EXCEPTION 'unseeded brokerage slug(s): % (run sql/001_cre_brokerages.sql)', missing;")
    w("    END IF;")
    w("END $$;")

    w("")
    w("CREATE TEMP TABLE _enum_b ON COMMIT DROP AS")
    w("SELECT b.id AS brokerage_id, e.*")
    w("FROM _enum e JOIN credeals.cre_brokerages b ON b.slug = e.slug;")

    # (2) append-only events (idempotent within the run).
    if events:
        w("")
        w("-- (2) append-only change ledger. ON CONFLICT keys the within-run idempotency index.")
        w("INSERT INTO credeals.cre_listing_events")
        w("    (listing_id, brokerage_id, scrape_job_id, event_type, field, old_value,")
        w("     new_value, source_value, source_status_value, sale_price_text,")
        w("     lease_rate_text, source_url, content_hash)")
        w("VALUES")
        value_rows = []
        for e in events:
            value_rows.append(
                "    ("
                + ", ".join([
                    _sql_uuid(e["listing_id"]),
                    _sql_uuid(e["brokerage_id"]),
                    _sql_uuid(run_uuid),
                    _sql_text(e["event_type"]),
                    _sql_text(e["field"]),
                    _sql_text(e["old_value"]),
                    _sql_text(e["new_value"]),
                    _sql_text(e["source_value"]),
                    _sql_text(e["source_status_value"]),
                    _sql_text(e["sale_price_text"]),
                    _sql_text(e["lease_rate_text"]),
                    _sql_text(e["source_url"]),
                    _sql_text(e["content_hash"]),
                ])
                + ")"
            )
        w(",\n".join(value_rows))
        w("ON CONFLICT (listing_id, event_type, COALESCE(field, ''), COALESCE(new_value, ''), scrape_job_id) DO NOTHING;")

    # (3) refresh cre_source_index from the enumeration (present rows).
    w("")
    w("-- (3) refresh the enumeration snapshot. first_seen is preserved on conflict.")
    w("INSERT INTO credeals.cre_source_index AS si")
    w("    (brokerage_id, external_id, source_key, url, source_lastmod, fingerprint,")
    w("     observed_status, soft_deleted, first_seen, last_seen, last_enumerated_at)")
    w("SELECT brokerage_id, external_id, source_key, url, source_lastmod, fingerprint,")
    w("       observed_status, false, now(), now(), now()")
    w("FROM _enum_b")
    w("ON CONFLICT (brokerage_id, external_id) DO UPDATE SET")
    w("    last_seen          = now(),")
    w("    last_enumerated_at = now(),")
    w("    fingerprint        = EXCLUDED.fingerprint,")
    w("    observed_status    = EXCLUDED.observed_status,")
    w("    source_lastmod     = COALESCE(EXCLUDED.source_lastmod, si.source_lastmod),")
    w("    url                = EXCLUDED.url,")
    w("    source_key         = COALESCE(EXCLUDED.source_key, si.source_key),")
    w("    soft_deleted       = false;")

    # (3b) mark coverage-gated disappeared ids gone in the MONITOR index only.
    if disappear_marks:
        w("")
        w("-- (3b) record disappearance in cre_source_index (monitor table only; never cre_listings).")
        mark_rows = ", ".join(
            f"({_sql_text(bid)}, {_sql_text(eid)})" for bid, eid in disappear_marks
        )
        w("UPDATE credeals.cre_source_index si")
        w("SET soft_deleted = true")
        w(f"FROM (VALUES {mark_rows}) AS d(brokerage_id, external_id)")
        w("WHERE si.brokerage_id = d.brokerage_id::uuid AND si.external_id = d.external_id;")

    # (4) neutral cre_listings columns ONLY (never status, never deleted_at).
    # EQUIRE-neutrality: cre_listings has a BEFORE UPDATE trigger that bumps
    # updated_at (a column the EQUIRE views surface) on ANY row update. So this
    # UPDATE is guarded to touch a row ONLY when source_lastmod or canonical_key
    # actually changes; an unchanged enumerated row is skipped, so updated_at does
    # not churn on every monitor pass. When it does fire, source_lastmod advanced
    # (the source actually updated the listing), so bumping updated_at is correct.
    # last_seen_at is intentionally NOT written here: enumeration freshness lives in
    # cre_source_index.last_seen (refreshed every run in step 3); writing
    # last_seen_at = now() per row would defeat the no-churn guard above.
    w("")
    w("-- (4) NEUTRAL cre_listings columns only. Never status. Never deleted_at.")
    w("--     Guarded so updated_at (trigger-bumped, EQUIRE-visible) moves only on real change.")
    w("UPDATE credeals.cre_listings l")
    w("SET source_lastmod = COALESCE(e.source_lastmod, l.source_lastmod),")
    w("    canonical_key  = COALESCE(e.canonical_key, l.canonical_key)")
    w("FROM _enum_b e")
    w("WHERE l.brokerage_id = e.brokerage_id AND l.external_id = e.external_id")
    w("  AND (l.source_lastmod IS DISTINCT FROM COALESCE(e.source_lastmod, l.source_lastmod)")
    w("       OR l.canonical_key  IS DISTINCT FROM COALESCE(e.canonical_key, l.canonical_key));")

    # (5) enrichment queue for new + changed listings.
    queue_rows = []
    for (bid, sk, eid), url in sorted(enqueue_new.items()):
        queue_rows.append((bid, sk, eid, url, "new"))
    changed_only = {k: v for k, v in enqueue_changed.items() if k not in enqueue_new}
    for (bid, sk, eid), url in sorted(changed_only.items()):
        queue_rows.append((bid, sk, eid, url, "changed"))
    if queue_rows:
        w("")
        w("-- (5) durable enrichment queue for new + changed listings.")
        w("INSERT INTO credeals.cre_enrichment_queue")
        w("    (brokerage_id, source_key, external_id, url, reason)")
        w("VALUES")
        rows = []
        for bid, sk, eid, url, reason in queue_rows:
            rows.append(
                "    ("
                + ", ".join([
                    _sql_uuid(bid), _sql_text(sk), _sql_text(eid),
                    _sql_text(url), _sql_text(reason),
                ])
                + ")"
            )
        w(",\n".join(rows))
        w("ON CONFLICT (brokerage_id, external_id, reason) DO NOTHING;")

    w("")
    w("COMMIT;")
    return "\n".join(lines)


def sql_sample(sql, max_data_rows=3):
    """Trim COPY data blocks so a sample of the generated SQL stays readable."""
    out = []
    in_copy = False
    data_count = 0
    for line in sql.split("\n"):
        if in_copy:
            if line == "\\.":
                if data_count > max_data_rows:
                    out.append(f"... ({data_count - max_data_rows} more COPY data rows elided) ...")
                out.append(line)
                in_copy = False
                data_count = 0
                continue
            data_count += 1
            if data_count <= max_data_rows:
                out.append(line)
            continue
        out.append(line)
        if line.startswith("COPY ") and line.rstrip().endswith("FROM stdin;"):
            in_copy = True
            data_count = 0
    return "\n".join(out)


# ---------------------------------------------------------------------------
# Read-only state load (apply mode only)
# ---------------------------------------------------------------------------


def _psql_read(db_url, sql):
    """Run one read-only query and return a list of tuples. Uses -tA -F$'\\t'
    so NULL renders as an empty field. Never prints the DB URL."""
    psql = find_psql()
    proc = subprocess.run(
        [psql, db_url, "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-c", sql],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"psql read failed ({proc.returncode}): {proc.stderr.strip()}")
    rows = []
    for line in proc.stdout.splitlines():
        if line == "":
            continue
        rows.append(tuple(line.split("\t")))
    return rows


def _in_list(values):
    if not values:
        return "(NULL)"
    return "(" + ", ".join(sql_lit(v) for v in sorted(values)) + ")"


def load_prior_state(db_url, slugs):
    """Load brokerage ids, the prior enumeration snapshot, the listing state, and
    the soft-deleted canonical_key index for the touched brokerages. Read-only.
    """
    brokerage_rows = _psql_read(
        db_url,
        f"SELECT slug, id FROM credeals.cre_brokerages WHERE slug IN {_in_list(slugs)};",
    )
    brokerage_by_slug = {slug: bid for slug, bid in brokerage_rows}
    brokerage_ids = list(brokerage_by_slug.values())

    prior_index = {}
    prior_listings = {}
    soft_deleted_canon = defaultdict(list)
    if brokerage_ids:
        for row in _psql_read(db_url, (
            "SELECT brokerage_id, external_id, COALESCE(fingerprint, ''), soft_deleted, "
            "COALESCE(observed_status, ''), COALESCE(source_key, ''), COALESCE(url, '') "
            f"FROM credeals.cre_source_index WHERE brokerage_id IN {_in_list(brokerage_ids)};"
        )):
            bid, eid, fp, soft, obs, sk, url = row
            prior_index[(bid, eid)] = {
                "fingerprint": fp or None,
                "soft_deleted": soft == "t",
                "observed_status": obs or None,
                "source_key": sk or None,
                "url": url or None,
            }
        for row in _psql_read(db_url, (
            "SELECT id, brokerage_id, external_id, COALESCE(status, ''), (deleted_at IS NOT NULL) "
            f"FROM credeals.cre_listings WHERE brokerage_id IN {_in_list(brokerage_ids)};"
        )):
            lid, bid, eid, status, deleted = row
            prior_listings[(bid, eid)] = {
                "id": lid,
                "status": status or None,
                "deleted": deleted == "t",
            }
        for row in _psql_read(db_url, (
            "SELECT brokerage_id, canonical_key, id FROM credeals.cre_listings "
            "WHERE deleted_at IS NOT NULL AND canonical_key IS NOT NULL "
            f"AND brokerage_id IN {_in_list(brokerage_ids)};"
        )):
            bid, ck, lid = row
            soft_deleted_canon[(bid, ck)].append(lid)

    return brokerage_by_slug, prior_index, prior_listings, dict(soft_deleted_canon)


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def build_summary(finalized, per_source_flat, baseline_source_keys,
                  coverage_ok_by_source, event_counts, skipped_no_url):
    """Per-source counts: enumerated (flat), grouped, by norm_status,
    canonical_key coverage, baseline flag, coverage flag, events-by-type."""
    grouped = defaultdict(int)
    status_hist = defaultdict(lambda: defaultdict(int))
    canonical_present = defaultdict(int)
    for g in finalized:
        sk = g["source_key"]
        grouped[sk] += 1
        status_hist[sk][g["norm_status"] or "none"] += 1
        if g["canonical_key"]:
            canonical_present[sk] += 1

    by_source = {}
    for sk in sorted(set(list(grouped) + list(per_source_flat))):
        n_groups = grouped.get(sk, 0)
        by_source[sk] = {
            "enumerated_flat": per_source_flat.get(sk, 0),
            "grouped": n_groups,
            "baseline_seed": sk in baseline_source_keys,
            "coverage_ok": coverage_ok_by_source.get(sk),
            "by_norm_status": dict(status_hist.get(sk, {})),
            "canonical_key_present": canonical_present.get(sk, 0),
            "canonical_key_coverage": (
                round(canonical_present.get(sk, 0) / n_groups, 4) if n_groups else 0.0
            ),
            "events": dict(event_counts.get(sk, {})),
        }

    events_by_type = defaultdict(int)
    for sk_counts in event_counts.values():
        for etype, n in sk_counts.items():
            events_by_type[etype] += n

    return {
        "totals": {
            "enumerated_flat": sum(per_source_flat.values()),
            "grouped": len(finalized),
            "skipped_no_url": skipped_no_url,
        },
        "by_source": by_source,
        "events_by_type": dict(events_by_type),
    }


def print_summary(summary, mode, quiet):
    if quiet:
        return
    t = summary["totals"]
    print(f"mode: {mode}", file=sys.stderr)
    print(
        f"totals: enumerated_flat={t['enumerated_flat']} grouped={t['grouped']} "
        f"skipped_no_url={t['skipped_no_url']}",
        file=sys.stderr,
    )
    for sk, s in summary["by_source"].items():
        label = "BASELINE SEED" if s["baseline_seed"] else "diff"
        cov = s["coverage_ok"]
        cov_str = "" if cov is None else f" coverage_ok={cov}"
        status_str = " ".join(f"{k}={v}" for k, v in sorted(s["by_norm_status"].items()))
        events_str = " ".join(f"{k}={v}" for k, v in sorted(s["events"].items())) or "none"
        print(
            f"  [{sk}] {label}{cov_str} | enumerated={s['enumerated_flat']} "
            f"grouped={s['grouped']} | status: {status_str} | "
            f"canonical_key: {s['canonical_key_present']}/{s['grouped']} "
            f"({s['canonical_key_coverage'] * 100:.1f}%) | "
            f"events(would-write): {events_str}",
            file=sys.stderr,
        )
    print(
        "events_by_type(total): "
        + (" ".join(f"{k}={v}" for k, v in sorted(summary["events_by_type"].items())) or "none"),
        file=sys.stderr,
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--in", dest="inputs", action="append", required=True,
                    help="collector output JSON (repeatable)")
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument("--dry-run", action="store_true",
                    help="never connect; assume empty prior state (baseline seed). Default when no write flag is given.")
    ap.add_argument("--apply", "--live", dest="apply", action="store_true",
                    help="connect read-only, derive real deltas, and write one transaction")
    ap.add_argument("--force-disappear", action="store_true",
                    help="bypass the per-source coverage gate for disappeared events")
    ap.add_argument("--out", default=None, help="write the event/summary JSON here")
    ap.add_argument("--quiet", action="store_true", help="suppress the per-source summary")
    args = ap.parse_args()

    # Safe default: only connect/write when --apply is explicit and --dry-run is not.
    apply_mode = bool(args.apply) and not args.dry_run
    mode = "apply" if apply_mode else "dry-run"

    groups, run_started_at, per_source_flat, skipped_no_url, errored_source_keys = load_artifact_groups(args.inputs)
    if not groups:
        sys.exit("nothing to monitor (0 usable listings)")
    finalized_by_key = {key: finalize_group(g) for key, g in groups.items()}
    finalized = sorted(
        finalized_by_key.values(), key=lambda g: (g["slug"], g["external_id"])
    )
    run_source_keys = {g["source_key"] for g in finalized}
    slugs = sorted({g["slug"] for g in finalized})
    started_at = run_started_at or datetime.now(timezone.utc).isoformat()
    run_uuid = str(uuid.uuid4())
    notes = ("monitor observe-only " + ", ".join(os.path.basename(p) for p in args.inputs))[:480]

    events = []
    enqueue_new = {}
    enqueue_changed = {}
    disappear_marks = []
    event_counts = {}
    baseline_source_keys = set(run_source_keys)  # dry-run: empty prior -> all seed
    coverage_ok_by_source = {}

    db_url = None
    if apply_mode:
        db_url, env_path = load_db_url(args.env_file)
        print(f"credentials: {env_path}", file=sys.stderr)
        brokerage_by_slug, prior_index, prior_listings, soft_deleted_canon = load_prior_state(db_url, slugs)

        # Re-key the current enumeration by (brokerage_id, external_id) for diffing.
        current_records = {}
        for g in finalized:
            bid = brokerage_by_slug.get(g["slug"])
            if bid is None:
                continue  # unseeded slug; the write transaction guard will raise
            current_records[(bid, g["external_id"])] = g

        # Baseline-seed is decided per source_key: a source with zero prior index
        # rows is a first-ever enumeration and is seeded silently (no events).
        prior_count_by_source = defaultdict(int)
        prior_live_count_by_source = defaultdict(int)
        for prior in prior_index.values():
            sk = prior["source_key"] or ""
            prior_count_by_source[sk] += 1
            if not prior["soft_deleted"]:
                prior_live_count_by_source[sk] += 1
        baseline_source_keys = {
            sk for sk in run_source_keys if prior_count_by_source.get(sk, 0) == 0
        }

        # Per-source coverage gate for disappearance.
        enum_count_by_source = defaultdict(int)
        for g in finalized:
            enum_count_by_source[g["source_key"]] += 1
        for sk in run_source_keys:
            prior_live = prior_live_count_by_source.get(sk, 0)
            enum_count = enum_count_by_source.get(sk, 0)
            errored = sk in errored_source_keys
            decision = coverage_decision(
                in_baseline=sk in baseline_source_keys,
                errored=errored,
                force_disappear=args.force_disappear,
                prior_live=prior_live,
                enum_count=enum_count,
            )
            coverage_ok_by_source[sk] = decision
            if decision is False and not args.quiet:
                if errored:
                    # Not overridable by --force-disappear: forcing disappearance on
                    # a known-truncated pass is the exact mass soft-delete hazard the
                    # coverage gate exists to prevent.
                    print(
                        f"WARNING: coverage gate failed for source '{sk}': the "
                        "enumeration reported an error this run (truncated/failed "
                        "pass); skipping disappeared events for this source.",
                        file=sys.stderr,
                    )
                else:
                    print(
                        f"WARNING: coverage gate failed for source '{sk}': enumerated "
                        f"{enum_count} < {DISAPPEAR_COVERAGE_FRACTION} * "
                        f"prior {prior_live}; skipping disappeared events for this source "
                        "(use --force-disappear to override).",
                        file=sys.stderr,
                    )

        for sk in sorted(baseline_source_keys):
            if not args.quiet:
                print(f"baseline seed: source '{sk}' has no prior index rows; seeding silently (no events).",
                      file=sys.stderr)

        events, enqueue_new, enqueue_changed, disappear_marks, event_counts = derive_events(
            current_records, prior_index, prior_listings, soft_deleted_canon,
            run_source_keys, baseline_source_keys, coverage_ok_by_source, run_uuid,
        )
    else:
        if not args.quiet:
            print("dry-run: no DB connection; assuming empty prior state (baseline seed for every source).",
                  file=sys.stderr)
        coverage_ok_by_source = {sk: None for sk in run_source_keys}

    sql = build_write_sql(
        finalized, events, enqueue_new, enqueue_changed,
        disappear_marks, run_uuid, started_at, notes, slugs,
    )

    summary = build_summary(
        finalized, per_source_flat, baseline_source_keys,
        coverage_ok_by_source, event_counts, skipped_no_url,
    )
    print_summary(summary, mode, args.quiet)

    if args.out:
        out_doc = {
            "mode": mode,
            "run_id": run_uuid,
            "artifacts": [os.path.basename(p) for p in args.inputs],
            "started_at": started_at,
            "baseline_source_keys": sorted(baseline_source_keys),
            "summary": summary,
            "enqueue": {"new": len(enqueue_new),
                        "changed": len({k for k in enqueue_changed if k not in enqueue_new})},
            "disappear_marks": len(disappear_marks),
            "events": events if apply_mode else [],
        }
        with open(args.out, "w") as f:
            json.dump(out_doc, f, indent=2, default=str)
        if not args.quiet:
            print(f"wrote summary/events JSON: {args.out}", file=sys.stderr)

    if not apply_mode:
        if not args.quiet:
            print("\n----- generated SQL (sample, COPY data elided) -----", file=sys.stderr)
            print(sql_sample(sql), file=sys.stderr)
        print("dry-run: not connecting", file=sys.stderr)
        return

    # Apply: pipe the single transaction through psql, exactly like cre_ingest.
    out_dir = tempfile.mkdtemp(prefix="cre_monitor_")
    sql_path = os.path.join(out_dir, "monitor.sql")
    with open(sql_path, "w") as f:
        f.write(sql)
    psql = find_psql()
    proc = subprocess.run(
        [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_path],
        stdout=sys.stdout, stderr=sys.stderr,
    )
    if proc.returncode != 0:
        sys.exit(f"psql exited {proc.returncode}")
    print(
        f"monitor complete: {len(events)} events, "
        f"{len(enqueue_new)} new + {len({k for k in enqueue_changed if k not in enqueue_new})} changed enqueued, "
        f"{len(disappear_marks)} disappeared marks",
        file=sys.stderr,
    )


if __name__ == "__main__":
    main()
