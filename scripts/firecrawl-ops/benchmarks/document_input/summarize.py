"""Export body-free, paired benchmark measurements from immutable private records."""
from __future__ import annotations

import argparse
import collections
import csv
import io
import json
import statistics
from pathlib import Path

import adjudication
import postprocess
import runner


def recall(reference, actual):
    expected = collections.Counter(runner.tokens(reference))
    found = collections.Counter(runner.tokens(actual))
    return sum((expected & found).values()) / sum(expected.values()) if expected else None


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    position = (len(values) - 1) * fraction
    lo = int(position)
    hi = min(lo + 1, len(values) - 1)
    return values[lo] + (values[hi] - values[lo]) * (position - lo)


def generate(manifest_path, gold_path, results, expected_gold_sha256):
    manifest, manifest_hash = runner.load_manifest(manifest_path)
    gold_raw = gold_path.read_bytes()
    gold = json.loads(gold_raw)
    if runner.digest(gold_raw) != expected_gold_sha256:
        raise ValueError("reviewed blind gold hash changed")
    if gold.get("schema") != "document-input-adjudication-gold/v1" or gold.get("manifest_sha256") != manifest_hash:
        raise ValueError("reviewed gold manifest binding changed")
    golden = {d["id"]: d for d in gold["documents"]}
    if len(golden) != len(gold["documents"]) or set(golden) != {d["id"] for d in manifest["documents"]}:
        raise ValueError("reviewed gold must cover the exact document set once")
    seal = json.loads((results / "preparation.json").read_bytes())
    if json.loads((results / "run.json").read_bytes())["manifest_sha256"] != manifest_hash:
        raise ValueError("run manifest changed")
    rows = []
    for document in manifest["documents"]:
        if golden[document["id"]]["pdf_sha256"] != document["sha256"]:
            raise ValueError("reviewed gold PDF binding changed")
        input_bytes = (results / f"{document['id']}.input.json").read_bytes()
        if runner.digest(input_bytes) != seal.get(document["id"]):
            raise ValueError("prepared reference changed")
        inputs = json.loads(input_bytes)
        if inputs.get("pdf_sha256") != document["sha256"]:
            raise ValueError("prepared PDF identity changed")
        reference = inputs.get("pdftotext", {}).get("text", "")
        anydoc = inputs["anydoc"]
        for index, model in enumerate(runner.MODELS):
            for arm in runner.ARMS:
                path = results / f"{document['id']}--{index}--{arm}.result.json"
                raw = path.read_bytes()
                original = json.loads(raw)
                if (original.get("document_id"), original.get("pdf_sha256"), original.get("model"), original.get("arm")) != (document["id"], document["sha256"], model, arm):
                    raise ValueError("model result identity changed")
                if original.get("preparation_sha256") != seal[document["id"]]:
                    raise ValueError("model result reference binding changed")
                intent = json.loads(path.with_name(path.name.replace(".result.json", ".intent.json")).read_bytes())
                dispatched = intent.get("dispatch") == "started"
                derived = postprocess.derive(raw)
                usable = bool(derived["normalized_valid"] and original.get("finish_reason") == "stop" and not original.get("truncated") and original.get("http_status") == 200)
                parsed = derived["normalized_content"] if usable else None
                # This is an explicit sensitivity analysis. Never mutate the original
                # strict-format outcome or treat a token-limited answer as complete.
                evaluated = {**original, "status": "completed" if usable else original["status"], "parsed": parsed}
                judgements = adjudication.adjudicate(golden[document["id"]], evaluated)
                fact_counts = collections.Counter(p["disposition"] for p in judgements if p["kind"] == "fact")
                table_counts = collections.Counter(p["disposition"] for p in judgements if p["kind"] == "table_row")
                diagnostic = runner.score(document, evaluated, inputs, manifest_path) if usable else None
                usage = original.get("usage") or {}
                cost = runner.reconciled_cost(path, original)
                row = {
                    "document_id": document["id"], "pdf_sha256": document["sha256"],
                    "pages": document.get("pages"), "model": model, "arm": arm,
                    "original_status": original["status"], "http_status": original.get("http_status"),
                    "api_dispatched": dispatched,
                    "strict_json_completed": original["status"] == "completed",
                    "usable_after_fence_normalization": usable,
                    "outer_json_fence_removed": derived["removed_outer_json_fence"],
                    "truncated": original.get("truncated", False),
                    "finish_reason": original.get("finish_reason"),
                    "elapsed_seconds": original.get("elapsed_seconds"),
                    "end_to_end_seconds": original.get("end_to_end_seconds"),
                    "actual_gateway_cost_usd": cost,
                    "prompt_tokens": usage.get("prompt_tokens"),
                    "completion_tokens": usage.get("completion_tokens"),
                    "reasoning_tokens": (usage.get("completion_tokens_details") or {}).get("reasoning_tokens"),
                    "anydoc_status": anydoc["status"], "anydoc_seconds": anydoc["elapsed_seconds"],
                    "anydoc_token_recall_proxy": recall(reference, anydoc.get("text", "")) if anydoc["status"] == "extracted" else None,
                    "transcription_token_recall_proxy": recall(reference, parsed["transcription"]) if usable else None,
                    "reference_tokens": len(runner.tokens(reference)),
                    "reviewed_fact_probes": len(golden[document["id"]].get("facts", [])),
                    "fact_matched": fact_counts["matched"], "fact_missed": fact_counts["missed"],
                    "fact_wrong_association_candidate": fact_counts["wrong_association_candidate"],
                    "fact_unknown": fact_counts["unknown"], "fact_unscorable": fact_counts["unscorable"],
                    "table_matched": table_counts["matched"], "table_missed": table_counts["missed"],
                    "table_wrong_association_candidate": table_counts["wrong_association_candidate"],
                    "table_unknown": table_counts["unknown"], "table_unscorable": table_counts["unscorable"],
                    "table_exact_row_proxy_hits": diagnostic.get("table_exact_row_hits") if diagnostic else None,
                    "table_exact_row_proxy_denominator": diagnostic.get("table_gold_rows") if diagnostic else None,
                    "original_result_sha256": runner.digest(raw),
                }
                rows.append(row)
    summaries = []
    for model in runner.MODELS:
        for arm in runner.ARMS:
            group = [r for r in rows if r["model"] == model and r["arm"] == arm]
            measured = [r for r in group if r["http_status"] == 200]
            usable = [r for r in group if r["usable_after_fence_normalization"]]
            latencies = [r["elapsed_seconds"] for r in measured if r["elapsed_seconds"] is not None]
            dispatched = [r for r in group if r["api_dispatched"]]
            costs = [r["actual_gateway_cost_usd"] for r in dispatched if r["actual_gateway_cost_usd"] is not None]
            overlaps = [r["transcription_token_recall_proxy"] for r in usable if r["transcription_token_recall_proxy"] is not None]
            summary = {
                "model": model, "arm": arm, "planned_documents": len(group),
                "http_200": len(measured), "usable_after_fence_normalization": len(usable),
                "strict_json_completed": sum(r["strict_json_completed"] for r in group),
                "fence_removed": sum(r["outer_json_fence_removed"] for r in group),
                "truncated": sum(r["truncated"] for r in group),
                "status_counts": dict(collections.Counter(r["original_status"] for r in group)),
                "actual_gateway_cost_usd_known": sum(costs) if costs else None,
                "api_dispatched": len(dispatched), "measured_cost_records": len(costs),
                "cost_unknown_dispatched": len(dispatched) - len(costs),
                "median_request_seconds": statistics.median(latencies) if latencies else None,
                "p95_request_seconds": percentile(latencies, .95),
                "median_token_recall_proxy_usable": statistics.median(overlaps) if overlaps else None,
            }
            for name in ("reviewed_fact_probes", "fact_matched", "fact_missed", "fact_wrong_association_candidate", "fact_unknown", "fact_unscorable", "table_matched", "table_missed", "table_wrong_association_candidate", "table_unknown", "table_unscorable"):
                summary[name] = sum(r[name] for r in group)
            summaries.append(summary)
    return {"schema": "document-input-public-measurements/v1", "manifest_sha256": manifest_hash,
            "reviewed_gold_sha256": runner.digest(gold_raw), "planned_cells": len(rows),
            "method": "Strict outcomes retained; one outer JSON fence may be removed for separately identified content scoring. Partial exact association probes are lower bounds; Poppler overlap is not text completeness.",
            "summary": summaries, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--expected-gold-sha256", required=True)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    result = generate(args.manifest, args.gold, args.results, args.expected_gold_sha256)
    args.out.mkdir(parents=True, exist_ok=True)
    runner.write_new(args.out / "measurements.json", runner.canonical(result))
    csv_out = io.StringIO()
    writer = csv.DictWriter(csv_out, fieldnames=list(result["rows"][0]))
    writer.writeheader()
    writer.writerows(result["rows"])
    runner.write_new(args.out / "measurements.csv", csv_out.getvalue().encode())
    print(json.dumps({"planned_cells": result["planned_cells"], "summary": result["summary"]}))


if __name__ == "__main__":
    main()
