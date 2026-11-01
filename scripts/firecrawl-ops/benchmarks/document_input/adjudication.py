"""Conservative, partial-gold association checks for private benchmark results.

Gold is an independent, source-reviewed sidecar bound to the frozen manifest hash.
Matches are exact after case/whitespace normalization; they are lower-bound probes,
not a measure of overall precision or complete document recall.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
import limits_and_budgets as limits


def norm(value):
    return " ".join(value.casefold().split()) if isinstance(value, str) else None


def aliases(value):
    if not isinstance(value, list) or not value or any(not isinstance(x, str) or not x.strip() for x in value):
        return None
    return {norm(x) for x in value}


def position_ok(expected, actual):
    return expected is None or (isinstance(expected, int) and not isinstance(expected, bool) and expected > 0 and actual == expected and not isinstance(actual, bool))


def judge_fact(gold, facts):
    labels = aliases(gold.get("label_aliases"))
    values = aliases(gold.get("value_aliases"))
    units = aliases(gold.get("unit_aliases")) if "unit_aliases" in gold else None
    if not labels or not values or ("unit_aliases" in gold and not units):
        return "unscorable"
    if not isinstance(facts, list) or any(not isinstance(f, dict) for f in facts):
        return "unknown"
    wrong = False
    uncertain = False
    for fact in facts:
        label, value = norm(fact.get("label")), norm(fact.get("value"))
        if label is None or value is None:
            uncertain = True
            continue
        # Require a complete value and unit rendering; never match a numeric substring.
        valid_values = values if units is None else {v + sep + u for v in values for u in units for sep in ("", " ")}
        if value not in valid_values:
            continue
        if label not in labels:
            wrong = True
        elif gold.get("page") is not None and fact.get("page") is None:
            uncertain = True
        elif position_ok(gold.get("page"), fact.get("page")):
            return "matched"
        else:
            wrong = True
    return "wrong_association_candidate" if wrong else "unknown" if uncertain else "missed"


def norm_row(row):
    if not isinstance(row, list) or not row or any(not isinstance(cell, str) for cell in row):
        return None
    return tuple(norm(cell) for cell in row)


def judge_table_row(gold_table, gold_row, tables):
    titles = aliases(gold_table.get("title_aliases"))
    headers = gold_table.get("header_aliases")
    row = norm_row(gold_row.get("cells"))
    if not titles or not isinstance(headers, list) or not headers or not row:
        return "unscorable"
    expected_headers = [aliases(cell) for cell in headers]
    if any(not a for a in expected_headers) or len(expected_headers) != len(row):
        return "unscorable"
    if not isinstance(tables, list) or any(not isinstance(t, dict) for t in tables):
        return "unknown"
    wrong = False
    uncertain = False
    for table in tables:
        raw_rows = table.get("rows")
        if not isinstance(raw_rows, list):
            uncertain = True
            continue
        rows = [norm_row(r) for r in raw_rows]
        if any(r is None for r in rows):
            uncertain = True
        if row not in rows:
            continue
        title = norm(table.get("title"))
        row_index = rows.index(row)
        has_header = any(r is not None and len(r) == len(expected_headers) and all(c in a for c, a in zip(r, expected_headers)) for r in rows[:row_index])
        if title is None or (gold_table.get("page") is not None and table.get("page") is None):
            uncertain = True
        elif title in titles and has_header and position_ok(gold_table.get("page"), table.get("page")):
            return "matched"
        else:
            wrong = True
    return "wrong_association_candidate" if wrong else "unknown" if uncertain else "missed"


def adjudicate(gold, result):
    """Return identifiers and dispositions only; never echo input values."""
    facts = gold.get("facts", [])
    tables = gold.get("tables", [])
    if not isinstance(facts, list) or not isinstance(tables, list):
        raise TypeError("invalid gold arrays")
    parsed = result.get("parsed") if result.get("status") == "completed" else None
    outcomes = []
    for fact in facts:
        if not isinstance(fact, dict) or not isinstance(fact.get("id"), str):
            raise TypeError("invalid fact probe")
        disposition = judge_fact(fact, parsed.get("facts") if isinstance(parsed, dict) else None)
        outcomes.append({"kind": "fact", "id": fact["id"], "disposition": disposition})
    for table in tables:
        if not isinstance(table, dict) or not isinstance(table.get("id"), str) or not isinstance(table.get("rows"), list):
            raise TypeError("invalid table probe")
        for row in table["rows"]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str):
                raise TypeError("invalid row probe")
            disposition = judge_table_row(table, row, parsed.get("tables") if isinstance(parsed, dict) else None)
            outcomes.append({"kind": "table_row", "id": table["id"] + "/" + row["id"], "disposition": disposition})
    return outcomes


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--gold", required=True, type=Path)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    args = parser.parse_args()
    manifest_raw = args.manifest.read_bytes()
    manifest = json.loads(manifest_raw)
    gold_raw = args.gold.read_bytes()
    gold = json.loads(gold_raw)
    manifest_hash = hashlib.sha256(manifest_raw).hexdigest()
    if gold.get("schema") != "document-input-adjudication-gold/v1" or gold.get("manifest_sha256") != manifest_hash:
        raise ValueError("gold is not bound to manifest")
    docs = {d["id"]: d["sha256"] for d in manifest["documents"]}
    probes = gold.get("documents")
    if not isinstance(probes, list) or len({d["id"] for d in probes}) != len(probes):
        raise ValueError("invalid gold documents")
    rows = []
    models = ("anthropic/claude-haiku-5.5", "openai/gpt-6.1-terra", "alibaba/qwen3.8-flash")
    for doc in probes:
        if not re.fullmatch(rf"[a-z0-9][a-z0-9_-]{{0,{limits.DOCUMENT_BENCH_MAX_ID_CHARS - 1}}}", doc["id"]) or docs.get(doc["id"]) != doc.get("pdf_sha256"):
            raise ValueError("gold document binding mismatch")
        for index, model in enumerate(models):
            for arm in ("anydoc_text", "native_pdf"):
                path = args.results / f"{doc['id']}--{index}--{arm}.result.json"
                raw_result = path.read_bytes() if path.exists() else None
                result = json.loads(raw_result) if raw_result is not None else {"status": "not_run"}
                if raw_result is not None and (result.get("document_id") != doc["id"] or result.get("pdf_sha256") != doc["pdf_sha256"] or result.get("model") != model or result.get("arm") != arm):
                    raise ValueError("result identity mismatch")
                rows.append({"document_id": doc["id"], "model": model, "arm": arm,
                             "result_sha256": hashlib.sha256(raw_result).hexdigest() if raw_result is not None else None,
                             "probes": adjudicate(doc, result)})
    counts = Counter(p["disposition"] for row in rows for p in row["probes"])
    report = {"schema": "document-input-adjudication/v1", "manifest_sha256": manifest_hash,
              "gold_sha256": hashlib.sha256(gold_raw).hexdigest(), "counts": dict(counts), "rows": rows,
              "limitation": "Partial reviewed probes and exact-match lower bounds; not overall precision or complete recall."}
    fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, sort_keys=True)
    print(json.dumps({"result_rows": len(rows), "counts": dict(counts)}))


if __name__ == "__main__":
    main()
