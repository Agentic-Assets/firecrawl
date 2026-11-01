"""Finite, private, resumable document input comparison. No automatic retries."""
from __future__ import annotations

import argparse
import base64
import concurrent.futures
import fcntl
import hashlib
import json
import math
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

OPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(OPS))
import limits_and_budgets as limits

MODELS = ("anthropic/claude-haiku-5.5", "openai/gpt-6.1-terra", "alibaba/qwen3.8-flash")
ARMS = ("anydoc_text", "native_pdf")
BASE = "https://ai-gateway.vercel.sh/v1"
PROMPT = """Extract the entire document, not a summary. Return exactly one JSON object with keys transcription, facts, tables. transcription must include all legible source text, in reading order, without invented text. facts is an array of objects {page, label, value}; tables is an array of objects {page, title, rows}, where rows is an array of arrays of strings. Preserve units, signs, punctuation, and uncertainty. Use null for unknown page numbers and other unavailable values. Include all visible rows and columns. Do not infer missing values. If the document cannot be read fully, say so in transcription and retain what is legible."""


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def write_new(path, value):
    # Publish only complete files: model worker threads and the budget reader run concurrently.
    fd, temp = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(value)
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temp, path, follow_symlinks=False)
    finally:
        os.unlink(temp)


def private_dir(path):
    path.mkdir(mode=0o700, parents=False, exist_ok=True)
    info = path.lstat()
    if not path.is_dir() or path.is_symlink() or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("run directory must be an owner-only directory")


def load_manifest(path):
    raw = path.read_bytes()
    obj = json.loads(raw)
    docs = obj.get("documents") if isinstance(obj, dict) else None
    if obj.get("schema") != "document-input-benchmark-corpus/v1" or not isinstance(docs, list) or len(docs) != limits.DOCUMENT_BENCH_MAX_DOCUMENTS:
        raise ValueError("manifest requires exactly 30 documents")
    seen = set()
    paths = set()
    for doc in docs:
        if not isinstance(doc, dict) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,79}", doc.get("id", "")) or doc["id"] in seen:
            raise ValueError("invalid or duplicate document id")
        seen.add(doc["id"])
        for field, sha_field in (("path", "sha256"), ("gold_path", "gold_sha256")):
            rel = Path(doc[field])
            if rel.is_absolute() or ".." in rel.parts or not re.fullmatch(r"[0-9a-f]{64}", doc[sha_field]):
                raise ValueError("invalid document path or hash")
            if field == "path" and str(rel) in paths:
                raise ValueError("duplicate PDF path")
            if field == "path":
                paths.add(str(rel))
            source = (path.parent / rel).resolve(strict=True)
            if not source.is_relative_to(path.parent.resolve()) or not source.is_file():
                raise ValueError("document path escapes corpus")
            if field == "path" and source.stat().st_size > limits.DOCUMENT_BENCH_MAX_PDF_BYTES:
                raise ValueError("PDF exceeds cap; no truncation")
            data = source.read_bytes()
            if digest(data) != doc[sha_field]:
                raise ValueError("corpus hash mismatch")
            if field == "path" and not data.startswith(b"%PDF-"):
                raise ValueError("corpus input is not a PDF")
            if field == "gold_path" and json.loads(data).get("schema") != "document-input-benchmark-gold/v1":
                raise ValueError("invalid gold schema")
    return obj, digest(raw)


def extraction(pdf, converter):
    start = time.monotonic()
    try:
        run = subprocess.run(["node", str(Path(__file__).with_name("anydoc.mjs")), str(converter), str(pdf)],
                             capture_output=True, timeout=limits.DOCUMENT_BENCH_TIMEOUT_SECONDS, check=False)
        if len(run.stdout) > limits.DOCUMENT_BENCH_MAX_TEXT_BYTES:
            return {"status": "text_limit_exceeded", "elapsed_seconds": time.monotonic() - start}
        result = json.loads(run.stdout)
        if not isinstance(result, dict) or result.get("status") not in ("extracted", "ocr_needed", "failed"):
            raise ValueError("invalid converter output")
        result["elapsed_seconds"] = time.monotonic() - start
        return result
    except (OSError, subprocess.TimeoutExpired, ValueError, json.JSONDecodeError):
        return {"status": "converter_error", "elapsed_seconds": time.monotonic() - start}


