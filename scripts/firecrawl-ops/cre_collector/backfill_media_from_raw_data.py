#!/usr/bin/env python3
"""
backfill_media_from_raw_data.py: recover already-stranded media/docs from
existing cre_listings.raw_data into the child tables, additively.

WHY THIS EXISTS
---------------
collect.ts captured source-native media and gated-document fields into
raw_data long before lib/harvest.ts existed, but cre_ingest.py only ever
emitted contacts / brochures (documents) / photos (images). Three well-known
shapes therefore sit "stranded" in raw_data, never projected into a child
table any consumer reads:

  * JLL detail videos / virtual tours / 360 URLs   (~328 listings)
      raw_data.jllDetail.videos[]        (string or {url,...})
      raw_data.jllDetail.virtualTours[]
      raw_data.jllDetail.view360URLs[]
    -> cre_listing_media   (mediaType video / virtual_tour, provider-detected)

  * Marcus & Millichap gated deal-room documents   (~3124 listings)
      raw_data.gatedDocuments[]          ({name, url, gated:true})
    -> cre_listing_documents (docType om / brochure / ... classified)

  * Colliers SalesTracker brochure / agreement links (~814 listings)
      raw_data.colliersSalesTrackerDetail.brochureUrl   (string url)
      raw_data.colliersSalesTrackerDetail.agreementUrl  (string url)
    -> cre_listing_documents (brochure -> 'brochure', agreement -> 'other')

This is a one-time-ish, re-runnable recovery pass. It reads ONLY existing
raw_data (no network, no scrape) and inserts the stranded items into:

  cre_listing_media       (video / virtual-tour / matterport / 360)
  cre_listing_documents   (classified docs; the table already exists)
  cre_listing_links       (off-listing / external links, if any are stranded)

CONTRACT (locked)
-----------------
* PURE-ADDITIVE. Never updates cre_listings, never soft-deletes, never touches
  status / deleted_at, never deletes existing child rows. INSERT-only.
* IDEMPOTENT. Re-running inserts nothing new: every INSERT is anti-joined on
  (listing_id, url) against the live child table (the additive analogue of
  ON CONFLICT DO NOTHING; the documents/media/links tables carry no
  (listing_id, url) unique index, so a NOT EXISTS guard is the correct
  idempotency mechanism here).
* --dry-run is the DEFAULT: it builds the SQL, prints per-shape counts, and
  writes NOTHING. --apply is required to actually write (gated, explicit).
* EXISTENCE-GUARDED tables. cre_listing_media and cre_listing_links are not yet
  in the sql/ migrations (only cre_listing_documents is). Their INSERTs are
  wrapped in a to_regclass(...) IS NOT NULL guard so an --apply against a DB
  that lacks them is a clean no-op for those shapes, never an error. This file
  does NOT create or alter any table (sql/ is owned by the migrations).
* Same DB-connection convention as cre_ingest.py: POSTGRES_URL_NON_POOLING /
  POSTGRES_URL via psql, discovered through the SAME loader
  (cre_ingest.load_db_url) and the SAME psql resolver (cre_ingest.find_psql).
  The URL is never printed (only the env-file path is).

The url classifier (classify_media / classify_doc) is a small, pure-Python
re-implementation of the relevant lib/harvest.ts detection (provider/mediaType
table + ordered docType keywords). It deliberately mirrors harvest.ts so a
later collect.ts re-run classifies the same url the same way; it is NOT a wire
back to collect.ts (that would be overkill for a raw_data sweep).

Usage:
  python3 backfill_media_from_raw_data.py                 # dry-run (default)
  python3 backfill_media_from_raw_data.py --dry-run
  python3 backfill_media_from_raw_data.py --apply         # writes (gated)
  python3 backfill_media_from_raw_data.py --keep-sql /tmp/backfill.sql --dry-run
  python3 backfill_media_from_raw_data.py --env-file /path/.env.local
  python3 backfill_media_from_raw_data.py --source colliers,marcus-millichap

Reads the same env discovery order as cre_ingest.py: --env-file flag, then
CRE_ENV_FILE, then the ~/Documents defaults.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
from urllib.parse import urlsplit

# Reuse the exact DB-connection convention from cre_ingest.py without
# duplicating it (load_db_url + find_psql + sql_lit). conftest.py / a sibling
# location puts cre_collector/ on sys.path under pytest; for a direct CLI run we
# add our own directory too so the import resolves from anywhere.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cre_ingest import find_psql, iter_copy_json_rows, load_db_url, sql_lit  # noqa: E402

# ---------------------------------------------------------------------------
# URL guards (mirror lib/harvest.ts httpUrl / hostOf, never throw)
# ---------------------------------------------------------------------------

_NON_HTTP_SCHEME = re.compile(r"^(?:data:|javascript:|mailto:|tel:|#)", re.I)


def http_url_or_none(raw):
    """Keep only absolute http(s) urls. Mirrors harvest.ts httpUrl (no baseUrl
    resolution: stranded raw_data fields are already absolute). Never throws."""
    if not isinstance(raw, str):
        return None
    s = raw.strip()
    if not s or _NON_HTTP_SCHEME.match(s):
        return None
    if not re.match(r"^https?://", s, re.I):
        return None
    try:
        parts = urlsplit(s)
    except ValueError:
        return None
    if parts.scheme.lower() not in ("http", "https") or not parts.netloc:
        return None
    return s


def host_of(url):
    """Lowercased hostname without leading www. Never throws."""
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        return ""
    return re.sub(r"^www\.", "", host.lower())


# ---------------------------------------------------------------------------
# MEDIA classification (mirror lib/harvest.ts classifyMedia provider table)
# ---------------------------------------------------------------------------

_VIMEO_HOST = re.compile(r"(?:^|\.)(?:vimeo\.com|player\.vimeo\.com)$")
_YOUTUBE_HOST = re.compile(r"(?:^|\.)(?:youtube\.com|youtu\.be|youtube-nocookie\.com)$")
_WISTIA_HOST = re.compile(r"(?:^|\.)(?:wistia\.(?:com|net|io)|wi\.st)$")
_BRIGHTCOVE_HOST = re.compile(r"(?:^|\.)(?:brightcove\.(?:com|net)|bcove\.video)$")
_MATTERPORT_HOST = re.compile(r"(?:^|\.)matterport\.com$")
_KUULA_HOST = re.compile(r"(?:^|\.)kuula\.co$")
_TOUR_PATH = re.compile(r"/360(?:[/?#.]|$)|virtual-?tour", re.I)


def classify_media(url, title=None):
    """Classify a url as media, returning a media dict or None.

    Mirrors harvest.ts classifyMedia's host/path table:
      vimeo/youtube/wistia/brightcove -> video
      matterport                       -> matterport
      kuula.co OR /360 OR virtual-tour -> virtual_tour
    Returns {mediaType, provider, url, embedUrl, title}; embedUrl is left null
    here (the backfill stores the discovered url and lets a later harvest pass
    derive canonical embeds). Returns None when the url is not recognized media.
    """
    u = http_url_or_none(url)
    if not u:
        return None
    host = host_of(u)
    low = u.lower()
    media_type = None
    provider = None
    if _VIMEO_HOST.search(host):
        media_type, provider = "video", "vimeo"
    elif _YOUTUBE_HOST.search(host):
        media_type, provider = "video", "youtube"
    elif _WISTIA_HOST.search(host):
        media_type, provider = "video", "wistia"
    elif _BRIGHTCOVE_HOST.search(host):
        media_type, provider = "video", "brightcove"
    elif _MATTERPORT_HOST.search(host):
        media_type, provider = "matterport", "matterport"
    elif _KUULA_HOST.search(host) or _TOUR_PATH.search(low):
        media_type, provider = "virtual_tour", host or None
    if media_type is None:
        return None
    return {
        "mediaType": media_type,
        "provider": provider,
        "url": u,
        "embedUrl": None,
        "title": _clean(title),
    }


# ---------------------------------------------------------------------------
# DOCUMENT classification (mirror lib/harvest.ts classifyDoc keyword table)
#
# The live cre_listing_documents.doc_type CHECK (sql/002) only allows
# (brochure, om, flyer, floor_plan, other). harvest.ts also emits financials /
# rent_roll (widened in a not-yet-applied sql/011). To stay safe against the
# CURRENT CHECK, classify with the full harvest.ts vocabulary, then clamp any
# type the live table does not accept down to 'other' (DOC_TYPE_DB_ALLOWED).
# ---------------------------------------------------------------------------

_DOC_EXT = re.compile(r"\.(?:pdf|docx?|xlsx?|pptx?)(?:[?#]|$)", re.I)

# doc_type values the LIVE cre_listing_documents CHECK accepts (sql/002).
DOC_TYPE_DB_ALLOWED = {"brochure", "om", "flyer", "floor_plan", "other"}

_DOC_RENT_ROLL = re.compile(r"rent[-_ ]?roll", re.I)
_DOC_FINANCIALS = re.compile(r"financ|pro[-_ ]?forma|proforma|\bt-?12\b", re.I)
_DOC_FLOOR_PLAN = re.compile(r"floor[-_ ]?plan|site[-_ ]?plan|floorplan|siteplan", re.I)
_DOC_OM = re.compile(
    r"offering|memorandum|(?:^|[/_-])om(?:[/_.-]|$)|teaser|dataroom|data[-_ ]room|deal[-_ ]room",
    re.I,
)
_DOC_FLYER = re.compile(r"flyer", re.I)
_DOC_BROCHURE = re.compile(r"brochure|marketing|\bpackage\b|\bdeck\b|\bpib\b", re.I)


def classify_doc(url, title=None):
    """Classify a url as a document, returning a doc dict or None.

    Mirrors harvest.ts classifyDoc: a document needs either a recognized file
    extension OR a documentary keyword in the url/title (gated deal-room links
    frequently lack an extension). Keyword order is most-specific-first.
    Returns {url, title, docType} with docType already clamped to a value the
    live cre_listing_documents CHECK accepts. Returns None for a non-document.
    """
    u = http_url_or_none(url)
    if not u:
        return None
    hay = f"{u.lower()} {(_clean(title) or '').lower()}"
    doc_type = None
    if _DOC_RENT_ROLL.search(hay):
        doc_type = "rent_roll"
    elif _DOC_FINANCIALS.search(hay):
        doc_type = "financials"
    elif _DOC_FLOOR_PLAN.search(hay):
        doc_type = "floor_plan"
    elif _DOC_OM.search(hay):
        doc_type = "om"
    elif _DOC_FLYER.search(hay):
        doc_type = "flyer"
    elif _DOC_BROCHURE.search(hay):
        doc_type = "brochure"
    if doc_type is None:
        if _DOC_EXT.search(u):
            doc_type = "other"
        else:
            return None
    # Clamp to the live CHECK so an --apply never violates the doc_type
    # constraint (financials / rent_roll are not yet allowed on prod).
    if doc_type not in DOC_TYPE_DB_ALLOWED:
        doc_type = "other"
    return {"url": u, "title": _clean(title), "docType": doc_type}


def _clean(v):
    """Trimmed non-empty string, else None (mirrors lib/util.clean intent)."""
    if not isinstance(v, str):
        return None
    s = v.strip()
    return s or None


# ---------------------------------------------------------------------------
# Stranded-shape extraction from one raw_data blob
# ---------------------------------------------------------------------------


def _flat_passes(raw):
    """Yield the flat sub-dicts of a raw_data blob.

    merge_rows() in cre_ingest.py wraps a dual sale+lease payload as
    {primary, secondary_pass}; a single-pass row is flat. Return the blob
    itself plus any primary / secondary_pass sub-dicts so a stranded field on
    either pass is found. Mirrors norm_status()'s sub-pass walk.
    """
    if not isinstance(raw, dict):
        return []
    subs = [raw]
    for key in ("primary", "secondary_pass"):
        nested = raw.get(key)
        if isinstance(nested, dict):
            subs.append(nested)
    return subs


def _iter_urls_titled(value):
    """Yield (url, title) from a stranded video/tour field.

    A field may be a bare string, a {url|href, name|title} object, or a list of
    either. Anything else yields nothing. Never throws.
    """
    if value is None:
        return
    if isinstance(value, list):
        for item in value:
            yield from _iter_urls_titled(item)
        return
    if isinstance(value, str):
        yield value, None
        return
    if isinstance(value, dict):
        url = value.get("url") or value.get("href") or value.get("src")
        title = value.get("title") or value.get("name") or value.get("label")
        if url:
            yield url, title
        return
    # scalar non-string (number/bool) -> nothing


def extract_from_raw(raw):
    """Return (media[], docs[]) classified from one raw_data blob.

    Covers the three known stranded shapes across flat AND {primary,
    secondary_pass} payloads, de-duped by url within this listing. Pure: no DB,
    no network. media[] and docs[] are dicts shaped for the staging COPY.
    """
    media_by_url = {}
    docs_by_url = {}

    for sub in _flat_passes(raw):
        if not isinstance(sub, dict):
            continue

        # 1) JLL stranded media: jllDetail.{videos,virtualTours,view360URLs}
        jll = sub.get("jllDetail")
        if isinstance(jll, dict):
            for field in ("videos", "virtualTours", "view360URLs"):
                for url, title in _iter_urls_titled(jll.get(field)):
                    m = classify_media(url, title)
                    if m is None:
                        # A 360/tour url whose host is unknown and path lacks a
                        # /360|virtual-tour marker would miss the media table; for
                        # the explicitly-tour fields, force a virtual_tour row so
                        # the stranded asset is still recovered.
                        u = http_url_or_none(url)
                        if u and field in ("virtualTours", "view360URLs"):
                            m = {
                                "mediaType": "virtual_tour",
                                "provider": host_of(u) or None,
                                "url": u,
                                "embedUrl": None,
                                "title": _clean(title),
                            }
                    if m and m["url"] not in media_by_url:
                        media_by_url[m["url"]] = m

        # 2) Marcus gated documents: gatedDocuments[] = {name, url, gated}
        for url, title in _iter_urls_titled(sub.get("gatedDocuments")):
            d = classify_doc(url, title)
            if d is None:
                # A gated deal-room link with no keyword/extension is still a
                # document (the source asserted it). Force 'other'.
                u = http_url_or_none(url)
                if u:
                    d = {"url": u, "title": _clean(title), "docType": "other"}
            if d and d["url"] not in docs_by_url:
                docs_by_url[d["url"]] = d

        # 3) Colliers SalesTracker brochure / agreement single-url fields
        cst = sub.get("colliersSalesTrackerDetail")
        if isinstance(cst, dict):
            brochure = http_url_or_none(cst.get("brochureUrl"))
            if brochure and brochure not in docs_by_url:
                d = classify_doc(brochure, "Brochure") or {
                    "url": brochure,
                    "title": "Brochure",
                    "docType": "brochure",
                }
                docs_by_url[brochure] = d
            agreement = http_url_or_none(cst.get("agreementUrl"))
            if agreement and agreement not in docs_by_url:
                # Confidentiality/listing agreement: not a marketing doc -> 'other'.
                d = classify_doc(agreement, "Agreement") or {
                    "url": agreement,
                    "title": "Agreement",
                    "docType": "other",
                }
                # An agreement should never be promoted to a marketing bucket.
                if d["docType"] in ("brochure", "flyer", "om"):
                    d = {"url": agreement, "title": d.get("title") or "Agreement", "docType": "other"}
                docs_by_url[agreement] = d

    return list(media_by_url.values()), list(docs_by_url.values())


# ---------------------------------------------------------------------------
# COPY encoding (mirror cre_ingest.copy_field for the columns we stage)
# ---------------------------------------------------------------------------


def copy_field(v):
    if v is None:
        return "\\N"
    if isinstance(v, bool):
        return "t" if v else "f"
    if isinstance(v, (dict, list)):
        v = json.dumps(v, ensure_ascii=False, separators=(",", ":"))
    s = str(v)
    return (
        s.replace("\\", "\\\\")
        .replace("\t", "\\t")
        .replace("\n", "\\n")
        .replace("\r", "\\r")
    )


# ---------------------------------------------------------------------------
# Reading raw_data out of the DB
# ---------------------------------------------------------------------------

# Brokerage slug filters for the three known stranded shapes. Used to bound the
# COPY scan when --source is given; otherwise all rows are scanned (the
# extractor only matches the three shapes regardless, so the filter is purely a
# scan-cost optimization).
SOURCE_SLUGS = {
    "jll": "jll",
    "marcus-millichap": "marcus-millichap",
    "colliers": "colliers",
}


def read_rows_sql(slugs):
    """Inner SELECT (one JSON object per row) for iter_copy_json_rows: (id,
    raw_data). Cast to ::text for a clean CSV round-trip. CSV COPY avoids the
    text-format backslash-doubling that would silently drop the M&M / jll /
    colliers rows this script targets (their raw_data embeds escaped-quote HTML).
    Only object-typed, not-soft-deleted raw_data is scanned."""
    where = ["l.deleted_at IS NULL", "jsonb_typeof(l.raw_data) = 'object'"]
    if slugs:
        slug_list = ", ".join(sql_lit(s) for s in sorted(slugs))
        where.append(
            "l.brokerage_id IN (SELECT id FROM credeals.cre_brokerages "
            f"WHERE slug IN ({slug_list}))"
        )
    where_sql = " AND ".join(where)
    return (
        "SELECT jsonb_build_object('id', l.id, 'raw_data', l.raw_data)::text "
        f"FROM credeals.cre_listings l WHERE {where_sql}"
    )


def fetch_rows(db_url, psql, slugs):
    """Yield (listing_id, raw_data_dict) via iter_copy_json_rows (CSV COPY;
    aborts loudly on an undecodable row instead of silently skipping it)."""
    for obj in iter_copy_json_rows(psql, db_url, read_rows_sql(slugs), label="media_backfill"):
        lid = obj.get("id")
        raw = obj.get("raw_data")
        if lid is not None and isinstance(raw, dict):
            yield lid, raw


# ---------------------------------------------------------------------------
# Write SQL builder (additive, idempotent, existence-guarded)
# ---------------------------------------------------------------------------

_MEDIA_STAGE_COLS = ["listing_id", "media_type", "provider", "url", "embed_url", "title"]
_DOC_STAGE_COLS = ["listing_id", "doc_type", "title", "url"]
_LINK_STAGE_COLS = ["listing_id", "link_type", "rel", "url"]


def build_sql(media_rows, doc_rows, link_rows):
    """Build the additive, idempotent backfill SQL.

    Each child INSERT is anti-joined on (listing_id, url) against the live
    table (idempotent: a re-run inserts nothing). cre_listing_media and
    cre_listing_links are existence-guarded with to_regclass so an apply
    against a DB without those tables is a no-op for those shapes (this script
    never creates tables; sql/ owns DDL). cre_listing_documents always exists
    (sql/002) so its INSERT is unguarded.
    """
    lines = []
    w = lines.append
    w("\\set ON_ERROR_STOP on")
    w("BEGIN;")
    w("SET LOCAL statement_timeout = '600s';")
    w("SET LOCAL standard_conforming_strings = on;")

    # --- staging tables ---
    w("""
