"""Preserve selected integral float representations through JSONB readback."""
import copy
import math
from decimal import Decimal, InvalidOperation

from source_evidence import acquisition, events

ENCODING = "json_pointer_to_python_repr_v1"
MAX_NODES = 500000
MAX_DEPTH = 128
MAX_WITNESSES = 10000
MAX_MAP_BYTES = 1_000_000


def _candidate(value):
    return type(value) is float and math.isfinite(value) and value.is_integer() and "e" in repr(value)


def collect(document, exceptional=None):
    """Collect canonical repr for scientific-notation integral floats only."""
    pending = [(document, "", 0)]
    result = {}
    visited = 0
    while pending:
        value, pointer, depth = pending.pop()
        visited += 1
        if visited > MAX_NODES or depth > MAX_DEPTH:
            raise ValueError("numeric witness traversal exceeds bound")
        if _candidate(value):
            acquisition._pointer_parts(pointer)
            result[pointer] = repr(value)
            if len(result) > MAX_WITNESSES:
                raise ValueError("numeric witness count exceeds bound")
        elif isinstance(value, dict):
            if len(value) + len(pending) + visited > MAX_NODES:
                raise ValueError("numeric witness traversal exceeds bound")
            pending.extend((child, acquisition._path(pointer, key), depth + 1) for key, child in value.items())
        elif isinstance(value, list):
            if len(value) + len(pending) + visited > MAX_NODES:
                raise ValueError("numeric witness traversal exceeds bound")
            pending.extend((child, acquisition._path(pointer, index), depth + 1) for index, child in enumerate(value))
    if exceptional is not None and (not isinstance(exceptional, dict) or set(result) & set(exceptional)):
        raise ValueError("numeric witnesses overlap exceptional literals")
    if len(events.encoded(result)) > MAX_MAP_BYTES:
        raise ValueError("numeric witness map exceeds bound")
    return result


def restore_value(value, literal):
    """Validate a witness against the stored number and restore its float type."""
    if (not isinstance(literal, str) or len(literal) > 32 or type(value) not in (int, float)
            or (type(value) is float and not math.isfinite(value))):
        raise ValueError("invalid numeric witness")
    try:
        number = float(literal)
        if (not _candidate(number) or repr(number) != literal
                or Decimal(str(value)) != Decimal(literal)):
            raise ValueError("numeric witness disagrees with stored value")
    except (ValueError, OverflowError, InvalidOperation) as exc:
        raise ValueError("numeric witness disagrees with stored value") from exc
    return number


def restore(document, witnesses, exceptional=None):
    """Restore only witnessed pointers and validate the complete witness map."""
    if (not isinstance(witnesses, dict) or not witnesses or len(witnesses) > MAX_WITNESSES
            or len(events.encoded(witnesses)) > MAX_MAP_BYTES
            or (exceptional is not None and not isinstance(exceptional, dict))
            or (exceptional is not None and set(witnesses) & set(exceptional))):
        raise ValueError("invalid numeric witness map")
    restored = copy.deepcopy(document)
    for pointer, literal in witnesses.items():
        parts = acquisition._pointer_parts(pointer)
        if not parts:
            raise ValueError("collector numeric witness cannot target root")
        node = restored
        for part in parts[:-1]:
            node = node[acquisition._child(node, part)]
        key = acquisition._child(node, parts[-1])
        node[key] = restore_value(node[key], literal)
    if collect(restored, exceptional) != witnesses:
        raise ValueError("numeric witness map is incomplete or noncanonical")
    return restored
