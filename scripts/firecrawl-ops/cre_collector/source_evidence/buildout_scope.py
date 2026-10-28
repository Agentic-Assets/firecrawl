"""Registered Buildout source coordinates for parsed-page observations."""
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from buildout_observation_artifact import invalid_number, unique_object


def supported_sources():
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


def scope_hash(source_key, inventory_sort):
    sources = supported_sources()
    if not isinstance(source_key, str) or source_key not in sources:
        raise ValueError("Buildout source is not registered")
    encoded = json.dumps([source_key, sources[source_key], inventory_sort],
                         ensure_ascii=False, separators=(",", ":")).encode("utf8")
    return hashlib.sha256(encoded).hexdigest()