CREATE TEMP TABLE _bf_media (
    listing_id uuid, media_type text, provider text, url text,
    embed_url text, title text
) ON COMMIT DROP;""")
    w(f"COPY _bf_media ({', '.join(_MEDIA_STAGE_COLS)}) FROM stdin;")
    for r in media_rows:
        w("\t".join(copy_field(r[c]) for c in _MEDIA_STAGE_COLS))
    w("\\.")

    w("""
CREATE TEMP TABLE _bf_docs (
    listing_id uuid, doc_type text, title text, url text
) ON COMMIT DROP;""")
    w(f"COPY _bf_docs ({', '.join(_DOC_STAGE_COLS)}) FROM stdin;")
    for r in doc_rows:
        w("\t".join(copy_field(r[c]) for c in _DOC_STAGE_COLS))
    w("\\.")

    w("""
CREATE TEMP TABLE _bf_links (
    listing_id uuid, link_type text, rel text, url text
) ON COMMIT DROP;""")
    w(f"COPY _bf_links ({', '.join(_LINK_STAGE_COLS)}) FROM stdin;")
    for r in link_rows:
        w("\t".join(copy_field(r[c]) for c in _LINK_STAGE_COLS))
    w("\\.")

    # --- documents: table always exists (sql/002); anti-join idempotent insert ---
    w("""
