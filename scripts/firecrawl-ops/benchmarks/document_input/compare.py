"""Offline comparisons of verified native_pdf reports and source-review sidecars.

No inference, credentials, original PDF or model output is read by this command.
Only selected references are scored; no result is a complete accuracy measure.
"""

from __future__ import annotations

import argparse
import collections
import itertools
import json
import math
import statistics
from pathlib import Path

import runner

DECISIONS = {
    "clean_correct",
    "missing",
    "wrong_association",
    "wrong_value",
    "unscorable",
    "uncertain_reference",
    "correct_with_conflict",
    "wrong_unspecified",
    "uncertain_output",
}
ALIASES = {
    "wrong": "wrong_unspecified",
    "uncertain": "uncertain_output",
    "incorrect_association": "wrong_association",
    "incorrect_value": "wrong_value",
    "unscorable_strict_format": "unscorable",
}


def numeric(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and value >= 0
    )


def percentiles(values):
    if not values:
        return {"median": None, "p90_nearest_rank": None, "p90_interpolated": None}
    values = sorted(values)
    position = (len(values) - 1) * 0.9
    lo, hi = math.floor(position), math.ceil(position)
    return {
        "median": statistics.median(values),
        "p90_nearest_rank": values[math.ceil(len(values) * 0.9) - 1],
        "p90_interpolated": values[lo] + (values[hi] - values[lo]) * (position - lo),
    }


