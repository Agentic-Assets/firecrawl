#!/usr/bin/env python3
"""om_parse.py: retired Firecrawl OM/PDF writer support (Phase-2 data-lift WS2).

The former highest-stakes write path in the data-lift: financial scalars parsed from an
Offering Memorandum / brochure PDF can reach board-facing cre_listings columns
(noi, cap_rate, occupancy_rate, units, year_built). So this module is
conservative and PROVENANCE-FIRST:

  - Document SELECTION: listings whose cre_listing_documents has a parseable
    OM / financials / rent_roll / brochure PDF (a `.pdf` URL or a resolvable
    viewer URL, via om_url_resolver) AND which still LACK an underwriting field.
    CBRE + JLL first (best OM density per the gap doc).
  - PARSE: the LOCAL self-hosted Firecrawl /v2/parse (Docling) renders the PDF
    to markdown/text. Zero cloud cost. No third-party spend.
  - EXTRACT: a PURE function extract_om_facts(text, source_doc_url) pulls noi,
    cap_rate, occupancy_rate, units, year_built and unit_mix / rent_roll line
    items. EVERY emitted scalar carries provenance (source_doc_url,
    parser_version, confidence in [0,1]). A non-underwriting PDF yields ZERO
    scalars (never fabricate).
  - CONFIDENCE FLOOR: extraction still classifies values by confidence for
    regression coverage, but Firecrawl does not write either parent scalars or
    OM-fact rows.
  - DRY-RUN ARTIFACTS are explicitly marked as retired diagnostics. The
    collector ingestor rejects both marked artifacts and the older
    externalId-plus-omFacts row shape.
  - GetCREdata is the sole production OM extraction writer. Firecrawl keeps the
    pure extractors and dry-run artifact path for regression coverage, but
    `--apply` fails closed before it can select, parse, or write to a database.
    Nothing is applied to prod and no connection string is ever printed.

Structure: PURE extractors / builders (asserted by tests with no DB, no network)
plus a thin run() that wires select -> parse -> extract -> diagnostic artifact. Reuses
cre_parse.py for ALL numeric parsing (never reinvents a regex) and reuses
cre_ingest.load_db_url / find_psql / sql_lit so credential loading and SQL
escaping are byte-identical to the production ingest path.

Usage:
  python3 om_parse.py                       # dry-run: select + parse + extract,
                                            # write the artifact, do NOT ingest
  python3 om_parse.py --apply               # rejected: writer retired
  python3 om_parse.py --sources cbre,jll    # restrict to source keys (default cbre,jll)
  python3 om_parse.py --limit 50            # cap candidate listings this run
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone

# Reuse cre_ingest verbatim for env-file discovery + SQL escaping + psql
# discovery (never re-implement credential loading or quote-doubling).
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cre_ingest import (  # noqa: E402
    RETIRED_OM_PARSE_ARTIFACT_KIND,
    find_psql,
    load_db_url,
    sql_lit,
)

# All numeric parsing goes through the frozen shared parser library (contract
# C.3). Never reinvent a money / percent / size regex here.
from cre_parse import (  # noqa: E402
    parse_money,
    parse_percent_to_fraction,
)
from om_url_resolver import resolve_pdf_url  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
OUT_OM_DIR = os.path.join(HERE, "out", "om")

# Parser version tag stamped on every emitted provenance row. Bump when the
# extractor logic changes so a re-parse is attributable (contract A.2).
PARSER_VERSION = "om-parse/1"

# Default Firecrawl API base (the local self-hosted stack; zero cloud cost).
DEFAULT_API_URL = os.getenv("FIRECRAWL_API_URL", "http://localhost:3002")

# Confidence floor: a scalar BELOW this writes ONLY cre_listing_om_facts, never
# the cre_listings column (contract H, open-risk 4 mitigation). A wrong NOI on a
# board column is the worst failure mode, so the gate is strict.
CONFIDENCE_FLOOR = 0.6

# doc_type values worth parsing for OM scalars. brochure included because the
# 70,414 existing rows are all typed 'brochure' until the re-classification pass
# (contract Section D); selection is not blocked on that pass.
PARSEABLE_DOC_TYPES = ("om", "financials", "rent_roll", "brochure")

# Scalar fact_keys that map onto an existing cre_listings column (contract A.2).
# The artifact builder emits the matching camelCase listing key for each when
# confidence clears the floor. fact_key is the snake_case cre_listings column.
_SCALAR_TO_LISTING_KEY = {
    "noi": "noi",
    "cap_rate": "capRatePct",          # cre_ingest norm_cap_rate accepts a percent
    "occupancy_rate": "occupancyRate",  # cre_ingest norm_occupancy_rate -> fraction
    "units": "units",
    "year_built": "yearBuilt",
}


# ---------------------------------------------------------------------------
# Pure extractors (no DB, no network; the unit tests assert on these)
# ---------------------------------------------------------------------------


def _clip(text):
    """Normalize whitespace for line/label matching (markdown can be ragged)."""
    return re.sub(r"[ \t]+", " ", text or "")


# NOI: a "Net Operating Income" label followed by a money value on the same or
# next short span. We require the explicit label (no bare "$N" guessing) so a
# random dollar figure in prose is never read as NOI.
_NOI_LABEL_RE = re.compile(
    r"net\s+operating\s+income\b[^$%\n\r]{0,40}?(\$\s*[0-9][0-9,]*(?:\.[0-9]+)?)",
    re.I,
)
_NOI_ABBR_RE = re.compile(
    r"\bNOI\b[^$%\n\r]{0,30}?(\$\s*[0-9][0-9,]*(?:\.[0-9]+)?)",
)

# Cap rate: a "Cap Rate" / "Capitalization Rate" label followed by a percent.
_CAP_RATE_RE = re.compile(
    r"(?:cap(?:italization)?\s*rate)\b[^%\n\r]{0,30}?([0-9]+(?:\.[0-9]+)?)\s*%",
    re.I,
)

# Occupancy: an "Occupancy" label followed by a percent.
_OCCUPANCY_RE = re.compile(
    r"\boccupancy\b[^%\n\r]{0,30}?([0-9]+(?:\.[0-9]+)?)\s*%",
    re.I,
)

# Units: a "Number of Units" / "Total Units" / "N Units" form -> integer.
_UNITS_LABEL_RE = re.compile(
    r"(?:number\s+of\s+units|total\s+units|unit\s+count)\b[^0-9\n\r]{0,20}?([0-9]{1,5})",
    re.I,
)
_UNITS_SUFFIX_RE = re.compile(r"\b([0-9]{1,5})\s+units\b", re.I)

# Year built: a "Year Built" / "Built in" / "Constructed in" label -> 4-digit yr.
_YEAR_BUILT_RE = re.compile(
    r"(?:year\s+built|built(?:\s+in)?|year\s+of\s+construction|constructed(?:\s+in)?)\b[^0-9\n\r]{0,15}?((?:18|19|20)\d{2})",
    re.I,
)

# Unit-mix table row: "<count> <unit-type>" with optional rent, e.g.
# "12 | 1BR/1BA | $1,450" or "12 One Bedroom $1,450". We key off an explicit
# unit-count + a bedroom/type token so prose is not mis-read as a unit-mix row.
_UNIT_MIX_RE = re.compile(
    r"(?P<count>[0-9]{1,4})\s*[|x*]?\s*"
    r"(?P<type>(?:studio|[0-9]\s*(?:br|bd|bed(?:room)?s?)(?:\s*/?\s*[0-9]\s*(?:ba|bath(?:room)?s?))?))"
    r"[^$\n\r]{0,40}?(?:\$\s*(?P<rent>[0-9][0-9,]*(?:\.[0-9]+)?))?",
    re.I,
)

# Rent-roll row: a tenant name (a quoted/Title-cased label) with a monthly /
# annual rent. Conservative: require a "$" rent value adjacent to a recognizable
# "Tenant" / "Suite" context so a narrative sentence is not captured.
_RENT_ROLL_RE = re.compile(
    r"(?:tenant|suite|unit)\s*[:#]?\s*(?P<label>[A-Za-z0-9 .,'&/-]{2,40}?)\s+"
    r"\$\s*(?P<rent>[0-9][0-9,]*(?:\.[0-9]+)?)",
    re.I,
)


def _confidence_for(label_specific, value_plausible):
    """Two-factor heuristic confidence in [0,1].

    label_specific: the value sat next to an explicit, unambiguous label
      (e.g. "Net Operating Income" / "Cap Rate"), not a bare token.
    value_plausible: the parsed value is in a sane domain for its field.

    Both true -> 0.8 (above the 0.6 floor: write the column). Label-specific but
    a borderline value, or a weaker label -> 0.5 (below floor: om_facts only).
    Neither -> caller does not emit the scalar at all.
    """
    if label_specific and value_plausible:
        return 0.8
    if label_specific or value_plausible:
        return 0.5
    return 0.3


def _fact(fact_key, source_doc_url, *, num=None, text=None, confidence,
          group="scalar", unit_count=None):
    """Shape one provenance-bearing om_facts row in the cre_ingest omFacts schema.

    Mirrors cre_ingest.om_facts_rows exactly: factGroup / factKey / factValueText
    / factValueNum / unitCount / sourceDocUrl / parserVersion / confidence. Every
    row carries source_doc_url + parser_version + confidence (the provenance
    contract; a row missing any of those is dropped by the ingest, so we always
    set them).
    """
    return {
        "factGroup": group,
        "factKey": fact_key,
        "factValueText": text,
        "factValueNum": num,
        "unitCount": unit_count,
        "sourceDocUrl": source_doc_url,
        "parserVersion": PARSER_VERSION,
        "confidence": round(float(confidence), 3),
    }


def _extract_noi(text, source_doc_url):
    m = _NOI_LABEL_RE.search(text) or _NOI_ABBR_RE.search(text)
    if not m:
        return None
    val = parse_money(m.group(1))
    if val is None or val <= 0:
        return None
    # Plausible commercial NOI: $1k .. $1B. Outside that, weaker confidence.
    plausible = 1_000 <= val <= 1_000_000_000
    specific = bool(_NOI_LABEL_RE.search(text))  # full label beats the bare abbr
    return _fact("noi", source_doc_url, num=val,
                 confidence=_confidence_for(specific, plausible))


def _extract_cap_rate(text, source_doc_url):
    m = _CAP_RATE_RE.search(text)
    if not m:
        return None
    frac = parse_percent_to_fraction(m.group(1) + "%")
    if frac is None:
        return None
    # Commercial cap rates cluster in ~3%-15%; reject the norm_cap_rate guards'
    # rejects (>= 0.5 fraction) defensively and grade plausibility tightly.
    if frac <= 0 or frac >= 0.5:
        return None
    plausible = 0.02 <= frac <= 0.20
    return _fact("cap_rate", source_doc_url, num=round(frac, 6),
                 confidence=_confidence_for(True, plausible))


def _extract_occupancy(text, source_doc_url):
    m = _OCCUPANCY_RE.search(text)
    if not m:
        return None
    frac = parse_percent_to_fraction(m.group(1) + "%")
    if frac is None or not (0 < frac <= 1):
        return None
    plausible = 0.30 <= frac <= 1.0  # a sub-30% occupancy in an OM is rare
    return _fact("occupancy_rate", source_doc_url, num=round(frac, 6),
                 confidence=_confidence_for(True, plausible))


def _extract_units(text, source_doc_url):
    m = _UNITS_LABEL_RE.search(text)
    specific = bool(m)
    if not m:
        m = _UNITS_SUFFIX_RE.search(text)
    if not m:
        return None
    try:
        val = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if val <= 0 or val > 100_000:
        return None
    plausible = 1 <= val <= 10_000
    return _fact("units", source_doc_url, num=val,
                 confidence=_confidence_for(specific, plausible))


def _extract_year_built(text, source_doc_url):
    m = _YEAR_BUILT_RE.search(text)
    if not m:
        return None
    try:
        yr = int(m.group(1))
    except (TypeError, ValueError):
        return None
    if not (1800 < yr <= datetime.now(timezone.utc).year + 1):
        return None
    return _fact("year_built", source_doc_url, num=yr,
                 confidence=_confidence_for(True, True))


def _extract_unit_mix(text, source_doc_url):
    """unit_mix line items: one row per (unit_type, count[, rent]). Always
    om_facts-only (no scalar column), so confidence is informational."""
    rows = []
    seen = set()
    for m in _UNIT_MIX_RE.finditer(text):
        try:
            count = int(m.group("count"))
        except (TypeError, ValueError):
            continue
        if count <= 0 or count > 10_000:
            continue
        unit_type = _clip(m.group("type")).strip().lower()
        key = (unit_type, count)
        if key in seen:
            continue
        seen.add(key)
        rent = m.group("rent")
        rent_num = parse_money("$" + rent) if rent else None
        rows.append(_fact(
            "unit_type", source_doc_url, group="unit_mix",
            text=unit_type, num=rent_num, unit_count=count,
            confidence=0.6,
        ))
    return rows


def _extract_rent_roll(text, source_doc_url):
    """rent_roll line items: one row per (tenant/suite label, rent). Always
    om_facts-only."""
    rows = []
    seen = set()
    for m in _RENT_ROLL_RE.finditer(text):
        label = _clip(m.group("label")).strip(" .,-")
        rent_num = parse_money("$" + m.group("rent"))
        if not label or rent_num is None or rent_num <= 0:
            continue
        key = (label.lower(), rent_num)
        if key in seen:
            continue
        seen.add(key)
        rows.append(_fact(
            "tenant", source_doc_url, group="rent_roll",
            text=label, num=rent_num,
            confidence=0.55,
        ))
    return rows


def extract_om_facts(text, source_doc_url):
    """Pure OM-fact extractor: text + source doc URL -> list of provenance-bearing
    om_facts-shaped dicts (cre_ingest omFacts schema).

    Pulls the scalar underwriting fields (noi, cap_rate, occupancy_rate, units,
    year_built) and the non-scalar unit_mix / rent_roll line items. EVERY scalar
    carries (source_doc_url, parser_version, confidence). A non-underwriting PDF
    (no recognizable labels) yields ZERO rows: never fabricate.

    The confidence FLOOR is NOT applied here (this returns the full audit set);
    listing_scalars_from_facts() applies the floor when deciding which scalars
    also write a cre_listings column.
    """
    if not isinstance(text, str) or not text.strip():
        return []
    if not isinstance(source_doc_url, str) or not source_doc_url:
        return []
    norm = _clip(text)
    facts = []
    for fn in (_extract_noi, _extract_cap_rate, _extract_occupancy,
               _extract_units, _extract_year_built):
        row = fn(norm, source_doc_url)
        if row is not None:
            facts.append(row)
    facts.extend(_extract_unit_mix(norm, source_doc_url))
    facts.extend(_extract_rent_roll(norm, source_doc_url))
    return facts


def listing_scalars_from_facts(facts):
    """Map the SCALAR facts that clear CONFIDENCE_FLOOR to cre_listings camelCase
    keys (so cre_ingest COALESCE-writes the column). Low-confidence scalars are
    omitted here (they still ride along as om_facts provenance rows). unit_mix /
    rent_roll never produce a scalar column.

    Returns a dict of {camelCaseListingKey: value}. When two facts share a
    fact_key (only possible across docs in a merged listing), the higher-confidence
    one wins.
    """
    best = {}
    for f in facts:
        if f.get("factGroup") != "scalar":
            continue
        fact_key = f.get("factKey")
        listing_key = _SCALAR_TO_LISTING_KEY.get(fact_key)
        if listing_key is None:
            continue
        conf = f.get("confidence")
        if conf is None or conf < CONFIDENCE_FLOOR:
            continue
        val = f.get("factValueNum")
        if val is None:
            continue
        prev = best.get(listing_key)
        if prev is None or conf > prev[1]:
            best[listing_key] = (val, conf)
    return {k: v[0] for k, v in best.items()}


def build_enriched_listing(candidate, facts):
    """Shape one re-ingest listing dict from a candidate + its extracted facts.

    The candidate carries the keys cre_ingest needs to address the row:
    sourceKey, externalId (native source id), url. We attach:
      - the cre_listings scalar keys for high-confidence facts (COALESCE-written),
      - the full omFacts provenance list (every fact, including low-confidence and
        line items) for the cre_listing_om_facts audit trail.

    Returns None when there are no facts at all (a non-underwriting PDF): we never
    emit an empty enrichment that would still trigger a child refresh.
    """
    if not facts:
        return None
    listing = {
        "sourceKey": candidate.get("sourceKey"),
        "externalId": candidate.get("externalId"),
        "url": candidate.get("url"),
        # transactionMode preserved so the merge tag is correct; ingest merges
        # sale+lease anyway, so a default does not blank data.
        "transactionMode": candidate.get("transactionMode") or "sale",
        "omFacts": facts,
    }
    listing.update(listing_scalars_from_facts(facts))
    return listing


# ---------------------------------------------------------------------------
# Candidate selection SQL (pure builder; tests assert the shape, no DB)
# ---------------------------------------------------------------------------


def build_candidate_sql(source_keys, *, limit, brokerage_slugs):
    """SELECT listings with a parseable OM/brochure document that still LACK an
    underwriting field. Joins cre_listings + cre_listing_documents.

    Selection predicate (contract Section D / gap doc):
      - the listing's brokerage is in `brokerage_slugs` (CBRE + JLL first),
      - it has >= 1 cre_listing_documents row whose doc_type is in
        PARSEABLE_DOC_TYPES and whose url ends in '.pdf' OR is a viewer URL we
        can resolve (the .pdf gate is applied in SQL; viewer resolution happens
        in Python via om_url_resolver, so we also pass through non-.pdf docs on
        the parseable doc_types and let Python decide),
      - AND the listing is missing at least one underwriting scalar
        (noi / cap_rate / occupancy_rate / units / year_built all relevant).

    Pins ON_ERROR_STOP + standard_conforming_strings exactly like
    cre_monitor.build_write_sql. `limit` is validated to an int and inlined as a
    bare integer; slugs / doc_types go through sql_lit. The brokerage slug list
    is sql_lit-quoted; no scraped text is f-string'd. Never selects soft-deleted
    rows.
    """
    limit = int(limit)
    slug_list = "(" + ", ".join(sql_lit(s) for s in brokerage_slugs) + ")"
    doc_type_list = "(" + ", ".join(sql_lit(t) for t in PARSEABLE_DOC_TYPES) + ")"
    return "\n".join([
        "\\set ON_ERROR_STOP on",
        "SET standard_conforming_strings = on;",
        "SELECT l.external_id, l.source_url, b.slug",
        "  FROM credeals.cre_listings l",
        "  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id",
        " WHERE l.deleted_at IS NULL",
        "   AND b.slug IN " + slug_list,
        "   AND EXISTS (",
        "     SELECT 1 FROM credeals.cre_listing_documents d",
        "      WHERE d.listing_id = l.id",
        "        AND d.doc_type IN " + doc_type_list,
        "        AND d.url IS NOT NULL",
        "   )",
        "   AND (l.noi IS NULL OR l.cap_rate IS NULL OR l.occupancy_rate IS NULL",
        "        OR l.units IS NULL OR l.year_built IS NULL)",
        " ORDER BY (l.noi IS NULL)::int + (l.cap_rate IS NULL)::int DESC",
        " LIMIT {};".format(limit),
    ])


def build_docs_sql(external_ids, brokerage_slugs):
    """Fetch the parseable document URLs for a set of (slug, external_id) listings.

    Returns rows (slug, external_id, doc_url, doc_type) for documents on the
    parseable doc_types. external_ids go through sql_lit; empty input yields a
    no-op SELECT (no malformed `IN ()`).
    """
    ids = sorted({e for e in external_ids if e})
    doc_type_list = "(" + ", ".join(sql_lit(t) for t in PARSEABLE_DOC_TYPES) + ")"
    slug_list = "(" + ", ".join(sql_lit(s) for s in brokerage_slugs) + ")"
    head = [
        "\\set ON_ERROR_STOP on",
        "SET standard_conforming_strings = on;",
    ]
    if not ids:
        return "\n".join(head + ["SELECT NULL WHERE false;"])
    id_list = "(" + ", ".join(sql_lit(i) for i in ids) + ")"
    return "\n".join(head + [
        "SELECT b.slug, l.external_id, d.url, d.doc_type",
        "  FROM credeals.cre_listings l",
        "  JOIN credeals.cre_brokerages b ON b.id = l.brokerage_id",
        "  JOIN credeals.cre_listing_documents d ON d.listing_id = l.id",
        " WHERE l.deleted_at IS NULL",
        "   AND b.slug IN " + slug_list,
        "   AND l.external_id IN " + id_list,
        "   AND d.doc_type IN " + doc_type_list,
        "   AND d.url IS NOT NULL;",
    ])


# ---------------------------------------------------------------------------
# Firecrawl /v2/parse client (the only network boundary; mocked in tests)
# ---------------------------------------------------------------------------


def parse_pdf_to_text(doc_url, *, api_url=None, timeout=180.0):
    """Render a PDF doc URL to markdown/text via the LOCAL Firecrawl /v2/parse.

    Resolves a viewer-wrapped URL to its .pdf first (om_url_resolver); an
    unresolvable URL returns None (skip, never guess). Downloads the PDF bytes
    and POSTs them as a multipart /v2/parse upload, reusing firecrawl_request's
    multipart helper. Returns the markdown string, or None on any failure (a
    failed parse must NEVER fabricate facts).

    This is the only function that touches the network; tests monkeypatch it.
    """
    resolved = resolve_pdf_url(doc_url)
    if not resolved:
        return None
    api_url = api_url or DEFAULT_API_URL
    # Lazy import so the pure extractors stay import-light and offline-testable.
    import tempfile
    import urllib.request
    from pathlib import Path

    sys.path.insert(0, os.path.dirname(HERE))  # scripts/firecrawl-ops on path
    try:
        from firecrawl_request import decode_json_or_bytes, request_multipart
    except ImportError:
        return None

    tmp = None
    try:
        with urllib.request.urlopen(resolved, timeout=timeout) as resp:
            data = resp.read()
        if not data:
            return None
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(data)
            tmp = fh.name
        options = {"formats": ["markdown"], "parsers": [{"type": "pdf", "mode": "auto"}]}
        status, raw = request_multipart(
            api_url, "/v2/parse",
            {"options": json.dumps(options, separators=(",", ":"))},
            {"file": Path(tmp)},
            None, timeout,
        )
        if status >= 400:
            return None
        result = decode_json_or_bytes(raw)
        payload = result.get("data") if isinstance(result, dict) else None
        if isinstance(payload, dict):
            md = payload.get("markdown")
            if isinstance(md, str) and md.strip():
                return md
        return None
    except Exception:
        return None
    finally:
        if tmp and os.path.isfile(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


# ---------------------------------------------------------------------------
# psql plumbing (mirrors cre_enrich._psql_query; never prints the DB url)
# ---------------------------------------------------------------------------


def _psql_query(db_url, sql):
    """Run one statement-set, return RETURNING/SELECT tuples. Never prints the url.

    The SQL is fed on STDIN via `-f -` (not `-c`) so the psql meta-command head
    the builders emit (`\\set ON_ERROR_STOP on`, pinned exactly like
    cre_monitor.build_write_sql) is honored; `-c` does not process backslash
    meta-commands and mis-parses the script. `-tA -F$'\\t'` keeps NULL rendering
    as an empty field for the tab split below.
    """
    psql = find_psql()
    proc = subprocess.run(
        [psql, db_url, "-q", "-tA", "-F", "\t", "-v", "ON_ERROR_STOP=1", "-f", "-"],
        input=sql,
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


# ---------------------------------------------------------------------------
# run() orchestration (thin; select -> parse -> extract -> re-ingest)
# ---------------------------------------------------------------------------


def _slugs_for_sources(source_keys):
    """Map source keys to brokerage slugs via cre_ingest.SOURCE_TO_BROKERAGE."""
    from cre_ingest import SOURCE_TO_BROKERAGE
    slugs = set()
    for k in source_keys:
        mapping = SOURCE_TO_BROKERAGE.get(k)
        if mapping:
            slugs.add(mapping[0])
        else:
            # Allow passing a brokerage slug directly (cbre, jll).
            slugs.add(k)
    return sorted(slugs)


RETIRED_WRITER_EXIT_CODE = 78


def run(args):
    if getattr(args, "apply", False):
        print(
            "om_parse.py --apply is retired: GetCREdata is the sole production "
            "OM extraction writer.",
            file=sys.stderr,
        )
        return RETIRED_WRITER_EXIT_CODE

    source_keys = [s.strip() for s in (args.sources or "").split(",") if s.strip()]
    if not source_keys:
        source_keys = ["cbre", "jll"]
    brokerage_slugs = _slugs_for_sources(source_keys)
    candidate_sql = build_candidate_sql(
        source_keys, limit=args.limit, brokerage_slugs=brokerage_slugs)

    if args.dry_run and args.show_sql:
        print("dry-run (--show-sql): not connecting", file=sys.stderr)
        print(candidate_sql)
        return 0

    db_url, env_path = load_db_url(args.env_file)
    print(f"credentials: {env_path}", file=sys.stderr)

    # (1) Select candidate listings (slug, external_id, source_url).
    rows = _psql_query(db_url, candidate_sql)
    candidates = []
    by_slug_ext = {}
    for t in rows:
        t = tuple(t) + ("",) * (3 - len(t))
        external_id, source_url, slug = t[:3]
        cand = {
            # per-row sourceKey resolved from the listing's own slug (not source_keys[0])
            "sourceKey": _source_key_for_slug(slug, source_keys),
            "externalId": external_id or None,
            "url": source_url or None,
            "slug": slug or None,
        }
        candidates.append(cand)
        by_slug_ext[(slug, external_id)] = cand
    if not candidates:
        print("0 candidates: nothing to OM-parse", file=sys.stderr)
        return 0
    print(f"{len(candidates)} candidate listing(s)", file=sys.stderr)

    # (2) Fetch their parseable document URLs.
    docs_sql = build_docs_sql([c["externalId"] for c in candidates], brokerage_slugs)
    doc_rows = _psql_query(db_url, docs_sql)
    docs_by_listing = {}
    for t in doc_rows:
        t = tuple(t) + ("",) * (4 - len(t))
        slug, external_id, doc_url, _doc_type = t[:4]
        if not doc_url:
            continue
        docs_by_listing.setdefault((slug, external_id), []).append(doc_url)

    # (3) Parse + extract per candidate, building enriched listings.
    enriched = []
    for cand in candidates:
        doc_urls = docs_by_listing.get((cand["slug"], cand["externalId"]), [])
        facts = []
        for doc_url in doc_urls:
            text = parse_pdf_to_text(doc_url, api_url=args.api_url)
            if not text:
                continue
            facts.extend(extract_om_facts(text, doc_url))
        listing = build_enriched_listing(cand, facts)
        if listing is not None:
            enriched.append(listing)

    print(f"{len(enriched)} listing(s) gained OM facts", file=sys.stderr)
    if not enriched:
        return 0

    # (4) Write a review-only artifact.  This envelope is intentionally not a
    # collector input: cre_ingest rejects it before staging any listing.
    os.makedirs(OUT_OM_DIR, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    artifact_path = os.path.join(OUT_OM_DIR, f"om_{stamp}.json")
    with open(artifact_path, "w") as f:
        json.dump(
            {
                "artifactKind": RETIRED_OM_PARSE_ARTIFACT_KIND,
                "listings": enriched,
            },
            f,
            indent=2,
        )
    print(f"artifact: {artifact_path}", file=sys.stderr)
    print("dry-run: retired diagnostic artifact written; it cannot be ingested.", file=sys.stderr)
    return 0


def _source_key_for_slug(slug, source_keys):
    """Pick the source_key whose brokerage slug matches; fall back to the slug.

    Uses cre_ingest.SOURCE_TO_BROKERAGE so the re-ingest sourceKey resolves to the
    right brokerage fold. A slug with multiple source keys (cbre / cbre-dealflow)
    resolves to the FLAT key (the one whose prefix is empty) so external_id is not
    re-prefixed on re-ingest.
    """
    from cre_ingest import SOURCE_TO_BROKERAGE
    matches = [k for k in source_keys
               if SOURCE_TO_BROKERAGE.get(k, (None,))[0] == slug]
    for k in matches:
        if SOURCE_TO_BROKERAGE.get(k, (None, ""))[1] == "":
            return k
    if matches:
        return matches[0]
    # The candidate's external_id is already the stored (folded) id, so re-ingest
    # under the flat source key for that slug to avoid double-prefixing.
    for k, (s, prefix) in SOURCE_TO_BROKERAGE.items():
        if s == slug and prefix == "":
            return k
    return slug


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--sources", default="cbre,jll",
                    help="comma-separated source keys / brokerage slugs (default cbre,jll)")
    ap.add_argument("--limit", type=int, default=100,
                    help="max candidate listings this run (default 100)")
    ap.add_argument("--api-url", default=DEFAULT_API_URL,
                    help="local Firecrawl API base (default %(default)s)")
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument("--apply", action="store_true",
                    help="rejected: Firecrawl OM writes are retired")
    ap.add_argument("--dry-run", action="store_true", default=True,
                    help="default; select+parse+extract+write artifact, do NOT ingest")
    ap.add_argument("--show-sql", action="store_true",
                    help="with --dry-run, print the candidate SQL and exit without connecting")
    args = ap.parse_args()
    sys.exit(run(args))


if __name__ == "__main__":
    main()
