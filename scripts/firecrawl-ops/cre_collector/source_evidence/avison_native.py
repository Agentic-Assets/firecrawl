"""Private projection of Avison's native website feed; no source acceptance.

Removed values and original response hashes must not enter the receipt. This
transform alone does not authorize admission or prove publisher visibility.
"""

import copy
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import unquote, urlsplit

from cre_avison_price_visibility import (
    ASK_KEYS,
    ASK_LABEL,
    INDEPENDENT,
    OTHER_LABEL,
    PROTECTED,
    PROTECTED_TEXT,
    _amount_pattern,
    _key,
    _tokens,
    asking_amounts,
    asking_control,
    project_value,
)

from source_evidence import acquisition, events

VERSION = "avison-native-asking-projection/v1"
MAX_CHANGES = 20000
MAX_NODES = 500000
MAX_RECEIPT_BYTES = 1000000
_MONEY = re.compile(
    r"(?:[$€£¥]\s*\d(?:[\d,]*\d)?(?:\.\d+)?(?:\s*(?:million|thousand|[kKmM])\b)?|\b(?:USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF)\s*\d(?:[\d,]*\d)?(?:\.\d+)?"
    r"|\b\d(?:[\d,]*\d)?(?:\.\d+)?\s*(?:USD|CAD|EUR|GBP|AUD|JPY|CNY|CHF|dollars?|million|thousand)\b)",
    re.IGNORECASE,
)
_URL = re.compile(r"https?://[^\s<>\"']+", re.IGNORECASE)
_LABEL = re.compile(f"(?:{ASK_LABEL})|(?:{OTHER_LABEL})", re.IGNORECASE)
_BARE_ASKING = re.compile(
    rf"(?:{ASK_LABEL})\s*[:=]\s*(?P<amount>(?:\d{{1,3}}(?:,\d{{3}})+|\d+)(?:\.\d+)?"
    r"(?:\s*(?:million|thousand|[kKmM])\b)?)(?![\w,]|\.\d)",
    re.IGNORECASE,
)


def _preceding_label(text, position):
    window = text[max(0, position - 160):position]
    clauses = re.split(r"[\n;]|\.(?=\s)|</(?:p|li|div)>", window, flags=re.IGNORECASE)
    labels = list(_LABEL.finditer(clauses[-1]))
    return labels[-1].group() if labels else ""


