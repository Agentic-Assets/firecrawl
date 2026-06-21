#!/usr/bin/env python3
"""Phase 2: measure what status activation WOULD assign, mirroring production semantics.

Read-only. Imports to_row, norm_status, STATUS_SOURCE_PATHS from cre_ingest.
Runs over real collector artifacts (freshest full-run per source).

Groups by (slug, external_id) via to_row() (production dedup key).
Applies TERMINAL-WINS across each group's flat listings:
  - call norm_status on each flat listing individually
  - if any yields a terminal status, use it
  - else use first non-None
NEVER calls norm_status on a merged/combined dict.

Outputs phase2_artifact_buckets.json to tasks/tmp/.
"""
import json
import os
import sys
from collections import defaultdict

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTOR = os.path.abspath(os.path.join(HERE, "..", "..", "scripts", "firecrawl-ops", "cre_collector"))
sys.path.insert(0, COLLECTOR)

from cre_ingest import to_row, norm_status, STATUS_SOURCE_PATHS  # noqa: E402

OUT = os.path.join(COLLECTOR, "out")
SCRAPED_AT = "2026-06-13T00:00:00Z"
TERMINAL_STATUSES = {"sold", "under_contract", "pending", "leased", "off_market"}
VALID_STATUSES = {"sold", "under_contract", "pending", "leased", "off_market"}

# Per-source freshest full-run artifact (some sources share an artifact and are split by sourceKey).
# artifact -> [sourceKey, ...] to process from that file
SOURCE_ARTIFACTS = {
    # Dedicated per-source full runs (freshest)
    "cbre_dealflow_full_2026-06-12_041740.json":         ["cbre-dealflow"],
    "jll_full_detail_enriched_2026-06-12.json":          ["jll"],
    "jll_investor_full_sitemap_detail_2026-06-12.json":  ["jll-investor"],
    "cushman_full_2026-06-12_022841.json":               ["cushman-wakefield"],
    "colliers_salestracker_full_2026-06-12_050241.json": ["colliers"],
    "colliers_main_full_2026-06-13.json":                ["colliers-main"],
    "newmark_full_refined_2026-06-12.json":              ["newmark"],
    "marcus_full_2026-06-12_130035.json":                ["marcus-millichap"],
    "avison_full_detail_2026-06-12.json":                ["avison-young"],
    "svn_full_cache_2026-06-12_assembled.json":          ["svn"],
    "nai_full_unbounded_2026-06-12_044310.json":         ["nai-global"],
    "lee_full_cache_2026-06-12_assembled.json":          ["lee-associates"],
    "transwestern_full_2026-06-12_121302_cleaned.json":  ["transwestern"],
    # Broad all-source artifact: extract cbre and savills rows (no fresher dedicated artifact)
    "full_latest_2026-06-11_230423.json":                ["cbre", "savills"],
}

# Explode to per-source lookup: sourceKey -> (artifact_name, [all_source_keys_in_file])
SOURCE_TO_ARTIFACT = {}
for art, keys in SOURCE_ARTIFACTS.items():
    for k in keys:
        SOURCE_TO_ARTIFACT[k] = art

ALL_SOURCE_KEYS = [
    "cbre", "cbre-dealflow", "jll", "jll-investor", "cushman-wakefield",
    "colliers", "colliers-main", "newmark", "marcus-millichap", "avison-young",
    "savills", "svn", "nai-global", "lee-associates", "transwestern",
]


def status_tier(source_key):
    paths = STATUS_SOURCE_PATHS.get(source_key)
    if paths is None:
        return "disappearance-only-null"  # not in map at all
    if paths:
        return "has-native-status"
    # Empty list = explicitly disappearance-only
    return "disappearance-only-null"


def process_source(source_key, artifact_name, all_listings_in_artifact):
    """
    From the listings slice belonging to source_key, group by (slug, external_id)
    via to_row(), then apply TERMINAL-WINS norm_status per group.
    Returns per-source result dict.
    """
    # Step 1: build groups
    # groups: (slug, external_id) -> [flat_listing, ...]
    groups = defaultdict(list)
    skipped = 0
    for lst in all_listings_in_artifact:
        if lst.get("sourceKey") != source_key:
            continue
        row = to_row(lst, {}, SCRAPED_AT)
        if row is None:
            skipped += 1
            continue
        key = (row["slug"], row["external_id"])
        groups[key].append(lst)

    # Step 2: TERMINAL-WINS status per group
    counts = {"sold": 0, "leased": 0, "off_market": 0, "under_contract": 0, "pending": 0, "null_status": 0}
    samples = []
    anomalies = []

    for (slug, ext_id), flat_listings in groups.items():
        # Call norm_status on each flat listing individually
        statuses = [norm_status(fl) for fl in flat_listings]

        # Terminal-wins: if any terminal status in group, use it; else first non-None
        final_status = None
        for s in statuses:
            if s in TERMINAL_STATUSES:
                final_status = s
                break
        if final_status is None:
            for s in statuses:
                if s is not None:
                    final_status = s
                    break

        # Sanity check: must not produce 'active'
        if final_status == "active":
            anomalies.append(f"ANOMALY: norm_status produced 'active' for {source_key} ext_id={ext_id}")

        # Check all statuses are in valid set
        for s in statuses:
            if s is not None and s not in VALID_STATUSES:
                anomalies.append(f"ANOMALY: invalid status '{s}' for {source_key} ext_id={ext_id}")

        if final_status is None:
            counts["null_status"] += 1
        elif final_status in counts:
            counts[final_status] += 1
        else:
            counts["null_status"] += 1
            anomalies.append(f"ANOMALY: unmapped status '{final_status}' for {source_key}")

        # Collect samples (up to 3 non-null)
        if final_status is not None and len(samples) < 3:
            title = ""
            for fl in flat_listings:
                title = fl.get("name") or fl.get("title") or fl.get("headline") or fl.get("street") or ""
                if title:
                    break
            samples.append(f"{final_status} :: {title[:80]}")

    terminal = counts["sold"] + counts["leased"] + counts["off_market"]
    total_groups = sum(counts.values())

    return {
        "source_key": source_key,
        "artifact": artifact_name,
        "total_rows": total_groups,
        "status_tier": status_tier(source_key),
        "terminal": terminal,
        "under_contract": counts["under_contract"],
        "pending": counts["pending"],
        "leased": counts["leased"],
        "sold": counts["sold"],
        "off_market": counts["off_market"],
        "null_status": counts["null_status"],
        "sample": samples,
        "_skipped_no_url": skipped,
        "_anomalies": anomalies,
    }