-- Documents: additive, idempotent. Insert a staged doc only when no row with
-- the same (listing_id, url) already exists (the additive analogue of ON
-- CONFLICT DO NOTHING; cre_listing_documents has no (listing_id,url) unique
-- index, so NOT EXISTS is the correct idempotency guard).
INSERT INTO credeals.cre_listing_documents (listing_id, doc_type, title, url)
SELECT s.listing_id, s.doc_type, s.title, s.url
FROM _bf_docs s
WHERE s.url IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM credeals.cre_listing_documents d
      WHERE d.listing_id = s.listing_id AND d.url = s.url
  );""")

    # --- media: existence-guarded (table not yet in sql/ migrations) ---
    w("""
-- Media: existence-guarded (cre_listing_media is not yet in sql/ migrations).
-- A no-op when the table is absent; never an error. Column set follows
-- harvest.ts MediaItem (media_type/provider/url/embed_url/title). Anti-joined
-- on (listing_id, url) for idempotency.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_media') IS NOT NULL THEN
    INSERT INTO credeals.cre_listing_media (listing_id, media_type, provider, url, embed_url, title)
    SELECT s.listing_id, s.media_type, s.provider, s.url, s.embed_url, s.title
    FROM _bf_media s
    WHERE s.url IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM credeals.cre_listing_media m
          WHERE m.listing_id = s.listing_id AND m.url = s.url
      );
  ELSE
    RAISE NOTICE 'cre_listing_media absent; skipped % staged media row(s)',
      (SELECT count(*) FROM _bf_media);
  END IF;