def reference(pdf, layout=False):
    try:
        args = ["pdftotext", "-enc", "UTF-8"] + (["-layout"] if layout else []) + [str(pdf), "-"]
        proc = subprocess.run(args, capture_output=True, timeout=limits.DOCUMENT_BENCH_TIMEOUT_SECONDS, check=False)
        if proc.returncode or len(proc.stdout) > limits.DOCUMENT_BENCH_MAX_TEXT_BYTES:
            return {"status": "failed_or_limit"}
        return {"status": "extracted", "text": proc.stdout.decode("utf-8"), "coverage": "unknown"}
    except (OSError, UnicodeError, subprocess.TimeoutExpired):
        return {"status": "unavailable"}


def prepare(manifest_path, out, converter):
    manifest, manifest_hash = load_manifest(manifest_path)
    private_dir(out)
    package = json.loads((converter / "node_modules/@firecrawl/anydoc/package.json").read_bytes())
    if package.get("version") != "0.2.4":
        raise ValueError("install pinned AnyDoc 0.2.4 in document_text")
    settings = {"schema": "document-input-benchmark-run/v1", "manifest_sha256": manifest_hash,
                "prompt_sha256": digest(PROMPT.encode()), "runner_sha256": digest(Path(__file__).read_bytes()), "models": MODELS, "arms": ARMS,
                "anydoc_version": package["version"], "anydoc_lock_sha256": digest((converter / "package-lock.json").read_bytes()),
                "bridge_sha256": digest(Path(__file__).with_name("anydoc.mjs").read_bytes())}
    path = out / "run.json"
    if path.exists():
        if json.loads(path.read_bytes()) != json.loads(canonical(settings)):
            raise ValueError("run identity changed")
    else:
        write_new(path, canonical(settings))
    for doc in manifest["documents"]:
        dest = out / f"{doc['id']}.input.json"
        if dest.exists():
            old = json.loads(dest.read_bytes())
            if old.get("pdf_sha256") != doc["sha256"]:
                raise ValueError("saved extraction does not match document")
            continue
        pdf = manifest_path.parent / doc["path"]
        result = {"schema": "document-input-benchmark-input/v1", "id": doc["id"], "pdf_sha256": doc["sha256"],
                  "anydoc": extraction(pdf, converter), "pdftotext": reference(pdf), "pdftotext_layout": reference(pdf, True)}
        write_new(dest, canonical(result))
    hashes = {doc["id"]: digest((out / f"{doc['id']}.input.json").read_bytes()) for doc in manifest["documents"]}
    seal = out / "preparation.json"
    if seal.exists():
        if json.loads(seal.read_bytes()) != hashes:
            raise ValueError("prepared input changed")
    else:
        write_new(seal, canonical(hashes))
    return manifest, manifest_hash


def request_json(url, key, *, data=None):
    headers = {"Authorization": "Bearer " + key, "Content-Type": "application/json"}
    req = urllib.request.Request(url, data=canonical(data) if data is not None else None, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=limits.DOCUMENT_BENCH_TIMEOUT_SECONDS) as response:
            body = response.read(limits.DOCUMENT_BENCH_MAX_RESPONSE_BYTES + 1)
            if len(body) > limits.DOCUMENT_BENCH_MAX_RESPONSE_BYTES:
                return {"http_status": response.status, "error": "response_limit_exceeded"}
            return {"http_status": response.status, "body": json.loads(body)}
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read(65537)
            if len(raw) > 65536:
                return {"http_status": exc.code, "error": "http_error_body_limit"}
            raw = raw.replace(key.encode(), b"[REDACTED]")
            body = json.loads(raw)
            error = body.get("error", {}) if isinstance(body, dict) else {}
            details = " ".join(str(error.get(k, "")) for k in ("type", "code", "message")) if isinstance(error, dict) else ""
            unsupported = "unsupportedfunctionality" in details.casefold() or "only image file parts are supported" in details.casefold()
            return {"http_status": exc.code, "error": "unsupported_file_input" if unsupported else "http_error", "body": body}
        except (OSError, ValueError, AttributeError):
            return {"http_status": exc.code, "error": "http_error_invalid_body"}
    except (OSError, TimeoutError, ValueError):
        return {"http_status": None, "error": "transport_or_invalid_json"}


