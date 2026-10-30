"""Bounded public-fact construction and credential inspection helpers."""
import copy
import json
import re
from collections import ChainMap
from html import unescape
from urllib.parse import unquote

from buildout_observation_artifact import credential_label, sensitive_string
from source_evidence import events


class _InspectionLimit(ValueError):
    """A bounded per-record inspection could not complete."""


def _pointer(label):
    return '/' + label.replace('~', '~0').replace('/', '~1')


def _credential_label_encoded(key):
    candidate = key
    for _ in range(5):
        if credential_label(candidate) or sensitive_string(candidate):
            return True
        decoded = unquote(unescape(candidate), errors='replace')
        if decoded == candidate:
            return False
        candidate = decoded
    # An unresolved nested encoding is not qualified for publication.
    return bool(re.search(r'%[0-9a-fA-F]{2}|&(?:[a-zA-Z][a-zA-Z0-9]+|#(?:x[0-9a-fA-F]+|[0-9]+));', candidate))


def _collect_credential_strings(value, removed, numeric_removed):
    pending = [value]
    seen = 0
    while pending:
        node = pending.pop()
        seen += 1
        if seen > 20000:
            raise _InspectionLimit('Newmark credential subtree exceeds bound')
        if isinstance(node, dict):
            pending.extend(node.values())
        elif isinstance(node, list):
            pending.extend(node)
        elif type(node) in (int, float):
            numeric_removed.append(str(node))
        elif isinstance(node, str) and node:
            removed.append(node)
            if len(removed) > 1000:
                raise _InspectionLimit('Newmark credential count exceeds bound')


def _encoded_json_credential(value, budget, removed, numeric_removed, depth=0):
    budget[0] += 1
    if budget[0] > 20000 or depth > 16:
        raise _InspectionLimit('Newmark encoded JSON inspection exceeds bound')
    if isinstance(value, dict):
        found = False
        for key, child in value.items():
            if _credential_label_encoded(key):
                _collect_credential_strings(child, removed, numeric_removed)
                found = True
            else:
                found = _encoded_json_credential(child, budget, removed, numeric_removed, depth + 1) or found
        return found
    if isinstance(value, list):
        found = False
        for child in value:
            found = _encoded_json_credential(child, budget, removed, numeric_removed, depth + 1) or found
        return found
    if isinstance(value, str):
        candidate = value.strip()
        if not candidate or candidate[0] not in '{["':
            return False
        try:
            parsed = json.loads(candidate)
        except (ValueError, RecursionError):
            return False
        return _encoded_json_credential(parsed, budget, removed, numeric_removed, depth + 1)
    return False


def _sensitive_encoded_string(value):
    candidate = value
    for _ in range(5):
        if sensitive_string(candidate):
            return True
        following = unquote(unescape(candidate), errors='replace')
        if following == candidate:
            return False
        candidate = following
    return True