END $$;""")

    # --- links: existence-guarded (table not yet in sql/ migrations) ---
    w("""
-- Links: existence-guarded (cre_listing_links is not yet in sql/ migrations).
-- No links are stranded in the three known shapes today, so this is normally
-- empty; kept for completeness and forward-compat. Anti-joined for idempotency.
DO $$ BEGIN
  IF to_regclass('credeals.cre_listing_links') IS NOT NULL THEN
    INSERT INTO credeals.cre_listing_links (listing_id, link_type, rel, url)
    SELECT s.listing_id, s.link_type, s.rel, s.url
    FROM _bf_links s
    WHERE s.url IS NOT NULL
      AND NOT EXISTS (
          SELECT 1 FROM credeals.cre_listing_links k
          WHERE k.listing_id = s.listing_id AND k.url = s.url
      );
  ELSE
    RAISE NOTICE 'cre_listing_links absent; skipped % staged link row(s)',
      (SELECT count(*) FROM _bf_links);
  END IF;
END $$;""")

    w("COMMIT;")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _summarize(media_rows, doc_rows, link_rows):
    """Per-shape and per-doc-type counts for the dry-run report."""
    listings_with_media = len({r["listing_id"] for r in media_rows})
    listings_with_docs = len({r["listing_id"] for r in doc_rows})
    media_by_type = {}
    for r in media_rows:
        media_by_type[r["media_type"]] = media_by_type.get(r["media_type"], 0) + 1
    doc_by_type = {}
    for r in doc_rows:
        doc_by_type[r["doc_type"]] = doc_by_type.get(r["doc_type"], 0) + 1
    return {
        "media_rows": len(media_rows),
        "media_listings": listings_with_media,
        "media_by_type": media_by_type,
        "doc_rows": len(doc_rows),
        "doc_listings": listings_with_docs,
        "doc_by_type": doc_by_type,
        "link_rows": len(link_rows),
    }


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument(
        "--dry-run", action="store_true",
        help="(default) build SQL, print counts, write nothing",
    )
    mode.add_argument(
        "--apply", action="store_true",
        help="actually INSERT the stranded media/docs (gated; off by default)",
    )
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument(
        "--source", default=None,
        help="comma list of brokerage slugs to scan (jll, marcus-millichap, "
             "colliers); default scans all (extractor only matches the three shapes)",
    )
    ap.add_argument(
        "--keep-sql", default=None,
        help="write the generated SQL to this path (works in dry-run too)",
    )
    args = ap.parse_args()

    # Default is dry-run: only --apply writes.
    apply = bool(args.apply)

    slugs = None
    if args.source:
        requested = [s.strip() for s in args.source.split(",") if s.strip()]
        unknown = [s for s in requested if s not in SOURCE_SLUGS]
        if unknown:
            sys.exit(f"unknown --source slug(s): {', '.join(unknown)} (known: {', '.join(SOURCE_SLUGS)})")
        slugs = {SOURCE_SLUGS[s] for s in requested}

    db_url, env_path = load_db_url(args.env_file)
    print(f"[backfill] env file: {env_path}")  # path only, never the URL
    psql = find_psql()

    media_rows, doc_rows, link_rows = [], [], []
    scanned = 0
    for lid, raw in fetch_rows(db_url, psql, slugs):
        scanned += 1
        media, docs = extract_from_raw(raw)
        for m in media:
            media_rows.append({
                "listing_id": lid,
                "media_type": m["mediaType"],
                "provider": m["provider"],
                "url": m["url"],
                "embed_url": m["embedUrl"],
                "title": m["title"],
            })
        for d in docs:
            doc_rows.append({
                "listing_id": lid,
                "doc_type": d["docType"],
                "title": d["title"],
                "url": d["url"],
            })

    stats = _summarize(media_rows, doc_rows, link_rows)
    print(f"[backfill] listings scanned: {scanned}")
    print(
        f"[backfill] MEDIA   : {stats['media_rows']} row(s) across "
        f"{stats['media_listings']} listing(s) {stats['media_by_type'] or '{}'}"
    )
    print(
        f"[backfill] DOCS    : {stats['doc_rows']} row(s) across "
        f"{stats['doc_listings']} listing(s) {stats['doc_by_type'] or '{}'}"
    )
    print(f"[backfill] LINKS   : {stats['link_rows']} row(s)")

    sql = build_sql(media_rows, doc_rows, link_rows)
    if args.keep_sql:
        with open(args.keep_sql, "w") as f:
            f.write(sql)
        print(f"[backfill] SQL written to {args.keep_sql}")

    if not apply:
        print("[backfill] DRY-RUN: no rows written. Re-run with --apply to write.")
        return

    if not (media_rows or doc_rows):
        print("[backfill] nothing to insert; skipping --apply write.")
        return

    with tempfile.NamedTemporaryFile("w", suffix=".sql", delete=False) as tf:
        tf.write(sql)
        sql_path = tf.name
    try:
        proc = subprocess.run(
            [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", sql_path],
            capture_output=True,
            text=True,
        )
        sys.stdout.write(proc.stdout)
        if proc.returncode != 0:
            sys.stderr.write(proc.stderr)
            sys.exit(f"psql apply exited {proc.returncode}")
        # Surface RAISE NOTICE lines (existence-guard skips, counts).
        if proc.stderr.strip():
            sys.stderr.write(proc.stderr)
    finally:
        os.unlink(sql_path)
    print("[backfill] APPLIED.")


if __name__ == "__main__":
    main()