def response_content(body):
    try:
        choice = body["choices"][0]
        text = choice["message"]["content"]
        if not isinstance(text, str):
            raise TypeError()
        try:
            parsed = json.loads(text)
            valid = isinstance(parsed, dict) and isinstance(parsed.get("transcription"), str) and isinstance(parsed.get("facts"), list) and isinstance(parsed.get("tables"), list)
            if valid:
                valid = all(isinstance(f, dict) and isinstance(f.get("label"), str) and "value" in f and
                            (f.get("page") is None or type(f.get("page")) is int and f["page"] > 0) for f in parsed["facts"])
                valid = valid and all(isinstance(t, dict) and isinstance(t.get("rows"), list) and
                                      (t.get("page") is None or type(t.get("page")) is int and t["page"] > 0) and
                                      all(isinstance(row, list) and all(isinstance(cell, str) for cell in row)
                                          for row in t["rows"]) for t in parsed["tables"])
        except (ValueError, TypeError):
            parsed, valid = None, False
        return text, parsed if valid else None, choice.get("finish_reason")
    except (KeyError, IndexError, TypeError, ValueError):
        return None, None, None


def lookup_cost(lookup):
    body = lookup.get("body") if isinstance(lookup, dict) else None
    generation = body.get("data", body) if isinstance(body, dict) else None
    if not isinstance(generation, dict) or generation.get("is_byok") is not False:
        return None
    value = generation.get("total_cost")
    if isinstance(value, bool) or value is None:
        return None
    try:
        cost = float(value)
        return cost if math.isfinite(cost) and cost >= 0 else None
    except (ValueError, TypeError):
        return None


def reconciled_cost(result_path, value):
    sidecar = result_path.with_name(result_path.name.replace(".result.json", ".cost.json"))
    if sidecar.exists():
        saved = json.loads(sidecar.read_bytes())
        if saved.get("result_sha256") != digest(result_path.read_bytes()) or saved.get("generation_id") != value.get("generation_id"):
            raise ValueError("cost reconciliation binding changed")
        return lookup_cost(saved.get("lookup"))
    return value.get("actual_cost_usd")


def reconcile(out, key):
    checked = resolved = 0
    for path in out.glob("*.result.json"):
        value = json.loads(path.read_bytes())
        gen_id = value.get("generation_id")
        sidecar = path.with_name(path.name.replace(".result.json", ".cost.json"))
        if not isinstance(gen_id, str) or not gen_id.startswith("gen_") or sidecar.exists():
            continue
        checked += 1
        lookup = request_json(BASE + "/generation?" + urllib.parse.urlencode({"id": gen_id}), key)
        if lookup_cost(lookup) is not None:
            write_new(sidecar, canonical({"result_sha256": digest(path.read_bytes()), "generation_id": gen_id, "lookup": lookup}))
            resolved += 1
    return {"lookups": checked, "resolved": resolved}


def execute_call(spec, out, key):
    start = time.monotonic()
    result = request_json(BASE + "/chat/completions", key, data=spec["request"])
    elapsed = time.monotonic() - start
    body = result.get("body", {})
    text, parsed, finish = response_content(body) if isinstance(body, dict) else (None, None, None)
    gen_id = body.get("id") if isinstance(body, dict) else None
    lookup = request_json(BASE + "/generation?" + urllib.parse.urlencode({"id": gen_id}), key) if isinstance(gen_id, str) and gen_id.startswith("gen_") else None
    cost = lookup_cost(lookup)
    record = {**spec["identity"], "status": "unsupported" if result.get("error") == "unsupported_file_input" else "completed" if parsed is not None and finish == "stop" else "incomplete",
              "http_status": result.get("http_status"), "error": result.get("error"), "generation_id": gen_id,
              "finish_reason": finish, "truncated": finish == "length", "invalid_json": text is not None and parsed is None,
              "elapsed_seconds": elapsed, "preparation_seconds": spec["preparation_seconds"],
              "end_to_end_seconds": elapsed + spec["preparation_seconds"], "raw_output": text, "parsed": parsed,
              "usage": body.get("usage") if isinstance(body, dict) else None,
              "provider_metadata": body.get("provider_metadata") if isinstance(body, dict) else None,
              "provider_response": body,
              "generation_lookup": lookup, "actual_cost_usd": cost}
    write_new(out / (spec["name"] + ".result.json"), canonical(record))
    return record["status"]


