"""Finite native-PDF benchmark: offline prepare/report/adopt, opt-in calls.

Inputs and outputs can contain documents and responses; keep them owner-only.
The planning reservation is not a provider-enforced spending limit.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import fcntl
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path

import postprocess
import runner

limits = runner.limits
FIXTURE = Path(__file__).parent / "fixtures" / "synthetic-image-only.pdf"
FIXTURE_SHA256 = "d64dfdb48b80399a628b1bcb1459ec050efd64cf935f731777cec698f148c04f"
PROBE_PROMPT = (
    "Read the attached document. Return JSON with marker and annual_rent_usd_by_unit. "
    "Copy the exact visible marker and each unit label with its amount. "
    "If no document is visible say unavailable; never guess."
)
MODEL = re.compile(r"[a-z0-9][a-z0-9.-]*/[a-z0-9][a-z0-9.-]*\Z")
PROVIDER = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9.-]*\Z")
IDENT = re.compile(
    rf"[a-z0-9][a-z0-9_-]{{0,{limits.DOCUMENT_BENCH_MAX_ID_CHARS - 1}}}\Z"
)
EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh", "max"}


def load_json(path):
    raw = path.read_bytes()
    if len(raw) > limits.DOCUMENT_BENCH_MAX_METADATA_BYTES:
        raise ValueError("metadata too large")
    return json.loads(raw), runner.digest(raw)


def validate_config(config):
    if (
        not isinstance(config, dict)
        or config.get("schema") != "native-pdf-benchmark/v1"
    ):
        raise ValueError("invalid config schema")
    routes = config.get("routes")
    if not isinstance(routes, list) or not routes:
        raise ValueError("routes required")
    seen = set()
    for route in routes:
        if not isinstance(route, dict) or set(route) != {
            "model",
            "provider",
            "effort",
            "max_tokens",
        }:
            raise ValueError("route fields required")
        model, provider, effort, maximum = (
            route[k] for k in ("model", "provider", "effort", "max_tokens")
        )
        if (
            not isinstance(model, str)
            or not MODEL.fullmatch(model)
            or not isinstance(provider, str)
            or not PROVIDER.fullmatch(provider)
        ):
            raise ValueError("invalid model/provider")
        if "-fast" in model or "highspeed" in model:
            raise ValueError("fast-mode route prohibited")
        if (
            effort not in EFFORTS
            or type(maximum) is not int
            or not 1 <= maximum <= limits.DOCUMENT_BENCH_MAX_OUTPUT_TOKENS
        ):
            raise ValueError("invalid effort or output cap")
        key = (model, provider, effort, maximum)
        if key in seen:
            raise ValueError("duplicate route")
        seen.add(key)
    for field, maximum in (
        ("budget_usd", limits.DOCUMENT_BENCH_BUDGET_USD),
        ("reservation_usd", limits.DOCUMENT_BENCH_BUDGET_USD),
    ):
        value = config.get(field)
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or not 0 < value <= maximum
        ):
            raise ValueError("invalid budget or reservation")
    if config["reservation_usd"] > config["budget_usd"]:
        raise ValueError("reservation exceeds budget")
    workers = config.get("workers")
    if (
        type(workers) is not int
        or not 1 <= workers <= limits.DOCUMENT_BENCH_MAX_WORKERS
    ):
        raise ValueError("invalid workers")
    if set(config) != {"schema", "routes", "budget_usd", "reservation_usd", "workers"}:
        raise ValueError("unknown config fields")
    return config


def validate_manifest(path):
    manifest, sha = load_json(path)
    docs = manifest.get("documents") if isinstance(manifest, dict) else None
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema") != "document-input-benchmark-corpus/v1"
        or not isinstance(docs, list)
        or not 1 <= len(docs) <= limits.NATIVE_PDF_BENCH_MAX_DOCUMENTS
    ):
        raise ValueError("invalid corpus or document count exceeds shared limit")
    root = path.parent.resolve(strict=True)
    seen, paths, clean = set(), set(), []
    for doc in docs:
        if not isinstance(doc, dict):
            raise TypeError("invalid document")
        ident, rel, expected, pages = (
            doc.get(k) for k in ("id", "path", "sha256", "pages")
        )
        if not isinstance(ident, str) or not IDENT.fullmatch(ident) or ident in seen:
            raise ValueError("invalid or duplicate document id")
        if (
            not isinstance(rel, str)
            or Path(rel).is_absolute()
            or ".." in Path(rel).parts
            or rel in paths
        ):
            raise ValueError("invalid or duplicate document path")
        source = root / rel
        if any(
            (root.joinpath(*Path(rel).parts[:i])).is_symlink()
            for i in range(1, len(Path(rel).parts) + 1)
        ):
            raise ValueError("symlink document path")
        resolved = source.resolve(strict=True)
        if (
            not resolved.is_relative_to(root)
            or not resolved.is_file()
            or resolved.stat().st_size > limits.DOCUMENT_BENCH_MAX_PDF_BYTES
        ):
            raise ValueError("invalid document file")
        blob = resolved.read_bytes()
        if (
            not blob.startswith(b"%PDF-")
            or runner.digest(blob) != expected
            or type(pages) is not int
            or pages < 1
        ):
            raise ValueError("PDF hash, signature or page count invalid")
        info = subprocess.run(
            ["pdfinfo", str(resolved)],
            capture_output=True,
            timeout=limits.DOCUMENT_BENCH_TIMEOUT_SECONDS,
            check=False,
        )
        match = re.search(rb"(?m)^Pages:\s*([0-9]+)\s*$", info.stdout)
        if info.returncode != 0 or not match or int(match.group(1)) != pages:
            raise ValueError("PDF page count does not match manifest")
        saved = {"id": ident, "path": rel, "sha256": expected, "pages": pages}
        gold_path, gold_hash = doc.get("gold_path"), doc.get("gold_sha256")
        if gold_path is not None or gold_hash is not None:
            if (
                not isinstance(gold_path, str)
                or Path(gold_path).is_absolute()
                or ".." in Path(gold_path).parts
            ):
                raise ValueError("invalid gold path")
            if any(
                (root.joinpath(*Path(gold_path).parts[:i])).is_symlink()
                for i in range(1, len(Path(gold_path).parts) + 1)
            ):
                raise ValueError("symlink gold path")
            gold = (root / gold_path).resolve(strict=True)
            if (
                not gold.is_relative_to(root)
                or not gold.is_file()
                or gold.stat().st_size > limits.DOCUMENT_BENCH_MAX_METADATA_BYTES
                or runner.digest(gold.read_bytes()) != gold_hash
            ):
                raise ValueError("gold hash or path invalid")
            saved.update({"gold_path": gold_path, "gold_sha256": gold_hash})
        seen.add(ident)
        paths.add(rel)
        clean.append(saved)
    return clean, sha


def ensure_private(path):
    runner.private_dir(path)
    return path


def prepare(config_path, manifest_path, out):
    config_path = config_path.resolve(strict=True)
    manifest_path = manifest_path.resolve(strict=True)
    config, config_hash = load_json(config_path)
    validate_config(config)
    docs, manifest_hash = validate_manifest(manifest_path)
    if runner.digest(FIXTURE.read_bytes()) != FIXTURE_SHA256:
        raise ValueError("synthetic fixture changed")
    revision = subprocess.run(
        ["git", "-C", str(Path(__file__).parent), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    ensure_private(out)
    plan = {
        "schema": "native-pdf-benchmark-plan/v1",
        "config": config,
        "config_path": str(config_path),
        "config_sha256": config_hash,
        "manifest_path": str(manifest_path),
        "manifest_sha256": manifest_hash,
        "documents": docs,
        "prompt_sha256": runner.digest(runner.PROMPT.encode()),
        "runner_sha256": runner.digest(Path(runner.__file__).read_bytes()),
        "fixture_sha256": FIXTURE_SHA256,
        "module_sha256": runner.digest(Path(__file__).read_bytes()),
        "source_revision": revision,
    }
    plan_bytes = runner.canonical(plan)
    runner.write_new(out / "plan.json", plan_bytes)
    runner.write_new(
        out / "plan-seal.json",
        runner.canonical({"plan_sha256": runner.digest(plan_bytes)}),
    )
    ensure_private(out / "qualification")
    ensure_private(out / "results")
    return {
        "documents": len(docs),
        "routes": len(config["routes"]),
        "cells": len(docs) * len(config["routes"]),
        "network_calls": 0,
    }


def read_plan(out):
    ensure_private(out)
    plan, ph = load_json(out / "plan.json")
    if plan.get("schema") != "native-pdf-benchmark-plan/v1":
        raise ValueError("invalid plan")
    seal, _ = load_json(out / "plan-seal.json")
    config, config_hash = load_json(Path(plan["config_path"]))
    if (
        seal != {"plan_sha256": ph}
        or config != plan["config"]
        or config_hash != plan["config_sha256"]
    ):
        raise ValueError("sealed plan or config changed")
    validate_config(plan["config"])
    docs, mh = validate_manifest(Path(plan["manifest_path"]))
    if (
        docs != plan["documents"]
        or mh != plan["manifest_sha256"]
        or plan["prompt_sha256"] != runner.digest(runner.PROMPT.encode())
        or plan["runner_sha256"] != runner.digest(Path(runner.__file__).read_bytes())
        or plan["module_sha256"] != runner.digest(Path(__file__).read_bytes())
        or plan["fixture_sha256"] != runner.digest(FIXTURE.read_bytes())
    ):
        raise ValueError("sealed plan or source changed")
    return plan, ph


def request_for(route, blob, name, prompt, max_tokens):
    content = [
        {"type": "text", "text": prompt},
        {
            "type": "file",
            "file": {
                "filename": name + ".pdf",
                "file_data": "data:application/pdf;base64,"
                + base64.b64encode(blob).decode(),
            },
        },
    ]
    return {
        "model": route["model"],
        "messages": [{"role": "user", "content": content}],
        "max_tokens": max_tokens,
        "reasoning": {"effort": route["effort"]},
        "response_format": {"type": "json_object"},
        "providerOptions": {"gateway": {"only": [route["provider"]]}},
    }


def make_spec(plan, ph, doc, route, *, probe=False):
    name = "probe" if probe else doc["id"]
    blob = (
        FIXTURE.read_bytes()
        if probe
        else (Path(plan["manifest_path"]).parent / doc["path"]).read_bytes()
    )
    source_hash = FIXTURE_SHA256 if probe else doc["sha256"]
    if runner.digest(blob) != source_hash:
        raise ValueError("source hash changed")
    request = request_for(
        route,
        blob,
        name,
        PROBE_PROMPT if probe else runner.PROMPT,
        min(route["max_tokens"], limits.NATIVE_PDF_BENCH_PROBE_MAX_OUTPUT_TOKENS)
        if probe
        else route["max_tokens"],
    )
    route_id = runner.digest(runner.canonical(route))[:16]
    identity = {
        "document_id": name,
        "pdf_sha256": source_hash,
        "model": route["model"],
        "requested_provider": route["provider"],
        "arm": "native_pdf",
        "effort": route["effort"],
        "max_tokens": request["max_tokens"],
        "plan_sha256": ph,
        "manifest_sha256": plan["manifest_sha256"],
        "request_sha256": runner.digest(runner.canonical(request)),
        "reservation_usd": plan["config"]["reservation_usd"],
    }
    if not probe:
        identity["pages"] = doc["pages"]
    return {
        "name": name + "--" + route_id,
        "identity": identity,
        "request": request,
        "preparation_seconds": 0.0,
    }


def lookup_record(directory, path, result):
    side = path.with_name(path.name.replace(".result.json", ".cost.json"))
    if side.exists():
        saved = json.loads(side.read_bytes())
        if saved.get("result_sha256") != runner.digest(path.read_bytes()) or saved.get(
            "generation_id"
        ) != result.get("generation_id"):
            raise ValueError("receipt binding mismatch")
        return saved.get("lookup")
    return result.get("generation_lookup")


def validate_response(result):
    """Bind reported content to the captured provider response."""
    body = result.get("provider_response", {})
    text, parsed, finish = (
        runner.response_content(body) if isinstance(body, dict) else (None, None, None)
    )
    if (text, parsed, finish) != (
        result.get("raw_output"),
        result.get("parsed"),
        result.get("finish_reason"),
    ):
        raise ValueError("response content mismatch")
    if result.get("usage") != (body.get("usage") if isinstance(body, dict) else None):
        raise ValueError("response usage mismatch")
    expected_status = (
        "unsupported"
        if result.get("error") == "unsupported_file_input"
        else "completed"
        if parsed is not None and finish == "stop"
        else "incomplete"
    )
    if result.get("status") != expected_status:
        raise ValueError("response status mismatch")


def verified_cost(directory, path, result, identity):
    lookup = lookup_record(directory, path, result)
    if not isinstance(lookup, dict) or lookup.get("http_status") != 200:
        return None
    body = lookup.get("body", {})
    body = body.get("data", body) if isinstance(body, dict) else {}
    if not isinstance(body, dict):
        return None
    provider, model = body.get("provider_name"), body.get("model")
    if provider is not None and provider != identity["requested_provider"]:
        raise ValueError("provider receipt mismatch")
    if model is not None and model != identity["model"]:
        raise ValueError("model receipt mismatch")
    generation_id = result.get("generation_id")
    response = result.get("provider_response")
    if isinstance(response, dict) and response.get("id") != generation_id:
        raise ValueError("response generation identity mismatch")
    if not isinstance(response, dict):
        return None
    if (
        not isinstance(generation_id, str)
        or not generation_id.startswith("gen_")
        or body.get("id") != generation_id
        or provider != identity["requested_provider"]
        or model != identity["model"]
    ):
        return None
    return runner.lookup_cost(lookup)


def validate_inventory(directory, plan, ph, *, probe):
    """Reject stray or changed cells before spending or reconciling."""
    expected = {}
    docs = [None] if probe else plan["documents"]
    for doc in docs:
        for route in plan["config"]["routes"]:
            name = (
                ("probe" if probe else doc["id"])
                + "--"
                + runner.digest(runner.canonical(route))[:16]
            )
            expected[name] = (doc, route)
    for path in directory.glob("*.intent.json"):
        name = path.name.removesuffix(".intent.json")
        if name not in expected:
            raise ValueError("unexpected intent")
        doc, route = expected[name]
        spec = make_spec(plan, ph, doc, route, probe=probe)
        saved = json.loads(path.read_bytes())
        if (
            saved.get("identity") != spec["identity"]
            or saved.get("dispatch") != "started"
        ):
            raise ValueError("changed intent")
    for path in directory.glob("*.result.json"):
        name = path.name.removesuffix(".result.json")
        if name not in expected or not (directory / (name + ".intent.json")).exists():
            raise ValueError("unexpected result")


def audit_existing(directory, active_names=frozenset()):
    total = 0.0
    for intent in directory.glob("*.intent.json"):
        saved = json.loads(intent.read_bytes())
        identity = saved["identity"]
        result_path = intent.with_name(
            intent.name.replace(".intent.json", ".result.json")
        )
        if not result_path.exists():
            if intent.name not in active_names:
                raise ValueError("unsettled intent: no repeat or further dispatch")
            total += identity["reservation_usd"]
            continue
        result = json.loads(result_path.read_bytes())
        if any(result.get(k) != v for k, v in identity.items()):
            raise ValueError("result identity mismatch")
        validate_response(result)
        cost = verified_cost(directory, result_path, result, identity)
        total += cost if cost is not None else identity["reservation_usd"]
    for result in directory.glob("*.result.json"):
        if not result.with_name(
            result.name.replace(".result.json", ".intent.json")
        ).exists():
            raise ValueError("result without intent")
    return total


def qualifies(directory, spec):
    intent_path = directory / (spec["name"] + ".intent.json")
    result_path = directory / (spec["name"] + ".result.json")
    if not intent_path.exists() or not result_path.exists():
        return False
    identity = json.loads(intent_path.read_bytes()).get("identity")
    result = json.loads(result_path.read_bytes())
    if identity != spec["identity"] or any(
        result.get(k) != v for k, v in identity.items()
    ):
        raise ValueError("qualification identity changed")
    validate_response(result)
    if (
        verified_cost(directory, result_path, result, identity) is None
        or result.get("http_status") != 200
        or result.get("finish_reason") != "stop"
    ):
        return False
    text = result.get("raw_output")
    if not isinstance(text, str):
        return False
    fence = postprocess.FENCED_JSON.fullmatch(text)
    try:
        value = json.loads(fence.group(1) if fence else text)
        amounts = value["annual_rent_usd_by_unit"]
        return (
            value["marker"] == "CEDAR-9472"
            and str(amounts["ALPHA"]).replace(",", "") == "41327"
            and str(amounts["BETA"]).replace(",", "") == "86519"
        )
    except (ValueError, KeyError, TypeError):
        return False


def credential(env_file):
    key = (
        runner.read_env_key(env_file)
        if env_file
        else os.environ.get("AI_GATEWAY_API_KEY")
    )
    if not key:
        raise ValueError("Gateway key required")
    return key


def dispatch(out, plan, ph, *, probe, key):
    qualification, results = (
        ensure_private(out / "qualification"),
        ensure_private(out / "results"),
    )
    target = qualification if probe else results
    routes = plan["config"]["routes"]
    if not probe and not all(
        qualifies(qualification, make_spec(plan, ph, None, route, probe=True))
        for route in routes
    ):
        raise ValueError(
            "matching synthetic qualifications and verified receipts required"
        )
    tasks = (
        [(None, route) for route in routes]
        if probe
        else [(doc, route) for doc in plan["documents"] for route in routes]
    )
    lock = os.open(
        out / ".dispatch.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600
    )
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        validate_inventory(qualification, plan, ph, probe=True)
        validate_inventory(results, plan, ph, probe=False)
        audit_existing(qualification)
        audit_existing(results)
        pending = []
        for doc, route in tasks:
            spec = make_spec(plan, ph, doc, route, probe=probe)
            intent = target / (spec["name"] + ".intent.json")
            result = target / (spec["name"] + ".result.json")
            if intent.exists() or result.exists():
                if (
                    not intent.exists()
                    or not result.exists()
                    or json.loads(intent.read_bytes()).get("identity")
                    != spec["identity"]
                ):
                    raise ValueError("unsettled or changed existing cell")
                continue
            pending.append((doc, route))
        sent = 0
        with concurrent.futures.ThreadPoolExecutor(
            max_workers=plan["config"]["workers"]
        ) as pool:
            active = {}
            while pending or active:
                while pending and len(active) < plan["config"]["workers"]:
                    active_names = frozenset(active.values())
                    spent = audit_existing(
                        qualification, active_names
                    ) + audit_existing(results, active_names)
                    if (
                        spent + plan["config"]["reservation_usd"]
                        > plan["config"]["budget_usd"] + 1e-9
                    ):
                        break
                    doc, route = pending.pop(0)
                    spec = make_spec(plan, ph, doc, route, probe=probe)
                    runner.write_new(
                        target / (spec["name"] + ".intent.json"),
                        runner.canonical(
                            {"identity": spec["identity"], "dispatch": "started"}
                        ),
                    )
                    future = pool.submit(runner.execute_call, spec, target, key)
                    active[future] = spec["name"] + ".intent.json"
                    sent += 1
                if active:
                    done, _ = concurrent.futures.wait(
                        active, return_when=concurrent.futures.FIRST_COMPLETED
                    )
                    for future in done:
                        future.result()
                        del active[future]
                elif pending:
                    break
        return {
            "sent": sent,
            "remaining": len(pending),
            "planning_budget_usd": plan["config"]["budget_usd"],
            "provider_cap": False,
        }
    finally:
        os.close(lock)


def result_metadata(path, result):
    """Return body-free, format-only metrics; never expose the model response."""
    view = postprocess.derive(path.read_bytes())
    usable = (
        result.get("http_status") == 200
        and result.get("finish_reason") == "stop"
        and result.get("truncated") is False
    )
    usage = result.get("usage")
    raw = result.get("raw_output")
    field_readable, field_view = False, None
    if isinstance(raw, str):
        fence = postprocess.FENCED_JSON.fullmatch(raw)
        try:
            field_readable = bool(
                usable
                and isinstance(json.loads(fence.group(1) if fence else raw), dict)
            )
            if field_readable:
                field_view = (
                    "outer_json_fence_object" if fence else "strict_json_object"
                )
        except ValueError:
            pass
    syntax = "no_content"
    if isinstance(raw, str):
        try:
            json.loads(raw)
            syntax = "valid_json"
        except ValueError:
            syntax = "invalid_json"
    schema = (
        "valid"
        if result.get("parsed") is not None
        else "invalid"
        if syntax == "valid_json"
        else "unavailable"
    )
    known_errors = {
        "response_limit_exceeded",
        "http_error_body_limit",
        "unsupported_file_input",
        "http_error",
        "http_error_invalid_body",
        "transport_or_invalid_json",
    }
    error = result.get("error")
    safe_error = error if error in known_errors else "other" if error else None

    def numeric_tree(value):
        if not isinstance(value, dict):
            return {}
        return {
            key: numeric_tree(item) if isinstance(item, dict) else item
            for key, item in value.items()
            if isinstance(key, str)
            and (isinstance(item, dict) or type(item) in (int, float))
        }

    return {
        "http_status": result.get("http_status"),
        "error_code": safe_error,
        "json_syntax": syntax,
        "schema_validity": schema,
        "field_object_readable": field_readable,
        "field_object_view": field_view,
        "truncated": result.get("truncated"),
        "finish_reason": result.get("finish_reason"),
        "elapsed_seconds": result.get("elapsed_seconds"),
        "strict_usable": bool(usable and result.get("parsed") is not None),
        "usable_after_fence": bool(usable and view["normalized_valid"]),
        "fence_recovered": view["removed_outer_json_fence"],
        "usage": numeric_tree(usage),
        "result_sha256": runner.digest(path.read_bytes()),
    }


def report(out, plan, ph):
    validate_inventory(ensure_private(out / "results"), plan, ph, probe=False)
    rows = []
    for doc in plan["documents"]:
        for route in plan["config"]["routes"]:
            spec = make_spec(plan, ph, doc, route)
            target = out / "results" / (spec["name"] + ".result.json")
            intent = out / "results" / (spec["name"] + ".intent.json")
            row = {
                "document_id": doc["id"],
                "model": route["model"],
                "provider": route["provider"],
                "effort": route["effort"],
                "max_tokens": route["max_tokens"],
                "pages": doc["pages"],
                "pdf_sha256": doc["sha256"],
                "status": "not_started",
                "cost_usd": None,
            }
            if intent.exists():
                if json.loads(intent.read_bytes()).get("identity") != spec["identity"]:
                    raise ValueError("intent identity mismatch")
                row["status"] = "unsettled"
            if target.exists():
                if not intent.exists():
                    raise ValueError("result without intent")
                result = json.loads(target.read_bytes())
                if any(result.get(k) != v for k, v in spec["identity"].items()):
                    raise ValueError("result identity mismatch")
                validate_response(result)
                row.update(result_metadata(target, result))
                row.update(
                    {
                        "status": result.get("status"),
                        "cost_usd": verified_cost(
                            out / "results", target, result, spec["identity"]
                        ),
                    }
                )
            rows.append(row)
    return {
        "schema": "native-pdf-benchmark-report/v1",
        "plan_sha256": ph,
        "manifest_sha256": plan["manifest_sha256"],
        "prompt_sha256": plan["prompt_sha256"],
        "rows": rows,
        "warning": "Format and cost only; this is not semantic accuracy or full-text recall. Null cost is unknown.",
    }


def adopt(source, manifest_path, protocol_path, config_path, script_path):
    """Verify existing legacy results against explicit historical source bindings."""
    if not source.is_dir() or source.is_symlink():
        raise ValueError("existing private legacy source directory required")
    ensure_private(source)
    documents, manifest_hash = validate_manifest(manifest_path.resolve(strict=True))
    docs = {doc["id"]: doc for doc in documents}
    protocol, protocol_hash = load_json(protocol_path)
    _, config_hash = load_json(config_path)
    script_hash = runner.digest(script_path.read_bytes())
    if protocol.get("manifest_sha256") != manifest_hash or protocol.get(
        "prompt_sha256"
    ) != runner.digest(runner.PROMPT.encode()):
        raise ValueError("legacy protocol source binding mismatch")
    expected_config = protocol.get(
        "routes_config_sha256", protocol.get("config_sha256")
    )
    if config_hash != expected_config or protocol.get("runner_sha256") != runner.digest(
        Path(runner.__file__).read_bytes()
    ):
        raise ValueError("legacy config or runner binding mismatch")
    routes = protocol.get("required_routes") or [protocol.get("route")]
    allowed = {
        (route["model"], route["provider"])
        for route in routes
        if isinstance(route, dict)
    }
    if not allowed:
        raise ValueError("legacy routes missing")
    rows = []
    for result_path in sorted(source.glob("*.result.json")):
        value = json.loads(result_path.read_bytes())
        intent_path = result_path.with_name(
            result_path.name.replace(".result.json", ".intent.json")
        )
        if not intent_path.exists():
            raise ValueError("legacy result without intent")
        saved = json.loads(intent_path.read_bytes())
        identity = saved.get("identity")
        if not isinstance(identity, dict) or any(
            value.get(k) != v for k, v in identity.items()
        ):
            raise ValueError("legacy identity mismatch")
        doc = docs.get(identity.get("document_id"))
        route_key = (identity.get("model"), identity.get("requested_provider"))
        if (
            not doc
            or route_key not in allowed
            or identity.get("pdf_sha256") != doc["sha256"]
            or identity.get("pages") != doc["pages"]
            or identity.get("manifest_sha256") != manifest_hash
            or identity.get("protocol_sha256") != protocol_hash
            or identity.get("config_sha256") != config_hash
            or identity.get("script_sha256") != script_hash
            or identity.get("runner_sha256") != protocol["runner_sha256"]
            or identity.get("prompt_sha256") != protocol["prompt_sha256"]
            or identity.get("effort") != protocol.get("effort")
            or identity.get("max_tokens") != protocol.get("max_tokens")
            or identity.get("arm") != "native_pdf"
        ):
            raise ValueError("legacy source, route or protocol mismatch")
        route = {
            "model": route_key[0],
            "provider": route_key[1],
            "effort": identity["effort"],
            "max_tokens": identity["max_tokens"],
        }
        request = request_for(
            route,
            (manifest_path.resolve().parent / doc["path"]).read_bytes(),
            doc["id"],
            runner.PROMPT,
            route["max_tokens"],
        )
        if runner.digest(runner.canonical(request)) != identity.get("request_sha256"):
            raise ValueError("legacy request reconstruction mismatch")
        validate_response(value)
        row = {
            "document_id": identity.get("document_id"),
            "model": identity.get("model"),
            "provider": identity.get("requested_provider"),
            "effort": identity.get("effort"),
            "max_tokens": identity.get("max_tokens"),
            "pages": doc["pages"],
            "pdf_sha256": doc["sha256"],
            "intent_sha256": runner.digest(intent_path.read_bytes()),
            "cost_usd": verified_cost(source, result_path, value, identity),
            "status": value.get("status"),
        }
        row.update(result_metadata(result_path, value))
        rows.append(row)
    return {
        "schema": "native-pdf-legacy-inspection/v1",
        "manifest_sha256": manifest_hash,
        "prompt_sha256": protocol["prompt_sha256"],
        "rows": rows,
        "note": "Read-only; rows cover settled results only, not completeness; null costs unknown.",
        "intent_count": len(list(source.glob("*.intent.json"))),
        "result_count": len(rows),
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "action", choices=("prepare", "qualify", "run", "reconcile", "report", "adopt")
    )
    parser.add_argument("--config", type=Path)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--out", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--protocol", type=Path)
    parser.add_argument("--script", type=Path)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="explicitly authorize paid qualification or run",
    )
    args = parser.parse_args(argv)
    if args.action == "adopt":
        if not all(
            (args.source, args.manifest, args.protocol, args.config, args.script)
        ):
            raise ValueError(
                "--source, --manifest, --protocol, --config and --script required"
            )
        output = adopt(
            args.source, args.manifest, args.protocol, args.config, args.script
        )
    elif args.action == "prepare":
        if not args.config or not args.manifest or not args.out:
            raise ValueError("--config, --manifest and --out required")
        output = prepare(args.config, args.manifest, args.out)
    else:
        if not args.out:
            raise ValueError("--out required")
        plan, ph = read_plan(args.out)
        if args.action == "report":
            output = report(args.out, plan, ph)
        elif args.action == "reconcile":
            for name, probe in (("qualification", True), ("results", False)):
                directory = ensure_private(args.out / name)
                validate_inventory(directory, plan, ph, probe=probe)
                audit_existing(directory)
            key = credential(args.env_file)
            output = {}
            for name in ("qualification", "results"):
                directory = ensure_private(args.out / name)
                runner.reconcile(directory, key)
                audit_existing(directory)
                resolved = pending = 0
                for result_path in directory.glob("*.result.json"):
                    result = json.loads(result_path.read_bytes())
                    intent_path = result_path.with_name(
                        result_path.name.replace(".result.json", ".intent.json")
                    )
                    identity = json.loads(intent_path.read_bytes())["identity"]
                    if verified_cost(directory, result_path, result, identity) is None:
                        pending += 1
                    else:
                        resolved += 1
                output[name] = {"verified_costs": resolved, "pending_costs": pending}
        else:
            if not args.execute:
                output = {
                    "dry_run": True,
                    "action": args.action,
                    "routes": len(plan["config"]["routes"]),
                    "cells": len(plan["config"]["routes"])
                    * (1 if args.action == "qualify" else len(plan["documents"])),
                    "network_calls": 0,
                    "note": "--execute required for paid calls",
                }
            else:
                output = dispatch(
                    args.out,
                    plan,
                    ph,
                    probe=args.action == "qualify",
                    key=credential(args.env_file),
                )
    print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    try:
        main()
    except (ValueError, OSError, KeyError, TypeError, json.JSONDecodeError) as exc:
        print(
            f"native PDF benchmark refused: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        sys.exit(1)