def _prose(text, amounts):
    """Mask only monetary spans; retain unrelated text and independent rent."""
    urls = [(m.start(), m.end()) for m in _URL.finditer(text)]
    candidates = list(_MONEY.finditer(text))
    changes = []
    for match in candidates:
        if any(a <= match.start() < b for a, b in urls):
            continue
        label = _preceding_label(text, match.start())
        following = text[match.end():match.end() + 30]
        independent = bool(re.match(OTHER_LABEL, label, re.IGNORECASE) or re.match(r"\s*/\s*sf\s*/\s*(?:yr|year|mo|month)\b", following, re.IGNORECASE))
        if independent:
            continue
        amount = re.search(r"\d[\d,.]*", match.group())
        exact = False
        if amount:
            try:
                exact = float(amount.group().replace(",", "").rstrip(".")) in amounts
            except ValueError:
                pass
        reason = "withheld_asking_span" if exact or re.match(ASK_LABEL, label, re.IGNORECASE) else "unresolved_monetary_span"
        changes.append((match.start(), match.end(), reason))
    for match in _BARE_ASKING.finditer(text):
        start, end = match.span("amount")
        if any(a <= start < b for a, b in urls):
            continue
        if any(start < b and end > a for a, b, _ in changes):
            continue
        changes.append((start, end, "withheld_asking_span"))
    if amounts:
        protected = urls + [(m.start(), m.end()) for m in re.finditer(PROTECTED_TEXT, text, re.IGNORECASE)]
        protected += [(m.start(), m.end()) for m in re.finditer(r"\b(?:suite|ste|unit)\s+[\w-]+\b", text, re.IGNORECASE)]
        for match in re.finditer(_amount_pattern(amounts), text):
            if any(a <= match.start() < b for a, b in protected):
                continue
            if any(a <= match.start() < b for a, b, _ in changes):
                continue
            label = _preceding_label(text, match.start())
            if label and re.match(OTHER_LABEL, label, re.IGNORECASE):
                continue
            changes.append((match.start(), match.end(), "withheld_asking_span"))
    result = text
    for start, end, reason in sorted(changes, reverse=True):
        marker = "[withheld]" if reason == "withheld_asking_span" else "[unresolved monetary value]"
        result = result[:start] + marker + result[end:]
    # Refuse unrecognized asking syntax rather than emit an unmasked amount.
    check = _URL.sub("", result)
    for match in reversed(list(_MONEY.finditer(check))):
        label = _preceding_label(check, match.start())
        following = check[match.end():match.end() + 30]
        if ((label and re.match(OTHER_LABEL, label, re.IGNORECASE))
                or re.match(r"\s*/\s*sf\s*/\s*(?:yr|year|mo|month)\b", following, re.IGNORECASE)):
            check = check[:match.start()] + check[match.end():]
    check = re.sub(PROTECTED_TEXT, "", check, flags=re.IGNORECASE)
    check = re.sub(r"\b(?:suite|ste|unit)\s+[\w-]+\b", "", check, flags=re.IGNORECASE)
    if amounts and re.search(_amount_pattern(amounts), check):
        raise ValueError("Avison prose contains unresolved unformatted amount")
    if re.search(r"(?:asking(?:\s+price)?|sale\s+price|purchase\s+price)(?:\s*[:=]\s*|\s+)\d", _URL.sub("", result), re.IGNORECASE):
        raise ValueError("Avison prose contains unformatted asking amount")
    return result, {reason for _, _, reason in changes}


def _pointer(path, key):
    return path + "/" + str(key).replace("~", "~0").replace("/", "~1")


def _asking_key(key):
    tokens = _tokens(key)
    return (_key(key) in ASK_KEYS or "asking" in tokens
            or ("sale" in tokens and ("price" in tokens or "amount" in tokens)))


def _number(value):
    if type(value) not in (str, int, float, Decimal):
        return None
    try:
        out = Decimal(str(value).replace(",", "").replace("$", "").strip())
        return out if out.is_finite() and out > 0 else None
    except InvalidOperation:
        return None


def _asking_url(text):
    """Remove named asking query parameters without changing other URL bytes."""
    try:
        split = urlsplit(text)
        if split.scheme not in ("http", "https") or not split.netloc or not split.query:
            return text
        fields = split.query.split("&")
        keep = [field for field in fields if not _asking_key(unquote(field.partition("=")[0]))]
        if len(keep) == len(fields):
            return text
        base, _, fragment = text.partition("#")
        prefix = base.split("?", 1)[0]
        return prefix + ("?" + "&".join(keep) if keep else "") + ("#" + fragment if "#" in text else "")
    except ValueError:
        raise ValueError("invalid source URL") from None