def generate(reports, reviews=(), *, gold_raw=None):
    """Reports are JSON objects; manual decisions bind to exact result/PDF hashes."""
    if not reports:
        raise ValueError("at least one report required")
    manifest, prompt = (
        reports[0].get("manifest_sha256"),
        reports[0].get("prompt_sha256"),
    )
    if not all(isinstance(v, str) and len(v) == 64 for v in (manifest, prompt)):
        raise ValueError("source manifest and prompt hashes required")
    expected, source_hashes = {}, {}
    gold_hash = runner.digest(gold_raw) if gold_raw is not None else None
    if reviews and gold_raw is None:
        raise ValueError("frozen gold required to verify review completeness")
    if gold_raw is not None:
        gold = json.loads(gold_raw)
        if (
            gold.get("schema") != "document-input-adjudication-gold/v1"
            or gold.get("manifest_sha256") != manifest
        ):
            raise ValueError("gold manifest or schema mismatch")
        for document in gold["documents"]:
            doc = document["id"]
            if doc in expected:
                raise ValueError("duplicate gold document")
            roster = [("fact", f["id"]) for f in document.get("facts", [])]
            roster += [
                ("table_row", t["id"] + "/" + r["id"])
                for t in document.get("tables", [])
                for r in t["rows"]
            ]
            if len(roster) != len(set(roster)):
                raise ValueError("duplicate gold probe")
            expected[doc], source_hashes[doc] = set(roster), document["pdf_sha256"]
    groups, results, pdfs = {}, {}, {}
    for index, report in enumerate(reports):
        if report.get("schema") not in {
            "native-pdf-benchmark-report/v1",
            "native-pdf-legacy-inspection/v1",
        }:
            raise ValueError("verified native_pdf report required")
        if (
            report.get("manifest_sha256") != manifest
            or report.get("prompt_sha256") != prompt
        ):
            raise ValueError("reports use different manifest or prompt")
        for row in report["rows"]:
            required = (
                "document_id",
                "pdf_sha256",
                "model",
                "provider",
                "effort",
                "max_tokens",
            )
            if any(row.get(k) is None for k in required):
                raise ValueError("report row identity incomplete")
            doc = row["document_id"]
            if doc in pdfs and pdfs[doc] != row["pdf_sha256"]:
                raise ValueError("same document id has different PDF hashes")
            pdfs[doc] = row["pdf_sha256"]
            if gold_raw is not None and source_hashes.get(doc) != row["pdf_sha256"]:
                raise ValueError("gold PDF identity mismatch")
            key = (
                index,
                row["model"],
                row["provider"],
                row["effort"],
                row["max_tokens"],
            )
            bucket = groups.setdefault(key, {})
            if doc in bucket:
                raise ValueError("duplicate document in report route")
            bucket[doc] = row
            sha = row.get("result_sha256")
            if sha:
                if sha in results:
                    raise ValueError("duplicate result across reports")
                results[sha] = row
            for field in ("cost_usd", "elapsed_seconds"):
                if row.get(field) is not None and not numeric(row[field]):
                    raise ValueError("invalid numeric measurement")
    decisions = collections.defaultdict(list)
    seen, source_uncertainty = set(), {}
    for review in reviews:
        for decision in review["decisions"]:
            decision = dict(decision)
            for field in ("strict_decision", "semantic_decision"):
                decision[field] = ALIASES.get(decision.get(field), decision.get(field))
            sha = decision["result_sha256"]
            row = results.get(sha)
            if row is None:
                raise ValueError("review references an absent result")
            if (
                decision.get("document_id"),
                decision.get("model"),
                decision.get("requested_provider"),
                decision.get("source_pdf_sha256"),
            ) != (row["document_id"], row["model"], row["provider"], row["pdf_sha256"]):
                raise ValueError("review result/source identity mismatch")
            if (
                decision.get("kind") not in {"fact", "table_row"}
                or not decision.get("probe_id")
                or not decision.get("gold_sha256")
            ):
                raise ValueError("review probe/gold identity missing")
            if decision["gold_sha256"] != gold_hash or (
                decision["kind"],
                decision["probe_id"],
            ) not in expected.get(row["document_id"], set()):
                raise ValueError("review gold or probe mismatch")
            identity = sha, decision["kind"], decision["probe_id"]
            if identity in seen:
                raise ValueError("duplicate review decision")
            seen.add(identity)
            if any(
                decision.get(field) not in DECISIONS
                for field in ("strict_decision", "semantic_decision")
            ):
                raise ValueError("unknown review disposition")
            if (decision["strict_decision"] == "uncertain_reference") != (
                decision["semantic_decision"] == "uncertain_reference"
            ):
                raise ValueError(
                    "source uncertainty must be excluded consistently in both views"
                )
            source_key = decision["document_id"], decision["kind"], decision["probe_id"]
            uncertain = decision["semantic_decision"] == "uncertain_reference"
            if (
                source_key in source_uncertainty
                and source_uncertainty[source_key] != uncertain
            ):
                raise ValueError("source uncertainty differs across model/run reviews")
            source_uncertainty[source_key] = uncertain
            if (
                decision["strict_decision"] not in {"unscorable", "uncertain_reference"}
                and row.get("strict_usable") is not True
            ):
                raise ValueError("strict review contradicts unusable strict response")
            if decision["semantic_decision"] not in {
                "unscorable",
                "uncertain_reference",
            }:
                view = decision.get("semantic_view")
                allowed = (
                    view == "strict"
                    and row.get("strict_usable") is True
                    or view in {"exact_outer_fence_recovery", "fence_recovered"}
                    and row.get("usable_after_fence") is True
                    or view == "field_sensitivity"
                    and row.get("field_object_readable") is True
                    and row.get("field_object_view") == "strict_json_object"
                    or view == "field_sensitivity_fence"
                    and row.get("field_object_readable") is True
                    and row.get("field_object_view") == "outer_json_fence_object"
                )
                if not allowed:
                    raise ValueError(
                        "semantic review lacks a matching readable output view"
                    )
            decisions[sha].append(decision)
    golds = {d["gold_sha256"] for ds in decisions.values() for d in ds}
    if len(golds) > 1:
        raise ValueError("reviews use different gold references")
    summaries, pass_sets = [], []
    for group_id, (key, rows) in enumerate(groups.items()):
        costs = [r["cost_usd"] for r in rows.values() if r.get("cost_usd") is not None]
        timings = [
            r["elapsed_seconds"]
            for r in rows.values()
            if r.get("http_status") == 200 and r.get("elapsed_seconds") is not None
        ]
        reviewed = [d for r in rows.values() for d in decisions[r.get("result_sha256")]]
        pass_set, complete = set(), 0
        for row in rows.values():
            reviewed_probes = decisions[row.get("result_sha256")]
            covered = {(d["kind"], d["probe_id"]) for d in reviewed_probes}
            review_complete = bool(
                covered and covered == expected.get(row["document_id"])
            )
            complete += review_complete
            probes = [
                d
                for d in reviewed_probes
                if d["semantic_decision"] != "uncertain_reference"
            ]
            if (
                review_complete
                and probes
                and row.get("usable_after_fence") is True
                and all(d["semantic_decision"] == "clean_correct" for d in probes)
            ):
                pass_set.add(row["document_id"])
        passed = len(pass_set)
        pass_sets.append(pass_set)
        summary = {
            "group": group_id,
            "report_index": key[0],
            "model": key[1],
            "provider": key[2],
            "effort": key[3],
            "max_tokens": key[4],
            "documents": len(rows),
            "http_200": sum(r.get("http_status") == 200 for r in rows.values()),
            "strict_usable": sum(r.get("strict_usable") is True for r in rows.values()),
            "usable_after_fence": sum(
                r.get("usable_after_fence") is True for r in rows.values()
            ),
            "cost_known_count": len(costs),
            "cost_unknown_count": len(rows) - len(costs),
            "known_cost_sum_usd": sum(costs) if costs else None,
            "total_cost_usd": sum(costs) if len(costs) == len(rows) else None,
            "timed_http_200_count": len(timings),
            "api_seconds": percentiles(timings),
            "reviewed_documents": sum(
                bool(decisions[r.get("result_sha256")]) for r in rows.values()
            ),
            "complete_reference_review_documents": complete,
            "incomplete_reference_review_documents": len(rows) - complete,
            "format_and_selected_reference_pass_documents": passed,
            "cost_per_format_and_selected_reference_pass_usd": sum(costs) / passed
            if len(costs) == len(rows) and passed
            else None,
        }
        for view in ("strict_decision", "semantic_decision"):
            summary[view] = {}
            for kind in ("fact", "table_row"):
                counts = collections.Counter(
                    d[view] for d in reviewed if d["kind"] == kind
                )
                summary[view][kind] = {
                    "counts": dict(counts),
                    "reference_targets": sum(counts.values()),
                    "scorable_targets": sum(
                        v for k, v in counts.items() if k != "uncertain_reference"
                    ),
                }
        summaries.append(summary)
    paired = []
    for (ia, (_, a)), (ib, (_, b)) in itertools.combinations(
        enumerate(groups.items()), 2
    ):
        common = sorted(set(a) & set(b))
        cost = [
            doc
            for doc in common
            if all(rows[doc].get("cost_usd") is not None for rows in (a, b))
        ]
        speed = [
            doc
            for doc in common
            if all(
                rows[doc].get("http_status") == 200
                and rows[doc].get("elapsed_seconds") is not None
                for rows in (a, b)
            )
        ]
        eligible = sorted(pass_sets[ia] & pass_sets[ib])
        eligible_cost = [d for d in eligible if d in cost]
        eligible_speed = [d for d in eligible if d in speed]
        paired.append(
            {
                "a": ia,
                "b": ib,
                "common_documents": len(common),
                "paired_known_cost_count": len(cost),
                "a_cost_usd": sum(a[d]["cost_usd"] for d in cost) if cost else None,
                "b_cost_usd": sum(b[d]["cost_usd"] for d in cost) if cost else None,
                "b_cheaper_count": sum(
                    b[d]["cost_usd"] < a[d]["cost_usd"] for d in cost
                ),
                "paired_http_200_timing_count": len(speed),
                "all_http_200_b_faster_count": sum(
                    b[d]["elapsed_seconds"] < a[d]["elapsed_seconds"] for d in speed
                ),
                "both_format_and_complete_selected_reference_pass_documents": len(
                    eligible
                ),
                "passing_paired_known_cost_count": len(eligible_cost),
                "passing_b_cheaper_count": sum(
                    b[d]["cost_usd"] < a[d]["cost_usd"] for d in eligible_cost
                ),
                "passing_paired_timing_count": len(eligible_speed),
                "passing_b_faster_count": sum(
                    b[d]["elapsed_seconds"] < a[d]["elapsed_seconds"]
                    for d in eligible_speed
                ),
            }
        )
    return {
        "schema": "native-pdf-comparison/v1",
        "manifest_sha256": manifest,
        "prompt_sha256": prompt,
        "gold_sha256": gold_hash,
        "summary": summaries,
        "paired": paired,
        "limitations": "Selected agent-reviewed references only, not complete fact accuracy or text recall. Unknown cost is not zero. Reasoning is requested, not proven effective compute. Sequential runs are not a randomized causal effort comparison.",
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", action="append", type=Path, required=True)
    parser.add_argument("--review", action="append", type=Path, default=[])
    parser.add_argument(
        "--gold",
        type=Path,
        help="exact frozen reviewed-association gold, required with reviews",
    )
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)
    result = generate(
        [json.loads(p.read_bytes()) for p in args.report],
        [json.loads(p.read_bytes()) for p in args.review],
        gold_raw=args.gold.read_bytes() if args.gold else None,
    )
    result["input_report_sha256"] = [runner.digest(p.read_bytes()) for p in args.report]
    result["input_review_sha256"] = [runner.digest(p.read_bytes()) for p in args.review]
    if args.out:
        runner.write_new(args.out, runner.canonical(result))
    else:
        print(json.dumps(result))


if __name__ == "__main__":
    main()