def make_spec(doc, model, arm, manifest_path, out, effort, max_tokens, reserve):
    prepared = json.loads((out / f"{doc['id']}.input.json").read_bytes())
    extracted = prepared["anydoc"]
    preparation_hash = digest((out / f"{doc['id']}.input.json").read_bytes())
    identity = {"document_id": doc["id"], "pdf_sha256": doc["sha256"], "model": model, "arm": arm,
                "prompt_sha256": digest(PROMPT.encode()), "effort": effort, "max_tokens": max_tokens,
                "reservation_usd": reserve, "preparation_sha256": preparation_hash, "requested_provider": model.split("/")[0]}
    name = f"{doc['id']}--{MODELS.index(model)}--{arm}"
    if arm == "anydoc_text" and extracted["status"] != "extracted":
        return {"name": name, "identity": identity, "skip": extracted["status"], "preparation_seconds": extracted["elapsed_seconds"]}
    if arm == "anydoc_text":
        text = extracted["text"]
        identity["input_sha256"] = digest(text.encode())
        content = [{"type": "text", "text": PROMPT}, {"type": "text", "text": text}]
        prep = extracted["elapsed_seconds"]
    else:
        data = (manifest_path.parent / doc["path"]).read_bytes()
        if digest(data) != doc["sha256"]:
            raise ValueError("PDF changed after preparation")
        identity["input_sha256"] = doc["sha256"]
        content = [{"type": "text", "text": PROMPT}, {"type": "file", "file": {"filename": doc["id"] + ".pdf", "file_data": "data:application/pdf;base64," + base64.b64encode(data).decode()}}]
        prep = 0.0
    return {"name": name, "identity": identity, "preparation_seconds": prep,
            "request": {"model": model, "messages": [{"role": "user", "content": content}],
                        "max_tokens": max_tokens, "reasoning": {"effort": effort}, "response_format": {"type": "json_object"},
                        "providerOptions": {"gateway": {"only": [model.split("/")[0]]}}}}


def unsupported_evidence(path):
    if path is None:
        return None
    raw = path.read_bytes()
    if len(raw) > 1024 * 1024:
        raise ValueError("unsupported-route evidence exceeds bound")
    data = json.loads(raw)
    error = data.get("response", {}).get("error", {})
    if (data.get("model") != "alibaba/qwen3.8-flash" or data.get("provider") != "alibaba"
            or data.get("http_status") != 400 or error.get("type") != "AI_UnsupportedFunctionalityError"
            or "only image file parts are supported" not in str(error.get("message", "")).casefold()
            or not re.fullmatch(r"[a-f0-9]{64}", str(data.get("pdf_sha256", "")))
            or data.get("text_layer_empty") is not True):
        raise ValueError("unsupported-route evidence does not establish the qualified Qwen route")
    return digest(raw)


