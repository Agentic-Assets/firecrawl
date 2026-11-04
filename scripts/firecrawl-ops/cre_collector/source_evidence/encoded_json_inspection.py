"""Bounded inspection of JSON embedded inside source strings."""
import json

from source_evidence import acquisition


def _inspect_encoded_credentials(document, *, decoded_callback=None, inspect_keys=False):
    """Apply existing credential policy to bounded JSON embedded in strings.

    This does not assert general secret detection. Values are never transformed.
    """
    from source_evidence.automatic import _refuse_credential_fields

    max_nodes = 500000
    max_depth = 128
    max_encoded_depth = 16
    max_bytes = 16 * 1024 * 1024
    pending = [(document, 0, 0)]
    visited = scanned_bytes = 0
    _refuse_credential_fields(document)
    while pending:
        value, depth, encoded_depth = pending.pop()
        visited += 1
        if visited > max_nodes or depth > max_depth:
            raise ValueError('Pyramid encoded credential inspection exceeds limit')
        if isinstance(value, dict):
            pending.extend((child, depth + 1, encoded_depth) for child in value.values())
            if inspect_keys:
                pending.extend((key, depth + 1, encoded_depth) for key in value)
        elif isinstance(value, list):
            pending.extend((child, depth + 1, encoded_depth) for child in value)
        elif isinstance(value, str):
            candidate = value.strip()
            if not candidate or candidate[0] not in '{["':
                continue
            encoded = candidate.encode('utf8')
            scanned_bytes += len(encoded)
            if scanned_bytes > max_bytes:
                raise ValueError('Pyramid encoded credential inspection exceeds limit')
            try:
                json.loads(candidate)
            except json.JSONDecodeError:
                continue
            except (RecursionError, OverflowError, ValueError) as exc:
                raise ValueError('Pyramid encoded credential inspection uncertain') from exc
            if encoded_depth >= max_encoded_depth:
                raise ValueError('Pyramid encoded credential inspection exceeds limit')
            try:
                decoded, _ = acquisition.decode_json(encoded)
            except (ValueError, RecursionError) as exc:
                raise ValueError('Pyramid encoded credential inspection uncertain') from exc
            _refuse_credential_fields(decoded)
            if decoded_callback is not None:
                decoded_callback(decoded)
            pending.append((decoded, depth + 1, encoded_depth + 1))
