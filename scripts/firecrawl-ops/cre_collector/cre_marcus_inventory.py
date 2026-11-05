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
                        seen.add(urljoin(BASE, value) if kind == "url" else value)
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
        if any(witnesses[kind].get(row[key], 0) > 1 for kind, key in
               (("id", "external_id"), ("activity", "activity_id"), ("url", "source_url"))):
            held.append({"index": index, "reason": "conflicting_inventory_identity"})
        else:
            accepted.append(row)
    return {"staged": accepted, "held": sorted(held, key=lambda value: value["index"])}
