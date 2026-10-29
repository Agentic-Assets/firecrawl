"""Bounded source-owned projections for private native-feed captures.

Withholding receipts attest to a local projection; they do not authenticate a
publisher. Original input and hashes remain in private custody.
"""
import copy
import json

from source_evidence import acquisition, events

MAX_NODES = 500_000
MAX_DEPTH = 128
MAX_ROWS = 10_000
MAX_RECEIPTS_BYTES = 1_000_000


def _bounded(value):
    stack = [(value, 0)]; count = 0
    while stack:
        item, depth = stack.pop(); count += 1
        if count > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError('native feed projection exceeds structural bound')
        if isinstance(item, dict):
            stack.extend((x, depth + 1) for x in item.values())
        elif isinstance(item, list):
            stack.extend((x, depth + 1) for x in item)


def project(document, literals, *, source):
    """Project recognized shapes without mutating the original or moving rows."""
    if source not in ('srs', 'kidder-mathews', 'hanley') or not isinstance(literals, dict):
        raise ValueError('unsupported native feed source')
    _bounded(document)
    result = copy.deepcopy(document); removed = []
    if source == 'hanley':
        if not isinstance(result, list) or len(result) > MAX_ROWS:
            raise ValueError('Hanley array shape unresolved')
        for i, row in enumerate(result):
            visible = row.get('visibility') if isinstance(row, dict) else None
            if not isinstance(visible, str) or visible.strip().casefold() != 'public':
                result[i] = None
                removed.append({'pointer': f'/{i}', 'reason': 'nonpublic_or_unknown_visibility'})
    elif source == 'srs':
        if isinstance(result, dict) and isinstance(result.get('properties'), list) and len(result['properties']) > MAX_ROWS:
            raise ValueError('SRS array exceeds bound')
    elif isinstance(result, dict) and isinstance(result.get('results'), list) and len(result['results']) > MAX_ROWS:
        raise ValueError('Kidder array exceeds bound')
    paths = [x['pointer'] for x in removed]
    projected_literals = {k: v for k, v in literals.items() if not any(k == p or k.startswith(p + '/') for p in paths)}
    projected = acquisition.encode_json(json.loads(events.encoded(result)), projected_literals).encode('utf8')
    if len(projected) > events.MAX_ARTIFACT_BYTES or len(events.encoded(removed)) > MAX_RECEIPTS_BYTES:
        raise ValueError('native feed projected output exceeds bound')
    return result, projected_literals, {'schema': 'cre-native-feed-projection/v1', 'sourceKey': source,
                                     'withheld': removed, 'projectedCanonicalSha256': events.sha256(projected)}
