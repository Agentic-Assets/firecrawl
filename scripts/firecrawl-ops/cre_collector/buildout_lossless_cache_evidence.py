"""Private offline evidence from cached JSON, preserving repeated keys.

No network, database, source authorization, promotion, or collector integration.
Screening is incomplete; input and output remain sensitive, unreviewed data.
"""
import argparse
import hashlib
import json
from pathlib import Path

from buildout_cache_observation_pipeline import private_parent
from buildout_observation_artifact import (
    credential_label,
    read_bounded,
    sensitive_string,
    write_private,
)

MAX_INPUT = 16 * 1024 * 1024
MAX_OUTPUT = 32 * 1024 * 1024
MAX_DEPTH = 32
MAX_NODES = 500_000
MAX_NUMBER = 1000


class ObjectPairs(list):
    """Distinguishes an object from an array, including empty containers."""


class NumberToken(str):
    """Preserve the spelling of a JSON numeric token."""


def number(value):
    if len(value) > MAX_NUMBER:
        raise ValueError("numeric token exceeds bound")
    return NumberToken(value)


def invalid_number(_value):
    raise ValueError("non-finite number")


def decode(raw):
    if len(raw) > MAX_INPUT:
        raise ValueError("input exceeds bound")
    try:
        return json.loads(raw, object_pairs_hook=ObjectPairs, parse_int=number,
                          parse_float=number, parse_constant=invalid_number)
    except RecursionError as exc:
        raise ValueError("input nesting exceeds parser capacity") from exc


def unique_field(obj, field):
    if not isinstance(obj, ObjectPairs):
        return None
    matches = [(index, value) for index, (key, value) in enumerate(obj) if key == field]
    return matches[0] if len(matches) == 1 else None


def build(raw):
    root = decode(raw)
    nodes, quarantine = 0, []

    def blocked(path, reason):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES:
            raise ValueError("structure exceeds bound")
        quarantine.append({"ordinalPath": path, "reason": reason})
        return {"kind": "quarantined"}

    def visit(value, path, depth=0, attributes=False, inside_attribute=False, parent_name=None):
        nonlocal nodes
        nodes += 1
        if nodes > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("structure exceeds bound")
        if isinstance(value, ObjectPairs):
            entries = []
            for index, (key, item) in enumerate(value):
                child = [*path, index]
                if credential_label(key) or sensitive_string(key):
                    blocked(child, "credential-shaped key")
                    entries.append({"kind": "quarantined_entry"})
                    continue
                recognized = key == "index_attributes" or (key == "attributes" and parent_name == "buildoutInventoryObservation")
                entries.append({"key": key, "value": visit(item, child, depth + 1, attributes=recognized,
                                                               inside_attribute=inside_attribute, parent_name=key)})
            return {"kind": "object", "entries": entries}
        if isinstance(value, list):
            if attributes:
                children = []
                for index, item in enumerate(value):
                    child = [*path, index]
                    if not isinstance(item, list) or isinstance(item, ObjectPairs) or len(item) != 2 or not isinstance(item[0], str) or isinstance(item[0], NumberToken):
                        children.append(blocked(child, "malformed attribute pair"))
                    elif credential_label(item[0]) or sensitive_string(item[0]):
                        children.append(blocked(child, "credential-shaped attribute label"))
                    else:
                        children.append(visit(item, child, depth + 1, inside_attribute=True))
                return {"kind": "array", "items": children}
            if inside_attribute and value and isinstance(value[0], str) and not isinstance(value[0], NumberToken) and (credential_label(value[0]) or sensitive_string(value[0])):
                return blocked(path, "credential-shaped nested attribute label")
            return {"kind": "array", "items": [visit(item, [*path, index], depth + 1, inside_attribute=inside_attribute) for index, item in enumerate(value)]}
        if isinstance(value, NumberToken):
            return {"kind": "number", "token": str(value)}
        if isinstance(value, str):
            if sensitive_string(value):
                return blocked(path, "credential-shaped content")
            return {"kind": "string", "value": value}
        if value is None or isinstance(value, bool):
            return {"kind": "null"} if value is None else {"kind": "boolean", "value": value}
        raise TypeError("unsupported value")

    tree = visit(root, [])
    data = unique_field(root, "data")
    inventory = unique_field(data[1], "inventory") if data else None
    paths = []
    if inventory and isinstance(inventory[1], list) and not isinstance(inventory[1], ObjectPairs):
        paths = [[data[0], inventory[0], index] for index in range(len(inventory[1]))]
    document = {"schema": "buildout-lossless-cache-evidence-v1", "inputSha256": hashlib.sha256(raw).hexdigest(),
                "inputBytes": len(raw), "sourceAuthorization": "not_evaluated", "sourceCompleteness": "unverified",
                "identityResolution": "unresolved", "promotion": "not_authorized", "privateUnreviewed": True,
                "cacheBytesAuthority": "local_cache_not_original_provider_http", "tree": tree,
                "inventoryResolution": "unique_unverified" if inventory is not None and isinstance(inventory[1], list) and not isinstance(inventory[1], ObjectPairs) else "ambiguous_or_missing",
                "recordOrdinalPaths": paths, "quarantine": quarantine}
    output = (json.dumps(document, ensure_ascii=True, separators=(",", ":")) + "\n").encode("ascii")
    if len(output) > MAX_OUTPUT:
        raise ValueError("output exceeds bound; preserve original input")
    return output, {"records": len(paths), "quarantinedItems": len(quarantine), "inventoryResolution": document["inventoryResolution"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--out-dir", required=True, type=Path)
    args = parser.parse_args()
    raw = read_bounded(args.input, MAX_INPUT)
    result, summary = build(raw)
    private_parent(args.out_dir)
    args.out_dir.mkdir(mode=0o700)
    write_private(args.out_dir / "evidence.json", result)
    print(json.dumps(summary, sort_keys=True))


if __name__ == "__main__":
    main()
