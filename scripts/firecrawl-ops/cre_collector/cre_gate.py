#!/usr/bin/env python3
"""cre_gate.py: per-source coverage-and-anomaly gate for the CRE monitor layer.

Design: cre-intelligence-system-design.md sections 8 and 9 (the
coverage-and-anomaly gate that protects live inventory from a gappy run).

What it does, per source_key in a collector run artifact:
  1. Count the flat listings that cre_ingest.to_row() ACCEPTS (non-None). This
     is current_active. The count REUSES to_row() so the enumeration key the
     gate reasons about is exactly the one the ingestor writes (the enumeration
     key invariant in test_enum_key_invariant.py). Ids are never re-derived here.
  2. Read each source's error state from data["sources"] (any pass carrying an
     'error' marks that source as having a failed pass).
  3. Compare current_active to the rolling baseline in
     credeals.cre_source_baseline (live read only) and emit a verdict:
       first_seen : no baseline row yet, cannot gate; mark_missing_safe=false.
       hold       : a failed pass, OR below the absolute floor, OR a drop below
                    median * (1 - drop_threshold). mark_missing_safe=false.
       ok         : healthy. mark_missing_safe=true.
  4. Emit a per-brokerage rollup. A brokerage is mark-missing-safe ONLY if every
     one of its gated source_keys is verdict 'ok'. Sub-sources (cbre-dealflow,
     jll-investor, colliers-main) are gated as their OWN source_key, never
     folded into the parent, because each owns its own baseline row.

This file is OBSERVE-ONLY for listings. It NEVER writes credeals.cre_listings
(no status, no deleted_at, nothing). The only table it can write is the neutral
credeals.cre_source_baseline, and only under --apply --update-baseline.

Stdlib only. The DB URL is never printed.

Usage:
  python3 cre_gate.py --in ./out/run.json --dry-run            # default; no DB
  python3 cre_gate.py --in ./out/run.json --apply              # live gate read
  python3 cre_gate.py --in ./out/run.json --apply --update-baseline
  python3 cre_gate.py --in ./out/run.json --strict             # nonzero if any hold
"""

import argparse
import json
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone

from cre_ingest import (
    SOURCE_KEYS_BY_SLUG,
    SOURCE_TO_BROKERAGE,
    find_psql,
    load_db_url,
    sql_lit,
    to_row,
)

DEFAULT_FLOOR = 100
DEFAULT_DROP_THRESHOLD = 0.30

UNMAPPED_SLUG = "__unmapped__"


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _eprint(quiet, *parts):
    if not quiet:
        print(*parts, file=sys.stderr)


def _slug_for(source_key):
    mapping = SOURCE_TO_BROKERAGE.get(source_key)
    return mapping[0] if mapping else UNMAPPED_SLUG


def rolling_median(old_median, last_active, current):
    """3-point rolling median that resists a single spike (design section 9).

    new_median = median([old_median or current, last_active or current, current]).
    When there is no prior baseline (old_median and last_active both absent),
    this collapses to current, which is the first-seen seed value.
    """
    a = old_median if old_median is not None else current
    b = last_active if last_active is not None else current
    return sorted([a, b, current])[1]


# ---------------------------------------------------------------------------
# Count step (no DB needed)
# ---------------------------------------------------------------------------


def count_artifacts(inputs, quiet):
    """Aggregate per-source counts and error state across one or more artifacts.

    Returns (current_active, source_error, observed_keys, scraped_at,
    torow_errors). current_active is per source_key, counting FLAT listings that
    to_row() accepts (matching cre_ingest per_source_counts, not deduped).
    source_error[sk] holds the first error text seen for that source, or None.
    """
    current_active = {}
    source_error = {}
    observed = set()
    scraped_at = None
    torow_errors = 0

    for path in inputs:
        with open(path) as f:
            data = json.load(f)
        run_meta = data.get("runMeta") or {}
        scraped_at = scraped_at or run_meta.get("finishedAt") or run_meta.get("startedAt")
        sa = run_meta.get("finishedAt") or datetime.now(timezone.utc).isoformat()
        brokers_by_idx = {i: b for i, b in enumerate(data.get("brokers") or [])}

        for entry in data.get("sources") or []:
            sk = entry.get("sourceKey")
            if not sk:
                continue
            observed.add(sk)
            err = entry.get("error")
            if err and not source_error.get(sk):
                source_error[sk] = str(err)[:200]
            elif sk not in source_error:
                source_error[sk] = None

        for listing in data.get("listings") or []:
            sk = listing.get("sourceKey")
            if sk:
                observed.add(sk)
            try:
                row = to_row(listing, brokers_by_idx, sa)
            except Exception:
                # A single malformed listing must not crash the gate.
                torow_errors += 1
                row = None
            if row is not None:
                current_active[sk] = current_active.get(sk, 0) + 1

    return current_active, source_error, observed, scraped_at, torow_errors


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------