def _sanitize_encoded_labels(data, numeric, *, allow_label=None, inspect_strings=False, removed_strings=()):
    """Remove encoded credential labels, retain safe siblings and array order."""
    data = copy.deepcopy(data)
    changes, removed, numeric_removed, nodes, encoded_budget = [], list(removed_strings), [], [0], [0]
    if len(removed) > 1000 or any(not isinstance(value, str) or not value or len(value) > 4096 for value in removed):
        raise _InspectionLimit('Public credential seed exceeds bound')

    def visit(value, path, depth):
        nodes[0] += 1
        if nodes[0] > 20000 or depth > 32:
            raise _InspectionLimit('Newmark public credential inspection exceeds bound')
        if isinstance(value, dict):
            for key in list(value):
                child_path = path + _pointer(key)
                if _credential_label_encoded(key) and not (allow_label and allow_label(child_path, key)):
                    secret = value.pop(key)
                    _collect_credential_strings(secret, removed, numeric_removed)
                    numeric_removed.extend(literal for number_path, literal in numeric.items()
                                           if number_path == child_path or number_path.startswith(child_path + "/"))
                    changes.append((child_path, 'credential_key_removed'))
                else:
                    value[key] = visit(value[key], child_path, depth + 1)
        elif isinstance(value, list):
            for i, child in enumerate(value):
                value[i] = visit(child, path + '/' + str(i), depth + 1)
        elif isinstance(value, str) and ((inspect_strings and _sensitive_encoded_string(value))
                                          or _encoded_json_credential(value, encoded_budget, removed, numeric_removed)):
            changes.append((path, 'unverified_content_omitted'))
            return None
        return value

    data = visit(data, '', 0)
    # Detect recognizable copies of removed values in otherwise safe strings.
    if removed or numeric_removed:
        work = [0]
        numeric_patterns = [re.compile(r'(?<![A-Za-z0-9_.])' + re.escape(token) + r'(?![A-Za-z0-9_.])')
                            for token in set(numeric_removed)]
        def contains_copy(value):
            candidate = value
            for _ in range(5):
                work[0] += len(candidate) * max(1, len(removed) + len(numeric_removed))
                if work[0] > 128 * 1024 * 1024:
                    raise _InspectionLimit('Newmark credential-copy inspection exceeds bound')
                if (any(secret in candidate for secret in removed)
                        or any(pattern.search(candidate) for pattern in numeric_patterns)):
                    return True
                following = unquote(unescape(candidate), errors='replace')
                if following == candidate:
                    break
                candidate = following
            return False

        def redact(value, path, depth):
            if depth > 32:
                raise _InspectionLimit('Newmark credential-copy inspection exceeds bound')
            if isinstance(value, dict):
                for key in list(value):
                    if contains_copy(key):
                        value.pop(key)
                        changes.append((path + _pointer(key), 'credential_key_removed'))
                    else:
                        value[key] = redact(value[key], path + _pointer(key), depth + 1)
            elif isinstance(value, list):
                for i, child in enumerate(value):
                    value[i] = redact(child, path + '/' + str(i), depth + 1)
            elif isinstance(value, str):
                if contains_copy(value):
                    changes.append((path, 'unverified_content_omitted'))
                    return None
            elif ((path in numeric and numeric[path] in numeric_removed)
                  or (type(value) in (int, float) and str(value) in numeric_removed)):
                changes.append((path, 'unverified_content_omitted'))
                return None
            return value
        data = redact(data, '', 0)
    numeric = {p: literal for p, literal in numeric.items()
               if not any(p == path or p.startswith(path + '/') for path, _ in changes)}
    return data, numeric, changes


def _within(value, *, budget):
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        budget[0] += 1
        if budget[0] > 20000 or depth > 32:
            return False
        if isinstance(item, str) and len(item) > 16384:
            return False
        if isinstance(item, dict):
            if any(not isinstance(k, str) or len(k) > 16384 for k in item):
                return False
            pending.extend((child, depth + 1) for child in item.values())
        elif isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
    return True


def _build_facts(data, numeric, omissions):
    facts = []
    budget = [0]
    total_bytes = 0
    for label, value in data.items():
        path = _pointer(label)
        relatives = {p[len(path):]: literal for p, literal in numeric.items()
                     if p == path or p.startswith(path + '/')}
        fact = {'label': label, 'value': value, 'sourcePath': path}
        if relatives:
            fact['numericLiterals'] = relatives
        trial_budget = budget.copy()
        size = len(events.encoded(fact))
        if (len(facts) >= 1000 or len(path) > 1024 or len(label) > 16384
                or any(len(k) > 1024 or len(v) > 1024 for k, v in relatives.items())
                or not _within(value, budget=trial_budget) or total_bytes + size > 440000):
            omissions.append({'sourcePath': path if len(path) <= 1024 else None, 'reason': 'input_limit'})
            continue
        budget = trial_budget
        total_bytes += size
        facts.append(fact)
    return facts


def _stage_referenced_brokers(listing, brokers):
    """Add only this listing's contacts to the copied inspection object."""
    if brokers is None:
        return []
    if '_publicFactsStagedBrokers' in listing:
        raise ValueError('public facts broker staging field conflict')
    ids = list(listing.get('brokerIds') or [])
    if not ids:
        return []
    listing['_publicFactsStagedBrokers'] = [brokers.get(bid) for bid in ids]
    return ids


def _unstage_referenced_brokers(listing, brokers, ids):
    if brokers is None:
        return None
    if not ids:
        return brokers
    if '_publicFactsStagedBrokers' not in listing:
        raise ValueError('public facts broker staging field was removed')
    staged = listing.pop('_publicFactsStagedBrokers')
    if not isinstance(staged, list) or len(staged) != len(ids):
        raise ValueError('public facts broker staging field was altered')
    return ChainMap(dict(zip(ids, staged)), brokers)