def main():
    # Load each artifact once, process all sources from it
    results_by_source = {}
    artifact_cache = {}  # artifact_name -> listings list

    for source_key in ALL_SOURCE_KEYS:
        artifact_name = SOURCE_TO_ARTIFACT.get(source_key)
        if not artifact_name:
            print(f"  [WARN] No artifact mapping for {source_key}", file=sys.stderr)
            continue

        path = os.path.join(OUT, artifact_name)
        if not os.path.exists(path):
            print(f"  [WARN] Missing artifact: {artifact_name}", file=sys.stderr)
            continue

        if artifact_name not in artifact_cache:
            print(f"  Loading {artifact_name} ({os.path.getsize(path) / 1e6:.1f} MB)...", file=sys.stderr)
            with open(path) as f:
                data = json.load(f)
            artifact_cache[artifact_name] = data.get("listings") or []
            print(f"    -> {len(artifact_cache[artifact_name])} raw listings", file=sys.stderr)

        listings = artifact_cache[artifact_name]
        result = process_source(source_key, artifact_name, listings)
        results_by_source[source_key] = result
        print(
            f"  {source_key:20s}: {result['total_rows']:5d} groups | "
            f"terminal={result['terminal']:4d} | under_contract={result['under_contract']:3d} | "
            f"pending={result['pending']:3d} | null={result['null_status']:5d} | "
            f"tier={result['status_tier']}",
            file=sys.stderr,
        )
        if result["_anomalies"]:
            for a in result["_anomalies"][:5]:
                print(f"    {a}", file=sys.stderr)

    # Sanity checks
    print("\n=== SANITY CHECKS ===", file=sys.stderr)
    active_leaks = []
    bad_statuses = set()
    for sk, r in results_by_source.items():
        for anom in r.get("_anomalies", []):
            if "produced 'active'" in anom:
                active_leaks.append(anom)
            if "invalid status" in anom:
                bad_statuses.add(anom)
    print(f"  norm_status producing 'active': {active_leaks or 'NONE (OK)'}", file=sys.stderr)
    print(f"  invalid statuses: {bad_statuses or 'NONE (OK)'}", file=sys.stderr)

    # Build output
    per_source = list(results_by_source.values())
    totals = {
        "total_groups": sum(r["total_rows"] for r in per_source),
        "terminal": sum(r["terminal"] for r in per_source),
        "under_contract": sum(r["under_contract"] for r in per_source),
        "pending": sum(r["pending"] for r in per_source),
        "leased": sum(r["leased"] for r in per_source),
        "sold": sum(r["sold"] for r in per_source),
        "off_market": sum(r["off_market"] for r in per_source),
        "null_status": sum(r["null_status"] for r in per_source),
        "sources_with_native_status": sum(1 for r in per_source if r["status_tier"] == "has-native-status"),
        "sources_disappearance_only": sum(1 for r in per_source if r["status_tier"] == "disappearance-only-null"),
    }

    output = {
        "generated_at": "2026-06-13T00:00:00Z",
        "scraped_at_used": SCRAPED_AT,
        "per_source": per_source,
        "totals": totals,
    }

    out_path = os.path.join(HERE, "phase2_artifact_buckets.json")
    with open(out_path, "w") as f:
        json.dump(output, f, indent=2)
    print(f"\nWrote {out_path} ({os.path.getsize(out_path) / 1e3:.1f} KB)", file=sys.stderr)

    # Print summary table
    print("\n=== PER-SOURCE STATUS DISTRIBUTION ===")
    print(f"{'source_key':<22} {'total':>6} {'terminal':>8} {'sold':>6} {'leased':>7} "
          f"{'off_mkt':>7} {'uc':>5} {'pend':>5} {'null':>6}  tier")
    print("-" * 100)
    for r in per_source:
        print(
            f"{r['source_key']:<22} {r['total_rows']:>6} {r['terminal']:>8} "
            f"{r['sold']:>6} {r['leased']:>7} {r['off_market']:>7} "
            f"{r['under_contract']:>5} {r['pending']:>5} {r['null_status']:>6}  {r['status_tier']}"
        )
    print("-" * 100)
    print(
        f"{'TOTALS':<22} {totals['total_groups']:>6} {totals['terminal']:>8} "
        f"{totals['sold']:>6} {totals['leased']:>7} {totals['off_market']:>7} "
        f"{totals['under_contract']:>5} {totals['pending']:>5} {totals['null_status']:>6}"
    )


if __name__ == "__main__":
    main()