def run_calls(manifest_path, out, *, effort, max_tokens, workers, budget, reservation, credential=None, unsupported_hash=None):
    manifest, manifest_hash = load_manifest(manifest_path)
    expected = json.loads((out / "run.json").read_bytes())
    seal = json.loads((out / "preparation.json").read_bytes())
    if any(seal.get(d["id"]) != digest((out / f"{d['id']}.input.json").read_bytes()) for d in manifest["documents"]):
        raise ValueError("prepared inputs changed")
    if (expected["manifest_sha256"] != manifest_hash or expected["prompt_sha256"] != digest(PROMPT.encode())
            or expected.get("runner_sha256") != digest(Path(__file__).read_bytes())):
        raise ValueError("manifest or prompt changed")
    if effort not in ("low", "medium", "high") or not 1 <= max_tokens <= limits.DOCUMENT_BENCH_MAX_OUTPUT_TOKENS:
        raise ValueError("invalid shared model settings")
    if not 1 <= workers <= limits.DOCUMENT_BENCH_MAX_WORKERS or not 0 < budget <= limits.DOCUMENT_BENCH_BUDGET_USD or not 0 < reservation <= budget:
        raise ValueError("invalid worker or budget setting")
    key = credential or os.environ.get("AI_GATEWAY_API_KEY")
    if not key:
        raise ValueError("AI_GATEWAY_API_KEY is required")
    lock = os.open(out / ".runner.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        config = {"effort": effort, "max_tokens": max_tokens, "budget_usd": budget, "reservation_usd": reservation,
                  "models": MODELS, "arms": ARMS, "manifest_sha256": manifest_hash, "prompt_sha256": digest(PROMPT.encode()),
                  "runner_sha256": digest(Path(__file__).read_bytes()),
                  "unsupported_native_evidence_sha256": unsupported_hash}
        config_path = out / "settings.json"
        if config_path.exists():
            if json.loads(config_path.read_bytes()) != json.loads(canonical(config)):
                raise ValueError("benchmark settings changed; use a new run")
        else:
            write_new(config_path, canonical(config))
        tasks = [(doc, model, arm) for doc in manifest["documents"] for model in MODELS for arm in ARMS]
        statuses = {"dispatched": 0, "skipped": 0, "existing": 0, "budget_blocked": 0}
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
            active = set()
            for doc, model, arm in tasks:
                while len(active) >= workers:
                    done, active = concurrent.futures.wait(active, return_when=concurrent.futures.FIRST_COMPLETED)
                    for item in done:
                        item.result()
                spec = make_spec(doc, model, arm, manifest_path, out, effort, max_tokens, reservation)
                intent = out / (spec["name"] + ".intent.json")
                result = out / (spec["name"] + ".result.json")
                if unsupported_hash and model == "alibaba/qwen3.8-flash" and arm == "native_pdf":
                    spec["identity"]["unsupported_evidence_sha256"] = unsupported_hash
                if intent.exists() or result.exists():
                    if not intent.exists() or not result.exists():
                        raise ValueError("unsettled prior call; reconcile explicitly before resume")
                    if json.loads(intent.read_bytes())["identity"] != spec["identity"]:
                        raise ValueError("prior call identity changed")
                    prior = json.loads(result.read_bytes())
                    if any(prior.get(k) != value for k, value in spec["identity"].items()):
                        raise ValueError("prior result identity changed")
                    statuses["existing"] += 1
                    continue
                if unsupported_hash and model == "alibaba/qwen3.8-flash" and arm == "native_pdf":
                    spec["identity"]["unsupported_evidence_sha256"] = unsupported_hash
                    write_new(intent, canonical({"identity": spec["identity"], "dispatch": "not_started"}))
                    write_new(result, canonical({**spec["identity"], "status": "unsupported_not_run", "http_status": None,
                                                "actual_cost_usd": None, "preparation_seconds": 0.0,
                                                "end_to_end_seconds": None,
                                                "qualification_note": "separate route canary used reasoning_effort; no per-document request"}))
                    statuses["skipped"] += 1
                    continue
                if "skip" in spec:
                    write_new(intent, canonical({"identity": spec["identity"], "dispatch": "not_started"}))
                    write_new(result, canonical({**spec["identity"], "status": spec["skip"], "http_status": None,
                                                "actual_cost_usd": None, "preparation_seconds": spec["preparation_seconds"],
                                                "end_to_end_seconds": spec["preparation_seconds"]}))
                    statuses["skipped"] += 1
                    continue
                # Never refund an uncertain reservation. The declared reservation is not a provider-side hard cap.
                committed = 0.0
                for intent_path in out.glob("*.intent.json"):
                    saved = json.loads(intent_path.read_bytes())
                    if saved["dispatch"] != "started":
                        continue
                    result_path = out / intent_path.name.replace(".intent.json", ".result.json")
                    result_value = json.loads(result_path.read_bytes()) if result_path.exists() else {}
                    cost = reconciled_cost(result_path, result_value) if result_path.exists() else None
                    committed += float(cost) if isinstance(cost, (int, float)) and not isinstance(cost, bool) else float(saved["identity"]["reservation_usd"])
                if committed + reservation > budget + 1e-9:
                    statuses["budget_blocked"] += 1
                    continue
                write_new(intent, canonical({"identity": spec["identity"], "dispatch": "started"}))
                active.add(pool.submit(execute_call, spec, out, key))
                statuses["dispatched"] += 1
            for item in concurrent.futures.as_completed(active):
                item.result()
        return statuses
    finally:
        os.close(lock)


def tokens(text):
    return re.findall(r"\w+", text.casefold(), flags=re.UNICODE)


