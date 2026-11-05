"""Bounded local bridge from immutable Franklin artifact bytes to staged cards."""
import hashlib
import json
import re
import shutil
from pathlib import Path

from cre_franklin_site_sql import stage
from source_evidence.psql_process import run

ROOT = Path(__file__).resolve().parent
MAX_BRIDGE_BYTES = 72 * 1024 * 1024
MAX_PROJECTION_BYTES = 32 * 1024 * 1024


def project(raw, generation_id, env):
    """Reproject the exact input bytes locally; this is not database admission."""
    if (not isinstance(raw, bytes) or not raw
            or not isinstance(generation_id, str)
            or not re.fullmatch(r"[A-Za-z0-9._-]{1,128}", generation_id)
            or not isinstance(env, dict)):
        raise ValueError("invalid Franklin projection input")
    node = shutil.which("node", path=env.get("PATH"))
    loader = ROOT / "node_modules" / "tsx" / "dist" / "loader.mjs"
    bridge = ROOT / "sources" / "franklin-site-bridge.ts"
    if not node or not loader.is_file() or not bridge.is_file():
        raise ValueError("Franklin projection runtime unavailable")
    original = json.loads(raw)
    if (not isinstance(original, dict) or not isinstance(original.get("sources"), list)
            or not isinstance(original.get("listings"), list) or not isinstance(original.get("brokers"), list)
            or original.get("totalListings") != len(original["listings"])):
        raise ValueError("Franklin original artifact invalid")
    sources = original["sources"]
    if (not sources or any(not isinstance(entry, dict) or entry.get("sourceKey") != "franklin-street"
                           or entry.get("transaction") not in ("sale", "lease") for entry in sources)
            or len({entry["transaction"] for entry in sources}) != len(sources)):
        raise ValueError("Franklin original source scope invalid")
    successful = {entry["transaction"] for entry in sources if not entry.get("error")}
    for row in original["listings"]:
        if (not isinstance(row, dict) or row.get("sourceKey") != "franklin-street"
                or row.get("transactionMode") not in successful):
            raise ValueError("Franklin Buildout peer scope invalid")
    transactions = []
    for entry in sources:
        if entry["transaction"] in successful:
            count = sum(row["transactionMode"] == entry["transaction"] for row in original["listings"])
            if entry.get("supported") is not True or entry.get("truncated") is not False or entry.get("listingsCollected") != count:
                raise ValueError("Franklin Buildout peer accounting invalid")
            transactions.append({"transaction": entry["transaction"], "listingsCollected": count})
    result = {"coverage": "partial_unknown", "generationId": generation_id,
              "listings": [], "held": [], "pages": []}
    for entry in sources:
        if not entry.get("error") or not isinstance(entry.get("retainedObservations"), list):
            continue
        observations = [item for item in entry["retainedObservations"]
                        if isinstance(item, dict) and item.get("schema") == "franklin-street-page-observation-v1"]
        if len(observations) != 1:
            raise ValueError("Franklin page count invalid")
        # Send only one failed transaction and no unrelated Buildout peers.
        # The exact original remains in private custody and is rebound below.
        page_input = json.dumps({"runMeta": original.get("runMeta"), "sources": [entry],
                                "listings": [], "brokers": [], "totalListings": 0},
                               ensure_ascii=False, separators=(",", ":")).encode()
        try:
            if len(page_input) > MAX_BRIDGE_BYTES:
                raise OverflowError("Franklin page projection input bound")
            output = run([node, "--import", loader.as_uri(), str(bridge), generation_id],
                         page_input, env, max_results_bytes=MAX_PROJECTION_BYTES)
        except OverflowError:
            # Do not turn a bounded transport refusal into whole-source loss.
            # Keep every original card private and allow the other page through.
            result["held"].extend({"transaction": entry["transaction"],
                "card": {"ordinal": ordinal, "reason": "projection_input_bound", "card": card}}
                for ordinal, card in enumerate(observations[0].get("cards") or []))
            continue
        except RuntimeError as exc:
            if str(exc) != "psql outcome unknown: stdout_limit":
                raise
            result["held"].extend({"transaction": entry["transaction"],
                "card": {"ordinal": ordinal, "reason": "projection_output_bound", "card": card}}
                for ordinal, card in enumerate(observations[0].get("cards") or []))
            continue
        partial = json.loads(output)
        if (not isinstance(partial, dict) or partial.get("coverage") != "partial_unknown"
                or partial.get("generationId") != generation_id
                or not isinstance(partial.get("listings"), list)
                or not isinstance(partial.get("held"), list) or not isinstance(partial.get("pages"), list)):
            raise ValueError("Franklin projection response invalid")
        for key in ("listings", "held", "pages"):
            result[key].extend(partial[key])
    by_id = {}
    for row in result["listings"]:
        by_id.setdefault(row["id"].split(":")[-1], set()).add(row["transactionMode"])
    result["identityUncertainty"] = {"siteToBuildout": "unresolved", "possibleDuplicateProperties": True,
        "reusedSiteIds": [{"id": key, "transactions": sorted(value)} for key, value in by_id.items() if len(value) > 1]}
    result["buildout"] = {"transactions": transactions}
    cards = {}
    for source in original["sources"]:
        if not isinstance(source, dict) or source.get("sourceKey") != "franklin-street":
            raise ValueError("Franklin original source invalid")
        tx = source.get("transaction")
        for observation in source.get("retainedObservations") or []:
            if isinstance(observation, dict) and observation.get("schema") == "franklin-street-page-observation-v1":
                if tx in cards or not isinstance(observation.get("cards"), list):
                    raise ValueError("Franklin original card inventory invalid")
                cards[tx] = observation["cards"]

    def restore(tx, reference):
        ordinal = reference.get("ordinal") if isinstance(reference, dict) else None
        if (tx not in cards or type(ordinal) is not int or not 0 <= ordinal < len(cards[tx])):
            raise ValueError("Franklin card reference invalid")
        return cards[tx][ordinal]

    for listing in result["listings"]:
        observation = listing.get("franklinSiteObservation")
        observation["card"] = restore(listing.get("transactionMode"), observation)
    for held in result["held"]:
        reference = held["card"]
        if "card" not in reference:
            reference["card"] = restore(held["transaction"], reference)
    result["buildout"]["listings"] = original["listings"]
    result["buildout"]["brokers"] = original["brokers"]
    result["buildout"]["pendingListings"] = len(original["listings"])
    result["buildout"]["disposition"] = "private_pending_admission"
    if result["listings"]:
        stage(result["listings"])
    return {"artifact_sha256": hashlib.sha256(raw).hexdigest(), "projection": result}