def verdict_for(current, has_error, error_text, baseline_row, floor, drop_threshold):
    """Decide a verdict for one source_key.

    Precedence (matches design section 8/9 and the dry-run contract):
      1. No baseline row -> 'first_seen' (cannot gate, never mark-missing on
         first sight). This is checked FIRST so a dry run with an empty baseline
         yields 'first_seen' for every source, even one carrying an error.
      2. A failed pass -> 'hold'.
      3. current < floor -> 'hold'.
      4. current < median * (1 - drop_threshold) -> 'hold'.
      5. otherwise -> 'ok'.

    Returns (verdict, reason, mark_missing_safe, baseline_median).
    """
    if baseline_row is None:
        reason = "no baseline row; cannot gate (first sight)"
        if has_error:
            reason += "; a source pass also errored this run"
        return "first_seen", reason, False, None

    median = baseline_row.get("median")

    if has_error:
        return "hold", f"source pass error: {error_text}", False, median
    if current < floor:
        return "hold", f"current_active {current} below floor {floor}", False, median
    if median is not None and median > 0:
        threshold = median * (1.0 - drop_threshold)
        if current < threshold:
            keep_pct = int(round((1.0 - drop_threshold) * 100))
            return (
                "hold",
                f"current_active {current} below {keep_pct}% of baseline "
                f"median {median} (threshold {int(threshold)})",
                False,
                median,
            )
    return "ok", None, True, median


# ---------------------------------------------------------------------------
# Live baseline read / write (gated behind --apply; never in --dry-run)
# ---------------------------------------------------------------------------