def score(doc, result, inputs, manifest_path):
    parsed = result.get("parsed")
    if not isinstance(parsed, dict):
        return None
    from collections import Counter
    transcript = parsed["transcription"]
    reference_text = inputs.get("pdftotext", {}).get("text")
    recall = None
    if isinstance(reference_text, str) and tokens(reference_text):
        expected, observed = Counter(tokens(reference_text)), Counter(tokens(transcript))
        recall = sum((expected & observed).values()) / sum(expected.values())
    gold = json.loads((manifest_path.parent / doc["gold_path"]).read_bytes())
    rendered = json.dumps(parsed["facts"], ensure_ascii=False).casefold()
    facts = gold.get("facts", [])
    tables = gold.get("tables", [])
    fact_hits = sum(any(alias.casefold() in rendered for alias in f.get("aliases", []) if isinstance(alias, str) and alias) for f in facts)
    def label_value_match(reference, candidate):
        if not isinstance(candidate, dict) or not isinstance(reference.get("label"), str):
            return False
        label = " ".join(str(candidate.get("label", "")).casefold().split())
        expected_label = " ".join(reference["label"].casefold().split())
        candidate_value = json.dumps(candidate.get("value"), ensure_ascii=False).casefold()
        return label == expected_label and any(isinstance(alias, str) and alias and alias.casefold() in candidate_value
                                               for alias in reference.get("aliases", []))
    label_hits = sum(any(label_value_match(fact, candidate) for candidate in parsed["facts"]) for fact in facts)
    rows = [row for table in tables for row in table.get("rows", [])]
    actual_rows = [row for table in parsed["tables"] if isinstance(table, dict)
                   for row in table.get("rows", []) if isinstance(row, list)]
    def normalize(row):
        return tuple(" ".join(str(cell).casefold().split()) for cell in row)
    from collections import Counter
    expected_rows = Counter(normalize(row.get("cells", [])) for row in rows)
    found_rows = Counter(normalize(row) for row in actual_rows)
    row_hits = sum((expected_rows & found_rows).values())
    return {"pdftotext_token_recall_proxy": recall, "fact_value_presence_proxy_hits": fact_hits, "label_value_exact_proxy_hits": label_hits, "fact_gold_count": len(facts),
            "table_exact_row_hits": row_hits, "table_gold_rows": len(rows),
            "limitations": "value presence can match an unrelated field; ordered-row matches and text recall are proxies, not semantic adjudication"}


def report(manifest_path, out):
    manifest, manifest_hash = load_manifest(manifest_path)
    if json.loads((out / "run.json").read_bytes())["manifest_sha256"] != manifest_hash:
        raise ValueError("manifest changed")
    rows = []
    for doc in manifest["documents"]:
        inputs = json.loads((out / f"{doc['id']}.input.json").read_bytes())
        for model in MODELS:
            for arm in ARMS:
                path = out / f"{doc['id']}--{MODELS.index(model)}--{arm}.result.json"
                if path.exists():
                    result = json.loads(path.read_bytes())
                    result["actual_cost_usd"] = reconciled_cost(path, result)
                    metrics = score(doc, result, inputs, manifest_path)
                    rows.append({k: result.get(k) for k in ("document_id", "model", "arm", "status", "finish_reason", "truncated", "invalid_json", "elapsed_seconds", "end_to_end_seconds", "actual_cost_usd")} | {"metrics": metrics})
                else:
                    rows.append({"document_id": doc["id"], "model": model, "arm": arm, "status": "not_completed", "metrics": None})
    from collections import Counter
    summary = []
    for model in MODELS:
        for arm in ARMS:
            group = [row for row in rows if row["model"] == model and row["arm"] == arm]
            costs = [row["actual_cost_usd"] for row in group if isinstance(row.get("actual_cost_usd"), (float, int))]
            latency = [row["end_to_end_seconds"] for row in group if isinstance(row.get("end_to_end_seconds"), (float, int))]
            measured = [row["metrics"] for row in group if row.get("metrics")]
            recalls = [item["pdftotext_token_recall_proxy"] for item in measured if item["pdftotext_token_recall_proxy"] is not None]
            summary.append({"model": model, "arm": arm, "denominator": len(group),
                            "statuses": dict(Counter(row["status"] for row in group)),
                            "cost_known_count": len(costs), "known_cost_sum_usd": sum(costs) if costs else None,
                            "latency_count": len(latency), "mean_end_to_end_seconds": sum(latency) / len(latency) if latency else None,
                            "mean_text_recall_proxy": sum(recalls) / len(recalls) if recalls else None,
                            "fact_value_presence_proxy_hits": sum(item["fact_value_presence_proxy_hits"] for item in measured),
                            "label_value_exact_proxy_hits": sum(item["label_value_exact_proxy_hits"] for item in measured),
                            "fact_gold_count_scored": sum(item["fact_gold_count"] for item in measured),
                            "table_exact_row_hits": sum(item["table_exact_row_hits"] for item in measured),
                            "table_gold_rows_scored": sum(item["table_gold_rows"] for item in measured)})
    return {"schema": "document-input-benchmark-report/v1", "manifest_sha256": manifest_hash, "expected_calls": len(rows),
            "summary": summary, "rows": rows,
            "warning": "Missing/failed/OCR cases remain in the denominator; unknown cost is null. Automatic metrics are proxies."}


