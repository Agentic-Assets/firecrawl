#!/usr/bin/env python3
"""Phase-2 board-impact derivation (robust, deterministic, read-only).

For each source, pick the fullest available artifact, group listings by
(sourceKey, external_id) exactly as the ingestor does (via to_row), compute
norm_status with terminal-wins across the sale+lease group, and bucket each
group into terminal (sold/leased/off_market) / under_contract / pending / null.
Writes tasks/tmp/phase2_artifact_buckets.json for the memo phase.

Processes one artifact at a time (memory-safe) and tolerates per-listing errors.
"""
import json
import os
import sys
import traceback
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.abspath(os.path.join(HERE, "..", "..", "scripts", "firecrawl-ops", "cre_collector"))
sys.path.insert(0, COLLECTOR)
OUT = os.path.join(COLLECTOR, "out")

from cre_ingest import to_row, norm_status, STATUS_SOURCE_PATHS  # noqa: E402

SCRAPED_AT = "2026-06-13T00:00:00Z"
TERMINAL = {"sold", "leased", "off_market"}

# Curated full-run artifacts (best coverage per source). The all-source
# full_latest catches cbre / savills / any source not in a dedicated file.
ARTIFACTS = [
    "colliers_main_full_2026-06-13.json",
    "cushman_full_2026-06-12_022841.json",
    "full_latest_2026-06-11_230423.json",
    "jll_full_detail_enriched_2026-06-12.json",
    "newmark_full_refined_2026-06-12.json",
    "marcus_full_2026-06-12_130035.json",
    "avison_full_detail_2026-06-12.json",
    "cbre_dealflow_full_2026-06-12_041740.json",
    "transwestern_full_2026-06-12_121302_cleaned.json",
    "lee_full_cache_2026-06-12_assembled.json",
    "jll_investor_full_sitemap_detail_2026-06-12.json",
    "svn_full_cache_2026-06-12_assembled.json",
    "colliers_salestracker_full_2026-06-12_050241.json",
    "nai_active_only_from_full_2026-06-12_044310.json",
]


def bucket_artifact(path):
    """Return {sourceKey: {counts, samples}} for one artifact."""
    with open(path) as f:
        data = json.load(f)
    listings = data.get("listings") or []
    # group flat listings by (sourceKey, external_id)
    groups = defaultdict(list)  # (sk, ext) -> [flat listings]
    for lst in listings:
        try:
            row = to_row(lst, {}, SCRAPED_AT)
        except Exception:
            row = None
        if row is None:
            continue
        sk = lst.get("sourceKey") or "<none>"
        groups[(sk, row["external_id"])].append(lst)

    per_source = defaultdict(lambda: {
        "total": 0, "sold": 0, "leased": 0, "off_market": 0,
        "under_contract": 0, "pending": 0, "null_status": 0, "samples": [],
    })
    for (sk, _ext), flats in groups.items():
        # terminal-wins across the group's flat listings
        status = None
        for fl in flats:
            try:
                s = norm_status(fl)
            except Exception:
                s = None
            if s in TERMINAL or s in ("under_contract", "pending"):
                if s in TERMINAL:
                    status = s
                    break
                if status is None:
                    status = s
            elif s and status is None:
                status = s
        d = per_source[sk]
        d["total"] += 1
        key = status if status else "null_status"
        d[key] = d.get(key, 0) + 1
        if status and len(d["samples"]) < 4:
            title = (flats[0].get("name") or flats[0].get("headline")
                     or flats[0].get("street") or "")[:70]
            d["samples"].append(f"{status} :: {title}")
    return per_source


def main():
    # for each sourceKey, keep the artifact yielding the most groups
    best = {}  # sk -> (total, artifact, bucketdict)
    used = {}
    for name in ARTIFACTS:
        path = os.path.join(OUT, name)
        if not os.path.exists(path):
            print(f"  (skip missing) {name}", file=sys.stderr)
            continue
        try:
            per_source = bucket_artifact(path)
        except Exception:
            print(f"  (ERROR processing {name})", file=sys.stderr)
            traceback.print_exc()
            continue
        for sk, d in per_source.items():
            if sk not in best or d["total"] > best[sk][0]:
                best[sk] = (d["total"], name, d)
        print(f"  processed {name}: "
              + ", ".join(f"{sk}={d['total']}" for sk, d in sorted(per_source.items())),
              file=sys.stderr)

    result = {}
    for sk, (total, name, d) in best.items():
        terminal = d["sold"] + d["leased"] + d["off_market"]
        result[sk] = {
            "artifact": name,
            "total_rows": d["total"],
            "status_tier": ("disappearance-only-null"
                            if not STATUS_SOURCE_PATHS.get(sk) else "has-native-status"),
            "sold": d["sold"], "leased": d["leased"], "off_market": d["off_market"],
            "terminal": terminal,
            "under_contract": d["under_contract"], "pending": d["pending"],
            "null_status": d["null_status"],
            "samples": d["samples"],
        }

    out_path = os.path.join(HERE, "phase2_artifact_buckets.json")
    with open(out_path, "w") as f:
        json.dump(result, f, indent=2, sort_keys=True)

    # human summary
    print("\n" + "=" * 92)
    print(f"{'source':<20}{'tier':<26}{'total':>7}{'term':>6}{'uc':>5}{'pend':>5}{'null':>7}")
    print("=" * 92)
    grand = defaultdict(int)
    for sk in sorted(result, key=lambda k: -result[k]["total_rows"]):
        r = result[sk]
        print(f"{sk:<20}{r['status_tier']:<26}{r['total_rows']:>7}{r['terminal']:>6}"
              f"{r['under_contract']:>5}{r['pending']:>5}{r['null_status']:>7}")
        for k in ("total_rows", "terminal", "under_contract", "pending", "null_status"):
            grand[k] += r[k]
    print("-" * 92)
    print(f"{'TOTAL':<20}{'':<26}{grand['total_rows']:>7}{grand['terminal']:>6}"
          f"{grand['under_contract']:>5}{grand['pending']:>5}{grand['null_status']:>7}")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