def _psql_read(psql, db_url, sql):
    """Run a read-only query and return rows as lists of strings.

    Uses -tA (tuples only, unaligned) with a unit-separator field delimiter so
    ordinary text values do not collide with the separator. The DB URL is passed
    as an argv element and is never printed.
    """
    proc = subprocess.run(
        [psql, db_url, "-tAF", "\x1f", "-v", "ON_ERROR_STOP=1", "-c", sql],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        sys.exit(f"psql read failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}")
    rows = []
    for line in proc.stdout.splitlines():
        if line == "":
            continue
        rows.append(line.split("\x1f"))
    return rows


def read_baseline(env_file, quiet):
    """Read credeals.cre_source_baseline. Returns (baseline, db_url, psql)."""
    db_url, env_path = load_db_url(env_file)
    psql = find_psql()
    _eprint(quiet, f"baseline credentials: {env_path}")
    rows = _psql_read(
        psql,
        db_url,
        "SELECT source_key, median_active_rows, last_active_rows "
        "FROM credeals.cre_source_baseline;",
    )
    baseline = {}
    for r in rows:
        sk = r[0]
        median = int(r[1]) if len(r) > 1 and r[1] != "" else None
        last = int(r[2]) if len(r) > 2 and r[2] != "" else None
        baseline[sk] = {"median": median, "last": last}
    _eprint(quiet, f"baseline rows read: {len(baseline)}")
    return baseline, db_url, psql


def build_baseline_sql(updates, scraped_at, job_id):
    """Build an idempotent upsert into cre_source_baseline for the given rows.

    new_median is computed in Python (rolling_median) so each row carries its
    final value. last_accepted_job_id is COALESCEd so a run without a job id
    does not clobber a prior one.
    """
    sa_lit = f"{sql_lit(scraped_at)}::timestamptz" if scraped_at else "NULL"
    job_lit = f"{sql_lit(job_id)}::uuid" if job_id else "NULL"
    lines = ["\\set ON_ERROR_STOP on", "BEGIN;"]
    for u in updates:
        slug_lit = sql_lit(u["slug"]) if u["slug"] and u["slug"] != UNMAPPED_SLUG else "NULL"
        lines.append(
            "INSERT INTO credeals.cre_source_baseline "
            "(source_key, brokerage_slug, median_active_rows, last_active_rows, "
            "last_accepted_scraped_at, last_accepted_job_id, updated_at) VALUES ("
            f"{sql_lit(u['source_key'])}, {slug_lit}, "
            f"{int(u['new_median'])}, {int(u['current'])}, {sa_lit}, {job_lit}, now()) "
            "ON CONFLICT (source_key) DO UPDATE SET "
            "brokerage_slug = EXCLUDED.brokerage_slug, "
            "median_active_rows = EXCLUDED.median_active_rows, "
            "last_active_rows = EXCLUDED.last_active_rows, "
            "last_accepted_scraped_at = EXCLUDED.last_accepted_scraped_at, "
            "last_accepted_job_id = COALESCE(EXCLUDED.last_accepted_job_id, "
            "credeals.cre_source_baseline.last_accepted_job_id), "
            "updated_at = now();"
        )
    lines.append("COMMIT;")
    return "\n".join(lines)


def run_baseline_sql(psql, db_url, sql):
    """Execute the baseline upsert script. The DB URL is never printed."""
    fd, path = tempfile.mkstemp(prefix="cre_gate_baseline_", suffix=".sql")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(sql)
        proc = subprocess.run(
            [psql, db_url, "-q", "-v", "ON_ERROR_STOP=1", "-f", path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc.returncode != 0:
            sys.exit(f"baseline write failed (rc={proc.returncode}): {proc.stderr.strip()[:300]}")
    finally:
        os.unlink(path)


def select_baseline_updates(per_source, source_error, baseline):
    """Pick the rows to upsert on --update-baseline.

    'ok'         -> rolling-median update.
    'first_seen' -> seed a new row, but ONLY for a clean, non-empty pass (no
                    error and current_active > 0), so a failed or empty first
                    run never seeds a poisoned baseline.
    'hold'       -> never written.
    """
    updates = []
    for sk in sorted(per_source):
        info = per_source[sk]
        verdict = info["verdict"]
        current = info["current_active"]
        if verdict == "ok":
            br = baseline.get(sk) or {}
            new_median = rolling_median(br.get("median"), br.get("last"), current)
        elif verdict == "first_seen":
            if source_error.get(sk) or current <= 0:
                continue
            new_median = current
        else:
            continue
        updates.append(
            {
                "source_key": sk,
                "slug": _slug_for(sk),
                "new_median": new_median,
                "current": current,
            }
        )
    return updates


def rollup_brokerages(per_source):
    """Fold per-source-key verdicts into a per-brokerage mark-missing-safe rollup.

    A brokerage is mark-missing-safe only when (1) every OBSERVED member source_key
    is verdict 'ok', AND (2) every KNOWN folded source_key for that brokerage was
    actually observed this run. The second guard prevents a run that scraped only
    the parent (e.g. cbre but not cbre-dealflow) from looking "safe" while a whole
    sub-source's live inventory is simply absent. This mirrors cre_ingest's
    has_complete_folded_coverage so the gate's output is self-sufficient and never
    greenlights a soft-delete of an unobserved sub-source.
    """
    per_brokerage = {}
    for sk in sorted(per_source):
        slug = _slug_for(sk)
        pb = per_brokerage.setdefault(slug, {"mark_missing_safe": True, "source_keys": []})
        pb["source_keys"].append(sk)
        if not per_source[sk]["mark_missing_safe"]:
            pb["mark_missing_safe"] = False

    for slug, pb in per_brokerage.items():
        known = SOURCE_KEYS_BY_SLUG.get(slug, {slug})
        missing = known - set(pb["source_keys"])
        if missing:
            pb["mark_missing_safe"] = False
            pb["reason"] = f"incomplete folded coverage (missing {sorted(missing)})"
    return per_brokerage


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    ap.add_argument("--in", dest="inputs", action="append", required=True,
                    help="collector run artifact JSON (repeatable)")
    ap.add_argument("--env-file", default=None, help="env file holding POSTGRES_URL*")
    ap.add_argument("--dry-run", action="store_true",
                    help="default; compute verdicts with an empty baseline, no DB connection")
    ap.add_argument("--apply", action="store_true", help="live: read the baseline from the DB")
    ap.add_argument("--live", action="store_true", help="alias for --apply")
    ap.add_argument("--update-baseline", action="store_true",
                    help="upsert cre_source_baseline for ok/first_seen sources (requires --apply/--live)")
    ap.add_argument("--job-id", default=None,
                    help="optional cre_scrape_jobs uuid stamped as last_accepted_job_id")
    ap.add_argument("--floor", type=int, default=DEFAULT_FLOOR,
                    help=f"absolute minimum current_active to clear the gate (default {DEFAULT_FLOOR})")
    ap.add_argument("--drop-threshold", type=float, default=DEFAULT_DROP_THRESHOLD,
                    help=f"max fractional drop below baseline median before hold (default {DEFAULT_DROP_THRESHOLD})")
    ap.add_argument("--strict", action="store_true", help="exit nonzero if any source is 'hold'")
    ap.add_argument("--out", default=None, help="also write the JSON result here")
    ap.add_argument("--quiet", action="store_true", help="suppress informational stderr")
    args = ap.parse_args()

    quiet = args.quiet
    # --dry-run always wins: an explicit dry run never opens a DB connection.
    live = (args.apply or args.live) and not args.dry_run
    dry_run = not live

    for path in args.inputs:
        if not os.path.isfile(path):
            sys.exit(f"artifact not found: {path}")

    current_active, source_error, observed, scraped_at, torow_errors = count_artifacts(
        args.inputs, quiet
    )

    baseline = {}
    db_url = None
    psql = None
    if live:
        baseline, db_url, psql = read_baseline(args.env_file, quiet)
    else:
        _eprint(quiet, "dry-run: no DB connection; baseline treated as empty (every source first_seen)")

    # Per-source verdicts.
    per_source = {}
    for sk in sorted(observed):
        current = current_active.get(sk, 0)
        error_text = source_error.get(sk)
        has_error = bool(error_text)
        baseline_row = baseline.get(sk)  # always None in dry-run
        verdict, reason, mm_safe, median = verdict_for(
            current, has_error, error_text, baseline_row, args.floor, args.drop_threshold
        )
        per_source[sk] = {
            "current_active": current,
            "baseline_median": median,
            "verdict": verdict,
            "reason": reason,
            "mark_missing_safe": mm_safe,
        }

    # Per-brokerage rollup (coverage-aware; see rollup_brokerages).
    per_brokerage = rollup_brokerages(per_source)

    # Optional baseline update (live + explicit flag only).
    baseline_updated = False
    baseline_update_sources = []
    if args.update_baseline:
        if not live:
            _eprint(quiet, "--update-baseline requires --apply/--live; nothing written (dry-run)")
        else:
            updates = select_baseline_updates(per_source, source_error, baseline)
            if updates:
                run_baseline_sql(psql, db_url, build_baseline_sql(updates, scraped_at, args.job_id))
                baseline_updated = True
                baseline_update_sources = sorted(u["source_key"] for u in updates)
                _eprint(quiet, f"baseline upserted for: {baseline_update_sources}")
            else:
                _eprint(quiet, "baseline update: no ok/seedable sources this run")

    verdict_counts = {"ok": 0, "hold": 0, "first_seen": 0}
    for info in per_source.values():
        verdict_counts[info["verdict"]] = verdict_counts.get(info["verdict"], 0) + 1
    hold_sources = sorted(sk for sk, v in per_source.items() if v["verdict"] == "hold")
    safe_sources = sorted(sk for sk, v in per_source.items() if v["mark_missing_safe"])
    safe_brokerages = sorted(s for s, v in per_brokerage.items() if v["mark_missing_safe"])

    summary = {
        "mode": "live" if live else "dry_run",
        "floor": args.floor,
        "drop_threshold": args.drop_threshold,
        "baseline_rows_known": len(baseline),
        "total_sources": len(per_source),
        "verdict_counts": verdict_counts,
        "hold_sources": hold_sources,
        "mark_missing_safe_sources": safe_sources,
        "mark_missing_safe_brokerages": safe_brokerages,
        "torow_errors": torow_errors,
        "baseline_updated": baseline_updated,
        "baseline_update_sources": baseline_update_sources,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    result = {
        "generated_for_artifact": args.inputs[0] if len(args.inputs) == 1 else args.inputs,
        "per_source": per_source,
        "per_brokerage": per_brokerage,
        "summary": summary,
    }

    text = json.dumps(result, indent=2)
    print(text)
    if args.out:
        with open(args.out, "w") as f:
            f.write(text + "\n")
        _eprint(quiet, f"wrote {args.out}")

    _eprint(
        quiet,
        f"verdicts: ok={verdict_counts['ok']} hold={verdict_counts['hold']} "
        f"first_seen={verdict_counts['first_seen']} (mode={summary['mode']})",
    )

    # Advisory by default: exit 0. --strict turns any hold into a nonzero exit.
    if args.strict and hold_sources:
        sys.exit(2)


if __name__ == "__main__":
    main()
