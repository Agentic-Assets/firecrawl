"""Registered Buildout source coordinates for parsed-page observations."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from buildout_observation_artifact import invalid_number, unique_object


def _registered_sources():
    path = Path(__file__).resolve().parents[1] / "data" / "cre-buildout-sources.json"
    rows = json.loads(path.read_bytes(), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if not isinstance(rows, list) or len(rows) != 25:
        raise ValueError("Buildout source registry mismatch")
    result = {}
    for row in rows:
        if (not isinstance(row, list) or len(row) != 5
                or any(not isinstance(value, str) or not value for value in row)):
            raise ValueError("Buildout source registry row mismatch")
        key, company, plugin, url, host = row
        parsed = urlsplit(url)
        if (not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,127}", key)
                or not re.fullmatch(r"[a-f0-9]{40}", plugin) or not company.strip()
                or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port or parsed.query or parsed.fragment or parsed.hostname != host
                or key in result):
            raise ValueError("Buildout source registry identity mismatch")
        result[key] = plugin
    return result


def supported_feed_keys():
    """Return each registered source's explicit feed-key to plugin mapping."""
    result = {key: {key: plugin} for key, plugin in _registered_sources().items()}
    path = Path(__file__).resolve().parents[1] / "data" / "cre-buildout-core-feeds.json"
    rows = json.loads(path.read_bytes(), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if not isinstance(rows, list) or len(rows) != 4:
        raise ValueError("Buildout core feed registry mismatch")
    required = {("svn", "svn"), ("lee-associates", "lee-associates"),
                ("franklin-street", "franklin-street-sale"), ("franklin-street", "franklin-street-lease")}
    if any(key in result for key, _ in required):
        raise ValueError("Buildout core feed overlaps registered source keys")
    seen = set()
    for row in rows:
        if (not isinstance(row, list) or len(row) != 6
                or any(not isinstance(value, str) or not value for value in row)):
            raise ValueError("Buildout core feed row mismatch")
        key, company, plugin, url, host, feed_key = row
        parsed = urlsplit(url)
        identity = (key, feed_key)
        if (identity not in required or identity in seen or key in result and key != "franklin-street"
                or not re.fullmatch(r"[a-f0-9]{40}", plugin) or not company.strip()
                or parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
                or parsed.port or parsed.query or parsed.fragment or parsed.hostname != host):
            raise ValueError("Buildout core feed identity mismatch")
        seen.add(identity)
        result.setdefault(key, {})[feed_key] = plugin
    if seen != required or len(result) != 28:
        raise ValueError("Buildout core feed set mismatch")
    plugins = [plugin for feeds in result.values() for plugin in feeds.values()]
    if len(plugins) != 29 or len(set(plugins)) != len(plugins):
        raise ValueError("Buildout feed plugin identities overlap")
    return result


def supported_sources():
    """Preserve the source-to-plugin API for sources with one registered feed."""
    return {key: next(iter(feeds.values())) for key, feeds in supported_feed_keys().items() if len(feeds) == 1}


def scope_hash(source_key, inventory_sort, feed_key=None):
    sources = supported_feed_keys()
    if not isinstance(source_key, str) or source_key not in sources:
        raise ValueError("Buildout source is not registered")
    feeds = sources[source_key]
    if len(feeds) == 1:
        if feed_key is not None:
            raise ValueError("Buildout unique source cannot carry a feed key")
        plugin = next(iter(feeds.values()))
    else:
        if not isinstance(feed_key, str) or feed_key not in feeds:
            raise ValueError("Buildout source requires its exact feed key")
        plugin = feeds[feed_key]
    encoded = json.dumps([source_key, plugin, inventory_sort],
                         ensure_ascii=False, separators=(",", ":")).encode("utf8")
    return hashlib.sha256(encoded).hexdigest()