def project(document, numeric_literals=None):
    """Project asking information; preserve all other source JSON values.

    Ambiguous monetary spans are explicitly unresolved, while unrelated prose
    and independently labeled financial values remain in place.
    Input and literal map are never mutated and no original digest is created.
    """
    literals = {} if numeric_literals is None else copy.deepcopy(numeric_literals)
    if not isinstance(document, dict) or not isinstance(document.get("items"), list):
        raise ValueError("Avison website response needs an items array")  # noqa: TRY004
    if len(document["items"]) > 10000 or len(events.encoded(document)) > events.MAX_ARTIFACT_BYTES:
        raise ValueError("Avison website response exceeds bounds")
    if not isinstance(literals, dict):
        raise ValueError("invalid numeric literal map")  # noqa: TRY004
    # Verify input pointers before removing literals covered by a projection.
    acquisition.encode_json(document, literals)
    result = copy.deepcopy(document)
    changes = []
    visited = 0

    def record(path, action, reason):
        if len(path.encode("utf-8")) > 4096 or len(changes) >= MAX_CHANGES:
            raise ValueError("Avison projection receipt exceeds bounds")
        changes.append({"pointer": path, "action": action, "reason": reason})

    def source_amounts(value, path, depth=0):
        if depth > 128:
            raise ValueError("Avison projection structure exceeds bounds")
        found = set()
        if isinstance(value, dict):
            for key, item in value.items():
                child = _pointer(path, key)
                if _asking_key(key) or _key(key) == "hidepricetext":
                    number = _number(literals.get(child, item))
                    if number is not None:
                        found.add(number)
                found.update(source_amounts(item, child, depth + 1))
        elif isinstance(value, list):
            for i, item in enumerate(value):
                found.update(source_amounts(item, _pointer(path, i), depth + 1))
        return found

    def restore_and_record(original, projected, path, depth, amounts, independent=False):
        nonlocal visited
        visited += 1
        if visited > MAX_NODES or depth > 128:
            raise ValueError("Avison projection structure exceeds bounds")
        if isinstance(original, dict) and isinstance(projected, dict):
            if projected.keys() - original.keys():
                raise ValueError("Avison projection added a field")
            for key, value in original.items():
                child = _pointer(path, key)
                protected = (_key(key) in INDEPENDENT or _key(key) in PROTECTED
                             or _key(key) in {"hideprice", "name", "headline", "metatitle", "suite", "unit", "space",
                                              "suitenumber", "suiteid", "unitnumber", "unitid", "spacenumber", "spaceid"})
                if (independent or protected) and isinstance(value, (dict, list)):
                    # Rebuild independent subtrees from the source, not the
                    # generic projection, which can remove equal-valued facts.
                    # Explicit nested controls still own their local scope.
                    projected[key] = scopes(value, child, depth + 1, within_withheld=True)
                    continue
                protected = independent or protected
                number = _number(literals.get(child, value))
                if not protected and not _asking_key(key) and number is not None and number in amounts:
                    projected.pop(key, None)
                    record(child, "omitted", "withheld_asking_alias")
                    continue
                if key not in projected:
                    if (value is None or value == "") and child not in literals:
                        projected[key] = copy.deepcopy(value)
                    elif (_key(key) in ASK_KEYS or "asking" in _tokens(key)
                          or ("sale" in _tokens(key) and ("price" in _tokens(key) or "amount" in _tokens(key)))):
                        record(child, "omitted", "withheld_asking")
                    elif isinstance(value, str):
                        clean, reasons = _prose(value, amounts)
                        projected[key] = clean
                        for reason in sorted(reasons):
                            record(child, "projected", reason)
                    else:
                        reason = "unresolved_prose" if isinstance(value, str) else "withheld_asking"
                        record(child, "omitted", reason)
                else:
                    if isinstance(value, str) and re.match(r"^https?://", value, re.IGNORECASE):
                        safe_url = _asking_url(value)
                        if safe_url != value:
                            projected[key] = safe_url
                            record(child, "projected", "withheld_url_parameter")
                            continue
                    if (isinstance(value, str) and isinstance(projected[key], str)
                            and (value != projected[key] or (amounts and not protected))):
                        clean, reasons = _prose(value, amounts)
                        projected[key] = clean
                        for reason in sorted(reasons):
                            record(child, "projected", reason)
                    if (isinstance(value, str) and isinstance(projected[key], str)
                            and re.sub(r"\s+", "", value) == re.sub(r"\s+", "", projected[key])):
                        projected[key] = value
                    if not (isinstance(value, str) and isinstance(projected[key], str)):
                        restore_and_record(value, projected[key], child, depth + 1, amounts, protected)
            # Restoring empty values must retain the source key order.
            ordered = {key: projected[key] for key in original if key in projected}
            projected.clear()
            projected.update(ordered)
        elif isinstance(original, list) and isinstance(projected, list):
            if len(original) != len(projected):
                raise ValueError("Avison projection changed list cardinality")
            for index, (left, right) in enumerate(zip(original, projected)):
                child = _pointer(path, index)
                number = _number(literals.get(child, left))
                if not independent and number is not None and number in amounts:
                    projected[index] = "[withheld]"
                    record(child, "projected", "withheld_asking_alias")
                elif isinstance(left, str) and not independent:
                    safe_url = _asking_url(left) if re.match(r"^https?://", left, re.IGNORECASE) else left
                    if safe_url != left:
                        record(child, "projected", "withheld_url_parameter")
                    clean, reasons = _prose(safe_url, amounts)
                    projected[index] = clean
                    for reason in sorted(reasons):
                        record(child, "projected", reason)
                else:
                    restore_and_record(left, right, child, depth + 1, amounts, independent)
        elif type(original) is not type(projected) or original != projected:
            if not isinstance(original, str) or not isinstance(projected, str):
                raise ValueError("Avison projection changed an unsupported value")  # noqa: TRY004
            record(path, "projected", "unresolved_prose")

    def scopes(value, path, depth=0, within_withheld=False):
        if depth > 128:
            raise ValueError("Avison projection structure exceeds bounds")
        if isinstance(value, dict):
            control = asking_control(value)
            if control == "withheld":
                amounts = source_amounts(value, path)
                projected = project_value(value, asking_amounts(value))
                restore_and_record(value, projected, path, depth, amounts)
                return projected
            projected = {}
            for key, child in value.items():
                child_path = _pointer(path, key)
                if (within_withheld and _asking_key(key)
                        and not ((child is None or child == "") and child_path not in literals)):
                    record(child_path, "omitted", "withheld_asking")
                    continue
                projected[key] = scopes(child, child_path, depth + 1, within_withheld)
            return projected
        if isinstance(value, list):
            return [scopes(child, _pointer(path, i), depth + 1, within_withheld) for i, child in enumerate(value)]
        if within_withheld and isinstance(value, str) and re.match(r"^https?://", value, re.IGNORECASE):
            safe_url = _asking_url(value)
            if safe_url != value:
                record(path, "projected", "withheld_url_parameter")
            return safe_url
        return copy.deepcopy(value)

    scan_count = 0

    def scopes_validate(value, depth):
        nonlocal scan_count
        scan_count += 1
        if scan_count > MAX_NODES:
            raise ValueError("Avison projection structure exceeds bounds")
        if depth > 128:
            raise ValueError("Avison projection structure exceeds bounds")
        if isinstance(value, dict):
            asking_control(value)
            for child in value.values():
                scopes_validate(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                scopes_validate(child, depth + 1)

    scopes_validate(document, 0)
    result = scopes(document, "")

    # A removed source numeric literal must not survive in the literal map.
    excluded = tuple(change["pointer"] for change in changes)
    projected_literals = {p: value for p, value in literals.items()
                          if not any(p == x or p.startswith(x + "/") for x in excluded)}
    acquisition.encode_json(result, projected_literals)
    receipt = {"version": VERSION, "changes": changes,
               "unresolvedProse": sum(c["reason"] == "unresolved_monetary_span" for c in changes)}
    if len(events.encoded(receipt)) > MAX_RECEIPT_BYTES:
        raise ValueError("Avison projection receipt exceeds bounds")
    return result, projected_literals, receipt


def validate_projected(document, numeric_literals=None):
    """Require an idempotent projected representation, not an original hash."""
    projected, literals, receipt = project(document, numeric_literals)
    if projected != document or literals != (numeric_literals or {}) or receipt["changes"]:
        raise ValueError("Avison native response is not projected")
