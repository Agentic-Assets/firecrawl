#!/usr/bin/env python3
"""Empirical validation of norm_status against real collector artifacts.

Read-only. Imports the production norm_status/_canonical_key from cre_ingest and
runs them over real out/ artifacts, reporting per-source status distribution and
sample non-None hits. This is Phase-1 validation: it proves the STATUS_SOURCE_PATHS
resolve against live data and shows exactly what populating cre_listings.status
would change, WITHOUT touching the DB or the board.
"""
import json
import os
import sys
from collections import Counter, defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.join(HERE, "..", "..", "scripts", "firecrawl-ops", "cre_collector")
sys.path.insert(0, os.path.abspath(COLLECTOR))

from cre_ingest import norm_status, _canonical_key, STATUS_SOURCE_PATHS  # noqa: E402

OUT = os.path.abspath(os.path.join(COLLECTOR, "out"))

# Representative artifacts: prefer the most complete per-source full run.
ARTIFACTS = [
    "jll_investor_full_sitemap_detail_2026-06-12.json",
    "cbre_dealflow_full_2026-06-12_041740.json",
    "cushman_full_2026-06-12_022841.json",
    "colliers_salestracker_full_2026-06-12_050241.json",
    "colliers_main_full_2026-06-13.json",
    "avison_full_detail_2026-06-12.json",
    "jll_full_detail_enriched_2026-06-12.json",
    "full_latest_2026-06-11_230423.json",  # broad all-source: svn, lee, nai, etc.
]


def main():
    by_source_status = defaultdict(Counter)
    by_source_total = Counter()
    samples = defaultdict(list)
    canon_have = Counter()
    canon_total = Counter()

    for name in ARTIFACTS:
        path = os.path.join(OUT, name)
        if not os.path.exists(path):
            print(f"  (skip, missing) {name}", file=sys.stderr)
            continue
        with open(path) as f:
            data = json.load(f)
        listings = data.get("listings") or []
        for lst in listings:
            sk = lst.get("sourceKey") or "<none>"
            st = norm_status(lst)
            by_source_total[sk] += 1
            by_source_status[sk][st if st is not None else "NULL"] += 1
            canon_total[sk] += 1
            if _canonical_key(lst):
                canon_have[sk] += 1
            if st is not None and len(samples[sk]) < 3:
                samples[sk].append((st, (lst.get("title") or lst.get("name") or "")[:70]))

    print("=" * 78)
    print("norm_status distribution by source (real artifacts)")
    print("=" * 78)
    for sk in sorted(by_source_total, key=lambda k: -by_source_total[k]):
        total = by_source_total[sk]
        dist = by_source_status[sk]
        nonnull = total - dist.get("NULL", 0)
        has_paths = "paths" if STATUS_SOURCE_PATHS.get(sk) else "NO-paths"
        ck = canon_have[sk]
        print(f"\n[{sk}]  n={total}  status-bearing={nonnull}  "
              f"({has_paths})  canonical_key={ck}/{total}")
        for status, cnt in dist.most_common():
            print(f"    {status:>16} : {cnt}")
        for status, title in samples[sk]:
            print(f"      e.g. {status}: {title!r}")

    print("\n" + "=" * 78)
    print("SANITY CHECKS")
    print("=" * 78)
    # 1. No source ever produces 'active' (norm_status must never infer active).
    active_leak = {sk: c["active"] for sk, c in by_source_status.items() if c.get("active")}
    print(f"1. sources emitting 'active' (MUST be empty): {active_leak or 'OK none'}")
    # 2. Sources with empty paths + no text hits should be ~all NULL.
    nopath_nonnull = {
        sk: (by_source_total[sk] - by_source_status[sk].get("NULL", 0))
        for sk in by_source_total
        if not STATUS_SOURCE_PATHS.get(sk)
        and (by_source_total[sk] - by_source_status[sk].get("NULL", 0)) > 0
    }
    print(f"2. no-path sources with non-NULL (text-fallback hits only, expected small): {nopath_nonnull or 'none'}")
    # 3. All emitted statuses are in the widened DB CHECK set.
    allowed = {"sold", "under_contract", "pending", "leased", "off_market"}
    emitted = set()
    for c in by_source_status.values():
        emitted |= {k for k in c if k != "NULL"}
    bad = emitted - allowed
    print(f"3. emitted statuses outside widened CHECK (MUST be empty): {bad or 'OK none'}")
    print(f"   emitted status vocabulary: {sorted(emitted)}")


if __name__ == "__main__":
    main()