def read_env_key(path):
    """Read only the Gateway key; never echo the file or its values."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 1024 * 1024:
            raise ValueError("credential file must be owner-only and bounded")
        with os.fdopen(fd, "r", encoding="utf8", closefd=False) as stream:
            matches = [line.split("=", 1)[1].strip() for line in stream
                       if re.match(r"^\s*(?:export\s+)?AI_GATEWAY_API_KEY\s*=", line)]
        if len(matches) != 1:
            raise ValueError("credential file must have one Gateway key")
        value = shlex.split(matches[0], comments=True)
        if len(value) != 1 or not value[0] or "$" in value[0] or "`" in value[0]:
            raise ValueError("invalid Gateway key value")
        return value[0]
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("prepare", "run", "report", "reconcile"))
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    parser.add_argument("--converter-dir", type=Path, default=OPS / "cre_collector" / "document_text")
    parser.add_argument("--env-file", type=Path, help="owner-only env file with AI_GATEWAY_API_KEY")
    parser.add_argument("--unsupported-native-evidence", type=Path, help="hash-bound Qwen native-PDF qualification")
    parser.add_argument("--effort", choices=("low", "medium", "high"), default="medium")
    parser.add_argument("--max-tokens", type=int, default=limits.DOCUMENT_BENCH_MAX_OUTPUT_TOKENS)
    parser.add_argument("--workers", type=int, default=limits.DOCUMENT_BENCH_DEFAULT_WORKERS)
    parser.add_argument("--budget", type=float, default=limits.DOCUMENT_BENCH_DEFAULT_BUDGET_USD)
    parser.add_argument("--reservation", type=float, default=limits.DOCUMENT_BENCH_CALL_RESERVATION_USD)
    args = parser.parse_args()
    if args.action == "prepare":
        result, _ = prepare(args.manifest, args.out, args.converter_dir)
        print(json.dumps({"documents": len(result["documents"]), "status": "prepared"}))
    elif args.action == "run":
        private_dir(args.out)
        proof_hash = unsupported_evidence(args.unsupported_native_evidence)
        if proof_hash:
            proof = args.unsupported_native_evidence.read_bytes()
            target = args.out / "unsupported-native-evidence.json"
            if target.exists():
                if digest(target.read_bytes()) != proof_hash:
                    raise ValueError("unsupported route evidence changed")
            else:
                write_new(target, proof)
        print(json.dumps(run_calls(args.manifest, args.out, effort=args.effort, max_tokens=args.max_tokens,
                                   workers=args.workers, budget=args.budget, reservation=args.reservation,
                                   credential=read_env_key(args.env_file) if args.env_file else None,
                                   unsupported_hash=proof_hash)))
    elif args.action == "reconcile":
        private_dir(args.out)
        key = read_env_key(args.env_file) if args.env_file else os.environ.get("AI_GATEWAY_API_KEY")
        if not key:
            raise ValueError("AI_GATEWAY_API_KEY is required")
        print(json.dumps(reconcile(args.out, key)))
    else:
        result = report(args.manifest, args.out)
        target = args.out / "report.json"
        write_new(target, canonical(result))
        print(json.dumps({"expected_calls": result["expected_calls"], "report": str(target)}))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError) as error:
        print(f"benchmark refused: {type(error).__name__}: {error}", file=sys.stderr)
        sys.exit(1)
