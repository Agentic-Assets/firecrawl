"""Validate a bounded NAI public-feed capture before private retention.

The publisher request and HTTP receipt are collector assertions, not proof of
publisher authentication, freshness, identity, visibility or completeness.
"""
import base64
import binascii
import hashlib
import json
import os
import stat
from datetime import datetime
from pathlib import Path

from buildout_observation_artifact import invalid_number, unique_object

from source_evidence import acquisition, events

QUERY = ("query GET_LISTINGS_PUBLIC_POSTS($filter: PostFilter, $offset: Int, $limit: Int) "
         "{ publicPosts(filter: $filter, offset: $offset, limit: $limit) { id title summary content tags currency "
         "listingStatus price landSize sizeTotal sizeRangeH sizeRangeL url urlOriginal contactEmail urlDocument "
         "documentPreview updatedAt publishedAt contentType { id name } postImages { id url index } "
         "locations { id name geometry path } source { id socialLinks name bannerS3 logoS3(format: LOGO_100X100) } } }")
MAX_ENTRY_BYTES = ((16 * 1024 * 1024 * 4 + 2) // 3) + 64 * 1024 * 6 + 16384
PROVENANCE = {"bodyHashKind": "fetch_delivered_body_bytes_not_wire", "identity": "unverified",
              "visibility": "unverified", "completeness": "unverified"}
NATIVE_FORMATS = {"nai-global": "nai_public_posts_v1", "avison-young": "avison_sharplaunch_website_v1"}
ENV_PREFIXES = {"nai-global": "CRE_NAI_NATIVE_CAPTURE", "avison-young": "CRE_AVISON_NATIVE_CAPTURE"}


def supported(source, config):
    return source in NATIVE_FORMATS and config.get("native_source_formats", {}).get(source) == NATIVE_FORMATS[source]


def configure_worker_env(env, run_dir, source, attempt, config):
    """Remove inherited capture opt-ins; enable only the owned admitted attempt."""
    for prefix in ENV_PREFIXES.values():
        for suffix in ("DIR", "MAX_PAGES", "MAX_BYTES", "SINGLE_WRITER"):
            env.pop(prefix + "_" + suffix, None)
    if attempt.get("native_source_evidence"):
        number = attempt.get("number")
        expected = f"source-evidence/native-{source}-attempt-{number}"
        if (not supported(source, config) or type(number) is not int or not 1 <= number <= 1000000
                or attempt["native_source_evidence"].get("directory") != expected):
            raise ValueError("native source format is unsupported")
        prefix = ENV_PREFIXES[source]
        env[prefix + "_DIR"] = str(run_dir / expected)
        env[prefix + "_MAX_PAGES"] = str(config["max_native_pages"])
        env[prefix + "_MAX_BYTES"] = str(128 * 1024 * 1024)
        env[prefix + "_SINGLE_WRITER"] = "1"


def _decode(source, raw, filename, binding):
    if source == "avison-young":
        from source_evidence.avison_pages import decode_entry as decode_avison
        return decode_avison(raw, filename, binding)
    return decode_entry(raw, filename, binding)


def decode_entry(raw, filename, binding):
    """Validate one private page file and return its observation or diagnostic."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ENTRY_BYTES:
        raise ValueError("native page exceeds bound")
    entry = json.loads(raw.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if not isinstance(entry, dict) or set(entry) != {"schema", "receipt", "requestBody", "requestBodySha256",
                                                     "requestBodyBytes", "responseBodyBase64", "provenance"}:
        raise ValueError("native page shape mismatch")
    if entry["schema"] != "cre-nai-public-posts-page/v1" or entry["provenance"] != PROVENANCE:
        raise ValueError("native page provenance mismatch")
    if not isinstance(entry["requestBody"], str) or type(entry["requestBodyBytes"]) is not int:
        raise ValueError("native request type mismatch")
    request_raw = entry["requestBody"].encode("utf-8")
    digest = hashlib.sha256(request_raw).hexdigest()
    if len(request_raw) > 65536 or entry["requestBodyBytes"] != len(request_raw) or entry["requestBodySha256"] != digest:
        raise ValueError("native request hash mismatch")
    request = json.loads(request_raw, object_pairs_hook=unique_object, parse_constant=invalid_number)
    if not isinstance(request, dict) or set(request) != {"operationName", "query", "variables"} or request["operationName"] != "GET_LISTINGS_PUBLIC_POSTS" or request["query"] != QUERY:
        raise ValueError("native request operation mismatch")
    variables = request["variables"]
    if not isinstance(variables, dict) or set(variables) != {"offset", "limit", "filter"}:
        raise ValueError("native variables mismatch")
    offset, limit, filt = variables["offset"], variables["limit"], variables["filter"]
    if type(offset) is not int or not 0 <= offset <= 9007199254740991 or type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("native pagination mismatch")
    if not isinstance(filt, dict) or set(filt) != {"contentTypesIds", "sourcesIds", "locationsIds", "title"}:
        raise ValueError("native filter mismatch")
    ids = filt["sourcesIds"]
    if (filt["contentTypesIds"] != [4, 10] or filt["locationsIds"] != [] or filt["title"] != ""
            or not isinstance(ids, list) or not 1 <= len(ids) <= 40
            or any(type(x) is not int or not 0 < x <= 9007199254740991 for x in ids) or len(set(ids)) != len(ids)):
        raise ValueError("native source filter mismatch")
    if filename != f"page-{offset}-{hashlib.sha256(raw).hexdigest()}.json":
        raise ValueError("native page name mismatch")
    receipt = entry["receipt"]
    if (not isinstance(receipt, dict) or set(receipt) != acquisition.RECEIPT_KEYS
            or receipt.get("sourceKey") != "nai-global" or receipt.get("method") != "POST"
            or receipt.get("requestUrl") != "https://infabode.com/graphql"
            or receipt.get("finalUrl") != "https://infabode.com/graphql"
            or type(receipt.get("status")) is not int or not 100 <= receipt["status"] <= 599):
        raise ValueError("native receipt mismatch")
    try:
        body = base64.b64decode(entry["responseBodyBase64"], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError("native body encoding invalid") from exc
    if (base64.b64encode(body).decode() != entry["responseBodyBase64"]
            or type(receipt.get("bodyBytes")) is not int or receipt["bodyBytes"] != len(body)
            or len(body) > events.MAX_ARTIFACT_BYTES or receipt.get("bodySha256") != hashlib.sha256(body).hexdigest()):
        raise ValueError("native response hash mismatch")
    start, end = events.timestamp(receipt.get("startedAt")), events.timestamp(receipt.get("finishedAt"))
    if datetime.fromisoformat(start.replace("Z", "+00:00")) > datetime.fromisoformat(end.replace("Z", "+00:00")):
        raise ValueError("native clock mismatch")
    try:
        response, _ = acquisition.decode_json(body)
        rows = response.get("data", {}).get("publicPosts") if isinstance(response, dict) and isinstance(response.get("data"), dict) else None
    except (ValueError, TypeError, KeyError):
        return None, "invalid_json"
    context = {"requestBody": request, "requestBodySha256": digest,
               "pageFileSha256": hashlib.sha256(raw).hexdigest(), **binding}
    pointer = "/data/publicPosts" if receipt["status"] == 200 and isinstance(rows, list) else None
    observation = acquisition.convert(body, receipt=receipt, array_pointer=pointer, capture_context=context)
    return observation, None


def mark(run_dir, manifest, source, attempt):
    """Create a private attempt directory and durable marker before launch."""
    config = manifest["config"].get("automatic_source_evidence") or {}
    if not supported(source, config):
        return None
    relative = Path("source-evidence") / f"native-{source}-attempt-{attempt['number']}"
    parent = run_dir / "source-evidence"
    parent.mkdir(mode=0o700, exist_ok=True)
    parent_stat = parent.lstat()
    if not stat.S_ISDIR(parent_stat.st_mode) or parent_stat.st_uid != os.getuid() or parent_stat.st_mode & 0o077:
        raise ValueError("native evidence parent is not private")
    directory = run_dir / relative
    directory.mkdir(mode=0o700)
    attempt["native_source_evidence"] = {"state": "awaiting_artifact", "directory": str(relative), "pages": []}
    return directory


def settle(run_dir, manifest, source, attempt, save):
    """Scan a settled worker's immutable files, saving each page before continuing."""
    from source_evidence import automatic, cli
    config = manifest["config"].get("automatic_source_evidence") or {}
    state = attempt.get("native_source_evidence")
    if not state or state.get("state") != "awaiting_artifact":
        return
    expected = f"source-evidence/native-{source}-attempt-{attempt['number']}"
    try:
        if (not supported(source, config)
                or state.get("directory") != expected):
            raise ValueError("native attempt binding mismatch")
        automatic._verify_config(config, manifest["config"]["sources"])
        if manifest.get("collector_git_dirty") is not False or manifest.get("collector_git_sha") != config["collector_git_sha"]:
            raise ValueError("native collector identity mismatch")
        directory = run_dir / expected
        info = directory.lstat()
        if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("native directory is not private")
        entries = sorted(os.listdir(directory))
        names = [name for name in entries if not name.endswith(".json.plan")]
        if (any(name[:-5] not in names for name in entries if name.endswith(".json.plan"))
                or len(names) > config["max_native_pages"]
                or sum((directory / n).lstat().st_size for n in names) > 128 * 1024 * 1024):
            raise ValueError("native attempt exceeds bounds")
        known = {item["filename"] for item in state["pages"]}
        if not known.issubset(names):
            raise ValueError("sealed native page is missing")
        for name in names:
            if name in known:
                continue
            raw = automatic._read_private(directory / name, MAX_ENTRY_BYTES)
            binding = {"policySha256": config["policy_sha256"], "targetSha256": config["target_sha256"],
                       "collectorGitSha": config["collector_git_sha"],
                       "transportSha256": events.sha256(events.encoded(config["transport"])),
                       "generation": manifest["run_id"], "attempt": attempt["number"]}
            observation, diagnostic = _decode(source, raw, name, binding)
            if diagnostic:
                state["pages"].append({"state": "no_data", "filename": name,
                                       "file_sha256": hashlib.sha256(raw).hexdigest(), "reason": diagnostic})
                save(run_dir, manifest)
                continue
            if len(observation["records"]) > config["max_records"]:
                raise ValueError("native record limit exceeded")
            automatic._refuse_credential_fields(observation)
            plan = events.plan(observation, source_key=source)
            events.validate_content_policy(plan)
            if len(events.encoded(plan)) + 1 > cli.MAX_PLAN_BYTES:
                raise ValueError("native plan exceeds bound")
            plan_name = name + ".plan"
            plan_path = directory / plan_name
            automatic._write_exact(plan_path, events.encoded(plan) + b"\n", cli.MAX_PLAN_BYTES)
            state["pages"].append({"state": "pending", "filename": name, "plan_name": plan_name,
                                   "file_sha256": hashlib.sha256(raw).hexdigest(),
                                   "plan_sha256": plan["plan_sha256"],
                                   "plan_file_sha256": hashlib.sha256(plan_path.read_bytes()).hexdigest(),
                                   "capture_id": plan["events"][0]["capture_id"]})
            save(run_dir, manifest)
        state["state"] = "settled" if state["pages"] else "no_data"
    except (OSError, ValueError, TypeError, KeyError, RuntimeError) as exc:
        state["state"] = "refused"
        state["reason"] = type(exc).__name__
        manifest["automatic_source_evidence_paused"] = True
        manifest["automatic_source_evidence_status"] = "incomplete"
    save(run_dir, manifest)


def prepare_job(run_dir, manifest, source, attempt, page):
    """Revalidate the complete sealed page and plan immediately before append."""
    from source_evidence import automatic, cli
    config = manifest["config"]["automatic_source_evidence"]
    container = attempt["native_source_evidence"]
    expected = f"source-evidence/native-{source}-attempt-{attempt['number']}"
    if (not supported(source, config)
            or container["directory"] != expected
            or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != config["collector_git_sha"]):
        raise ValueError("native attempt path mismatch")
    pages = container.get("pages")
    if (not isinstance(pages, list) or len(pages) > config["max_native_pages"]
            or any(not isinstance(item, dict) or not isinstance(item.get("filename"), str) for item in pages)
            or len({item["filename"] for item in pages}) != len(pages)):
        raise ValueError("native page manifest exceeds bound or has duplicates")
    name = page["filename"]
    if Path(name).name != name or page["plan_name"] != name + ".plan":
        raise ValueError("native page path mismatch")
    directory = run_dir / expected
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        raise ValueError("native directory is not private")
    raw = automatic._read_private(directory / name, MAX_ENTRY_BYTES)
    if hashlib.sha256(raw).hexdigest() != page["file_sha256"]:
        raise ValueError("native page changed")
    binding = {"policySha256": config["policy_sha256"], "targetSha256": config["target_sha256"],
               "collectorGitSha": config["collector_git_sha"],
               "transportSha256": events.sha256(events.encoded(config["transport"])),
               "generation": manifest["run_id"], "attempt": attempt["number"]}
    observation, diagnostic = _decode(source, raw, name, binding)
    if diagnostic or len(observation["records"]) > config["max_records"]:
        raise ValueError("native page is no longer admissible")
    automatic._refuse_credential_fields(observation)
    expected_plan = events.plan(observation, source_key=source)
    events.validate_content_policy(expected_plan)
    sealed = automatic._read_private(directory / page["plan_name"], cli.MAX_PLAN_BYTES)
    if hashlib.sha256(sealed).hexdigest() != page["plan_file_sha256"]:
        raise ValueError("native plan file changed")
    plan = json.loads(sealed.decode("utf-8"), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (plan != expected_plan or page["plan_sha256"] != plan["plan_sha256"]
            or page["capture_id"] != plan["events"][0]["capture_id"]):
        raise ValueError("native plan binding mismatch")
    return plan
