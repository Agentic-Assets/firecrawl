"""Source-bound Marcus map/tile inventory; detail observations stay unverified."""
from __future__ import annotations

import base64
import json
import math
import re
from datetime import datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

SOURCE = "marcus-millichap"
BASE = "https://www.marcusmillichap.com"
_VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"}


def _time(value):
    if not isinstance(value, str):
        raise ValueError("Marcus observation time missing")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Marcus observation time invalid") from exc
    if result.tzinfo is None:
        raise ValueError("Marcus observation time invalid")
    return result


class _Tile(HTMLParser):
    """Read only the first source tile's identity, link and title."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.depth = 0
        self.finished = False
        self.attrs = None
        self.href = None
        self.title_depth = 0
        self.title_seen = False
        self.title_parts = []

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        if self.attrs is None and "mm-tile" in (attrs.get("class") or "").split():
            self.attrs = attrs
            self.depth = 1
            return
        if self.depth == 0 or self.finished:
            return
        if tag == "a" and self.href is None and "/properties/" in (attrs.get("href") or ""):
            self.href = attrs["href"]
        if tag == "h2" and not self.title_seen:
            self.title_seen = True
            self.title_depth = self.depth + 1
        if tag not in _VOID:
            self.depth += 1

    def handle_endtag(self, tag):
        if not self.depth or self.finished or tag in _VOID:
            return
        if self.title_depth == self.depth:
            self.title_depth = 0
        self.depth -= 1
        if not self.depth:
            self.finished = True

    def handle_data(self, data):
        if self.title_depth and self.depth >= self.title_depth:
            self.title_parts.append(data)


def _source_url(value, native):
    if not isinstance(value, str) or not value:
        raise ValueError("Marcus source URL missing")
    url = urljoin(BASE, value)
    parsed = urlsplit(url)
    if (parsed.scheme != "https" or parsed.netloc != "www.marcusmillichap.com"
            or parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None
            or not re.fullmatch(r"/properties/" + re.escape(native) + r"/[^/]+/?", parsed.path)):
        raise ValueError("Marcus source URL differs from native ID")
    return url


def stage_inventory(listing, generation, started_at, finished_at):
    """Re-derive map/tile identity without trusting fetched detail attribution."""
    if not isinstance(listing, dict) or listing.get("sourceKey") != SOURCE:
        raise ValueError("Marcus source differs")
    native, activity = listing.get("id"), listing.get("activityId")
    if not isinstance(native, str) or not re.fullmatch(r"[1-9][0-9]*", native):
        raise ValueError("Marcus deal ID invalid")
    if not isinstance(activity, str) or not activity or activity != activity.strip():
        raise ValueError("Marcus activity ID invalid")
    raw = listing.get("rawMarcusSearchRow")
    if not isinstance(raw, dict) or not isinstance(raw.get("rawMarcusMapDetail"), dict):
        raise ValueError("Marcus source map evidence missing")
    detail = raw["rawMarcusMapDetail"]
    html = detail.get("PropertyDetail")
    if not isinstance(html, str):
        raise ValueError("Marcus source tile missing")
    parser = _Tile()
    parser.feed(html)
    attrs = parser.attrs or {}
    if (not parser.finished or attrs.get("data-dealid") != native
            or attrs.get("data-activityid") != activity
            or raw.get("ActivityId") != activity or detail.get("ActivityId") != activity):
        raise ValueError("Marcus source inventory identity differs")
    url = _source_url(listing.get("url"), native)
    if any(_source_url(value, native) != url for value in
           (raw.get("PropertyUrl"), detail.get("PropertyUrl"), parser.href)):
        raise ValueError("Marcus source inventory URL differs")
    provenance = listing.get("freshnessProvenance")
    if not isinstance(provenance, dict) or provenance.get("generationId") != generation:
        raise ValueError("Marcus source generation differs")
    inventory = _time(listing.get("inventoryObservedAt"))
    if not _time(started_at) <= inventory <= _time(finished_at):
        raise ValueError("Marcus inventory observation outside source attempt")
    title = " ".join(" ".join(parser.title_parts).split()) or None
    if title is not None and ("\x00" in title or any(0xD800 <= ord(c) <= 0xDFFF for c in title)):
        title = None
    latitude, longitude = raw.get("Latitude"), raw.get("Longitude")
    try:
        if isinstance(latitude, bool) or isinstance(longitude, bool):
            raise ValueError("invalid coordinates")
        latitude, longitude = float(latitude), float(longitude)
        if (not math.isfinite(latitude) or not math.isfinite(longitude)
                or not -90 <= latitude <= 90 or not -180 <= longitude <= 180
                or (latitude == 0 and longitude == 0)):
            raise ValueError("invalid coordinates")
    except (TypeError, ValueError, OverflowError):
        latitude, longitude = None, None
    try:
        observation = json.dumps(listing, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("Marcus source observation cannot be retained") from exc
    return {"external_id": native, "activity_id": activity, "source_url": url,
            "inventory_observed_at": inventory.isoformat(), "title": title,
            "lat": latitude, "lng": longitude, "detail_qualified": False,
            "detail_error": bool(listing.get("detailError")),
            "observation_base64": base64.b64encode(observation.encode("ascii")).decode("ascii")}


def partition_inventory(listings, generation, started_at, finished_at):
    """Keep identifiable peers, holding all witnessed native-ID/URL collisions."""
    if not isinstance(listings, list):
        raise ValueError("Marcus inventory partition invalid")
    witnesses = {"id": {}, "activity": {}, "url": {}}
    staged, held = [], []
    for index, listing in enumerate(listings):
        if isinstance(listing, dict):
            raw = listing.get("rawMarcusSearchRow")
            raw = raw if isinstance(raw, dict) else {}
            detail = raw.get("rawMarcusMapDetail")
            detail = detail if isinstance(detail, dict) else {}
            tile = _Tile()
            try:
                tile.feed(detail.get("PropertyDetail") or "")
            except (TypeError, ValueError):
                tile = _Tile()
            attrs = tile.attrs or {}
            for kind, values in (("id", [listing.get("id"), attrs.get("data-dealid")]),
                                 ("activity", [listing.get("activityId"), raw.get("ActivityId"),
                                               detail.get("ActivityId"), attrs.get("data-activityid")]),
                                 ("url", [listing.get("url"), raw.get("PropertyUrl"),
                                          detail.get("PropertyUrl"), tile.href])):
                seen = set()
                for value in values:
                    if not isinstance(value, str) or not value:
                        continue
                    try:
                        seen.add(urljoin(BASE, value).rstrip("/") if kind == "url" else value)
                    except ValueError:
                        continue
                for value in seen:
                    witnesses[kind][value] = witnesses[kind].get(value, 0) + 1
        try:
            staged.append((index, stage_inventory(listing, generation, started_at, finished_at)))
        except (TypeError, ValueError, KeyError, RecursionError):
            held.append({"index": index, "reason": "unqualified_inventory_identity"})
    accepted = []
    for index, row in staged:
        if any(witnesses[kind].get(row[key].rstrip("/") if kind == "url" else row[key], 0) > 1 for kind, key in
               (("id", "external_id"), ("activity", "activity_id"), ("url", "source_url"))):
            held.append({"index": index, "reason": "conflicting_inventory_identity"})
        else:
            accepted.append(row)
    return {"staged": accepted, "held": sorted(held, key=lambda value: value["index"])}


def rederive_settled_artifact(run_dir, manifest, attempt_number, current_code_sha, current_git_dirty):
    """Read one exact rejected artifact; caller owns the manifest, lock and settlement.

    This is source custody and rederivation only. It grants neither DB admission
    nor permission to treat the source or any detail response as complete.
    """
    import hashlib
    from datetime import UTC, timedelta
    from pathlib import Path

    from cre_colliers_partial_checkpoint import _read_private_file

    run_dir = Path(run_dir)
    if (not isinstance(manifest, dict) or manifest.get("run_id") != run_dir.name
            or not isinstance(current_code_sha, str) or not re.fullmatch(r"[a-f0-9]{40}", current_code_sha)
            or current_git_dirty is not False or manifest.get("collector_git_dirty") is not False
            or manifest.get("collector_git_sha") != current_code_sha
            or type(attempt_number) is not int or attempt_number < 1):
        raise ValueError("Marcus source owner changed")
    target = (manifest.get("preflight") or {}).get("database_target")
    if (not isinstance(target, dict) or target.get("algorithm") != "sha256"
            or not isinstance(target.get("value"), str) or not re.fullmatch(r"[a-f0-9]{64}", target["value"])):
        raise ValueError("Marcus source target invalid")
    checkpoint = (manifest.get("sources") or {}).get(SOURCE) or {}
    attempts = checkpoint.get("attempts")
    if not isinstance(attempts, list) or not attempts or any(
            not isinstance(item, dict) or type(item.get("number")) is not int for item in attempts):
        raise ValueError("Marcus source attempts invalid")
    matched = [item for item in attempts if item["number"] == attempt_number]
    if len(matched) != 1 or attempt_number != max(item["number"] for item in attempts):
        raise ValueError("Marcus source attempt differs")
    attempt = matched[0]
    relative = f"rejected/{SOURCE}-attempt-{attempt_number}.json"
    digest = attempt.get("rejected_artifact_sha256")
    started, finished = _time(attempt.get("started_at")), _time(attempt.get("finished_at"))
    if (type(attempt.get("rc")) is not int or attempt["rc"] != 0
            or attempt.get("error") != f"{SOURCE}/sale freshness metric detailErrors must be zero"
            or attempt.get("rejected_artifact") != relative
            or not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest)
            or started < _time(manifest.get("started_at")) or finished < started
            or finished > datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("Marcus source attempt unsettled or ineligible")
    path = run_dir / relative
    size = path.lstat().st_size
    raw = _read_private_file(path, size)
    if len(raw) != size or hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("Marcus source artifact changed")
    data = json.loads(raw)
    meta = data.get("runMeta") if isinstance(data, dict) else None
    if not isinstance(meta, dict) or not isinstance(meta.get("freshness"), dict):
        raise ValueError("Marcus source run metadata missing")
    child_started, child_finished = _time(meta.get("startedAt")), _time(meta.get("finishedAt"))
    if (meta["freshness"].get("generationId") != run_dir.name
            or _time(meta["freshness"].get("generationStartedAt")) != _time(manifest["started_at"])
            or child_started < started or child_finished < child_started
            or child_finished - finished > timedelta(minutes=5)
            or child_finished > datetime.now(UTC) + timedelta(minutes=5)):
        raise ValueError("Marcus source clock discrepancy exceeds bound")
    rows, sources = data.get("listings"), data.get("sources")
    if (not isinstance(rows, list) or not isinstance(sources, list)
            or type(data.get("totalListings")) is not int or data["totalListings"] != len(rows)):
        raise ValueError("Marcus source artifact shape invalid")
    sale = [item for item in sources if isinstance(item, dict)
            and item.get("sourceKey") == SOURCE and item.get("transaction") == "sale"]
    if (len(sale) != 1 or any(not isinstance(item, dict) or item.get("sourceKey") != SOURCE for item in sources)
            or type(sale[0].get("listingsCollected")) is not int
            or sale[0]["listingsCollected"] != len(rows)
            or not isinstance(sale[0].get("freshness"), dict)
            or type(sale[0]["freshness"].get("detailErrors")) is not int
            or sale[0]["freshness"]["detailErrors"] < 1
            or sale[0]["freshness"]["detailErrors"] != sum(bool(row.get("detailError")) for row in rows if isinstance(row, dict))):
        raise ValueError("Marcus source outcome differs")
    partition = partition_inventory(rows, run_dir.name, meta["startedAt"], meta["finishedAt"])
    return {"schema": "marcus-inventory-custody/v1", "source": SOURCE, "generation": run_dir.name,
            "attempt": attempt_number, "generation_started_at": manifest["started_at"],
            "source_sha256": digest, "source_bytes": size,
            "producer_sha": current_code_sha, "target_sha256": target["value"],
            "started_at": meta["startedAt"], "finished_at": meta["finishedAt"],
            "coordinator_finished_at": attempt["finished_at"], "detail_qualified": 0,
            "detail_errors": sale[0]["freshness"]["detailErrors"], "coverage": "partial",
            "partition": partition}


def candidate_data(source):
    """Project the rederived inventory as explicitly partial, never fresh detail."""
    from cre_marcus_inventory_db import validate_staged

    if not isinstance(source, dict) or source.get("schema") != "marcus-inventory-custody/v1":
        raise ValueError("invalid Marcus inventory source")
    partition = source.get("partition")
    if not isinstance(partition, dict) or not isinstance(partition.get("held"), list):
        raise ValueError("invalid Marcus inventory partition")
    generation, start, finish = source["generation"], source["started_at"], source["finished_at"]
    staged = partition.get("staged")
    validate_staged(staged, generation, start, finish)
    listings = [{"sourceKey": SOURCE, "id": row["external_id"], "url": row["source_url"],
                 "marcusInventoryRecord": json.loads(base64.b64decode(row["observation_base64"], validate=True))}
                for row in staged]
    return {"schema": "marcus-inventory-partial/v1", "sourceKey": SOURCE,
            "binding": {"generation": generation, "attempt": source["attempt"], "startedAt": start,
                        "finishedAt": finish, "coordinatorFinishedAt": source["coordinator_finished_at"],
                        "sourceSha256": source["source_sha256"], "sourceBytes": source["source_bytes"],
                        "producerSha": source["producer_sha"], "targetSha256": source["target_sha256"]},
            "coverage": "partial_unknown", "held": partition["held"],
            "detailQualified": 0, "detailErrors": source["detail_errors"],
            "runMeta": {"mode": "partial", "transactions": ["sale", "lease"], "startedAt": start,
                        "finishedAt": finish, "freshness": {"generationId": generation,
                        "generationStartedAt": source["generation_started_at"], "requireFreshDetails": False}},
            "sources": [{"sourceKey": SOURCE, "transaction": "sale", "supported": True, "truncated": True,
                         "listingsCollected": len(listings), "error": "original source details unverified"},
                        {"sourceKey": SOURCE, "transaction": "lease", "supported": False,
                         "truncated": True, "listingsCollected": 0}],
            "listings": listings, "brokers": [], "totalListings": len(listings)}
