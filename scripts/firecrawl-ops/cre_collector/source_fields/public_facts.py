"""Bounded public-fact construction and credential inspection helpers."""
import base64
import binascii
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


def _encoded_json_credential(value, budget, removed, numeric_removed, depth=0, *, marked=False):
    budget[0] += 1
    if budget[0] > 20000 or depth > 16:
        raise _InspectionLimit('Newmark encoded JSON inspection exceeds bound')
    if isinstance(value, dict):
        found = False
        for key, child in value.items():
            if _credential_label_encoded(key) and not (marked and _BEARER.fullmatch(key.strip())):
                _collect_credential_strings(child, removed, numeric_removed)
                found = True
            else:
                found = _encoded_json_credential(child, budget, removed, numeric_removed, depth + 1, marked=marked) or found
        return found
    if isinstance(value, list):
        found = False
        for child in value:
            found = _encoded_json_credential(child, budget, removed, numeric_removed, depth + 1, marked=marked) or found
        return found
    if isinstance(value, str):
        candidate = value.strip()
        for step in range(6 if marked else 1):
            if candidate and candidate[0] in '{["':
                try:
                    parsed = json.loads(candidate)
                except (ValueError, RecursionError):
                    pass
                else:
                    return _encoded_json_credential(parsed, budget, removed, numeric_removed, depth + 1, marked=marked)
            if not marked:
                break
            following = unquote(unescape(candidate), errors='replace')
            if following == candidate:
                break
            if step == 5:
                raise _InspectionLimit('Public credential decoding exceeds bound')
            candidate = following
        return False
    return False


_BEARER = re.compile(r'''\bbearer[ \t]+([A-Za-z0-9._~+/-]+={0,2})(?=$|[\s,;\"'<>}\]])''', re.IGNORECASE)


def _bare_bearer_tokens(value):
    # A bare Bearer scheme plus token is ambiguous source text; treat the
    # matching token as unverified credential material for marked staging.
    found = set()
    candidate = value
    for _ in range(5):
        for match in _BEARER.finditer(candidate):
            found.add(match.group(1))
        following = unquote(unescape(candidate), errors='replace')
        if following == candidate:
            break
        candidate = following
    return found


# Only unambiguous credential labels are considered assignments.  A bare
# Basic scheme is qualified by the same canonical base64 user:password check
# used by the Buildout observation path, avoiding ordinary "Basic rent" prose.
_ASSIGNMENT = re.compile(r"(?=(?<![A-Za-z0-9_])([A-Za-z][A-Za-z0-9_-]*)[\"']?[ \t]*[=:][ \t]*[\"']?([^\s,;&\"'<>}\]]+))")
_BASIC = re.compile(r"\bbasic[ \t]+([A-Za-z0-9+/]+={0,2})(?=$|[\s,;\"'<>}\]])", re.IGNORECASE)
_AUTH_HEADER = re.compile(r"\bauthorization[ \t]*:[ \t]*(?:basic|bearer)[ \t]+[\"']?([^\s,;\"'<>}\]]+)", re.IGNORECASE)


def _marked_credential_tokens(value):
    found = set()
    pending = [(value, 0)]
    nodes = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > 20000 or depth > 16:
            raise _InspectionLimit('Public credential JSON inspection exceeds bound')
        if isinstance(item, dict):
            pending.extend((child, depth + 1) for pair in item.items() for child in pair)
            continue
        if isinstance(item, list):
            pending.extend((child, depth + 1) for child in item)
            continue
        if not isinstance(item, str):
            continue
        candidate = item
        for step in range(6):
            parsed_json = False
            if candidate.lstrip().startswith(('{', '[', '"')):
                try:
                    parsed = json.loads(candidate)
                    if isinstance(parsed, (dict, list, str)):
                        pending.append((parsed, depth + 1))
                        parsed_json = True
                except (ValueError, RecursionError):
                    pass
            if not parsed_json:
                found.update(_bare_bearer_tokens(candidate))
                bearer_token_positions = {match.start(1) for match in _BEARER.finditer(candidate)}
                for match in _ASSIGNMENT.finditer(candidate):
                    label, token = match.groups()
                    if (match.start(1) not in bearer_token_positions and _credential_label_encoded(label)
                            and token.lower() not in ('basic', 'bearer')):
                        found.add(token)
                for match in _AUTH_HEADER.finditer(candidate):
                    found.add(match.group(1))
                for match in _BASIC.finditer(candidate):
                    token = match.group(1)
                    try:
                        decoded = base64.b64decode(token + '=' * (-len(token) % 4), validate=True)
                        if base64.b64encode(decoded).decode('ascii').rstrip('=') == token.rstrip('=') and b':' in decoded:
                            found.add(token)
                            try:
                                literal = decoded.decode('utf-8')
                                if literal and len(literal) <= 4096 and not any(ord(ch) < 32 for ch in literal):
                                    found.add(literal)
                                    password = literal.partition(':')[2]
                                    if password:
                                        found.add(password)
                            except UnicodeDecodeError:
                                pass
                    except (ValueError, binascii.Error):
                        pass
            following = unquote(unescape(candidate), errors='replace')
            if following == candidate or step == 5:
                break
            candidate = following
    return found


def _sensitive_encoded_string(value):
    candidate = value
    for _ in range(5):
        if sensitive_string(candidate) or _marked_credential_tokens(candidate):
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
                credential_tokens = _marked_credential_tokens(key) if inspect_strings else set()
                if (_credential_label_encoded(key) or credential_tokens) and not (allow_label and allow_label(child_path, key)):
                    removed.extend(credential_tokens)
                    if len(removed) > 1000:
                        raise _InspectionLimit('Public credential count exceeds bound')
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
        elif isinstance(value, str):
            marked_sensitive = inspect_strings and _sensitive_encoded_string(value)
            # Do not short-circuit: the encoded JSON may contain additional
            # credential-labeled values whose independent copies need removal.
            encoded_sensitive = _encoded_json_credential(value, encoded_budget, removed, numeric_removed, marked=inspect_strings)
            if marked_sensitive or encoded_sensitive:
                removed.extend(_marked_credential_tokens(value) if inspect_strings else ())
                if len(removed) > 1000:
                    raise _InspectionLimit('Public credential count exceeds bound')
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
