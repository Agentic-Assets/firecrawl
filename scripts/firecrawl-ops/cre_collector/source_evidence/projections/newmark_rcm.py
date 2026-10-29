"""Private source-owned projection for Newmark RCM enrichment observations.

Original capture stays private. Receipts describe a local derivation, not source
permission, visibility, publisher identity, or independently proven original values.
"""
import base64
import binascii
import copy
import json
import math
import re
from html import unescape
from urllib.parse import unquote, urlsplit

from buildout_observation_artifact import sensitive_string
from source_evidence import acquisition, buildout_projection, events, map_projection

VERSION = 'newmark-rcm-enrichment-withholding/v1'
SCHEMA = 'newmark-rcm-detail-observation/v1'
MAX_BYTES = 16 * 1024 * 1024
MAX_RECEIPTS = 20000
KEYS = {'bingMapKey', 'captchaKey'}


def _scope(document, source_key):
    if source_key != 'newmark':
        raise ValueError('invalid Newmark projection source')
    if not isinstance(document, dict):
        raise ValueError('invalid Newmark projection artifact')  # noqa: TRY004
    meta, sources, listings = document.get('runMeta'), document.get('sources'), document.get('listings')
    if (not isinstance(meta, dict) or meta.get('mode') != 'enrich' or not isinstance(sources, list)
            or len(sources) != 1 or not isinstance(sources[0], dict) or sources[0].get('sourceKey') != source_key
            or not isinstance(listings, list) or any(not isinstance(x, dict) or x.get('sourceKey') != source_key for x in listings)
            or not isinstance(document.get('brokers'), list) or document.get('totalListings') != len(listings)):
        raise ValueError('invalid Newmark projection scope')
    observations = sources[0].get('retainedObservations', [])
    if not isinstance(observations, list) or len(observations) + len(listings) > 10000:
        raise ValueError('invalid Newmark projection observations')
    return observations


def _literal(value):
    if not isinstance(value, str):
        raise ValueError('Newmark RCM literal unavailable')  # noqa: TRY004
    decoded, literals = acquisition.decode_json(value.encode('utf8'))
    if not isinstance(decoded, dict):
        raise ValueError('Newmark RCM object required')  # noqa: TRY004
    return decoded, literals


def _same_js_value(expected, observed):
    """Compare parsed JS object structure without treating float rounding as loss."""
    stack = [(expected, observed)]; nodes = 0
    while stack:
        left, right = stack.pop(); nodes += 1
        if nodes > 500000:
            raise ValueError('Newmark parsed comparison exceeds bound')
        if type(left) in (int, float) and type(right) in (int, float):
            try:
                a, b = float(left), float(right)
            except (OverflowError, ValueError):
                return False
            if not math.isfinite(a) or not math.isfinite(b) or a != b:
                return False
        elif isinstance(left, dict) and isinstance(right, dict):
            if left.keys() != right.keys():
                return False
            stack.extend((left[key], right[key]) for key in left)
        elif isinstance(left, list) and isinstance(right, list):
            if len(left) != len(right):
                return False
            stack.extend(zip(left, right))
        elif type(left) is not type(right) or left != right:
            return False
    return True


def _remove(data):
    removed = []
    for key in sorted(KEYS):
        if key in data:
            removed.append(('/' + key, data.pop(key)))
    translation = data.get('translation')
    if isinstance(translation, dict):
        languages = translation.get('languages')
        if isinstance(languages, list):
            for index, row in enumerate(languages):
                if isinstance(row, dict) and 'TransPerfectAPIKey' in row:
                    removed.append((f'/translation/languages/{index}/TransPerfectAPIKey', row.pop('TransPerfectAPIKey')))
        selected = translation.get('selected')
        if isinstance(selected, dict) and 'TransPerfectAPIKey' in selected:
            removed.append(('/translation/selected/TransPerfectAPIKey', selected.pop('TransPerfectAPIKey')))
    return removed


def _scan_removed(value, removed):
    stack = [(value, 0)]
    secrets = {x for x in removed if isinstance(x, str) and x}
    if len(secrets) > 1000:
        raise ValueError('Newmark credential scan exceeds bound')
    budget = 0
    nodes = 0
    while stack:
        item, encoded_depth = stack.pop()
        nodes += 1
        if nodes > 500000:
            raise ValueError('Newmark credential scan exceeds bound')
        if isinstance(item, dict):
            if any(key in (KEYS | {'TransPerfectAPIKey'}) for key in item):
                raise ValueError('unreviewed Newmark credential key')
            stack.extend((child, encoded_depth) for child in item.values())
            stack.extend((key, encoded_depth) for key in item)
        elif isinstance(item, list): stack.extend((child, encoded_depth) for child in item)
        elif isinstance(item, str):
            budget += len(item) * max(1, len(secrets))
            if budget > 128 * 1024 * 1024:
                raise ValueError('Newmark credential scan exceeds bound')
            # Known source copies can occur in HTML attributes or URL values.
            # Decode bounded layers for comparison only; never mutate the evidence.
            decoded_text = item
            for _ in range(4):
                budget += len(decoded_text) * max(1, len(secrets))
                if budget > 128 * 1024 * 1024:
                    raise ValueError('Newmark credential scan exceeds bound')
                if any(secret in decoded_text for secret in secrets):
                    raise ValueError('unresolved Newmark credential copy')
                try:
                    following = unquote(unescape(decoded_text), errors='strict')
                    following = re.sub(r'\\u([0-9a-fA-F]{4})',
                                       lambda m: chr(int(m.group(1), 16)) if not 0xD800 <= int(m.group(1), 16) <= 0xDFFF else m.group(0),
                                       following)
                    following = re.sub(r'\\x([0-9a-fA-F]{2})', lambda m: chr(int(m.group(1), 16)), following)
                except UnicodeError as exc:
                    raise ValueError('Newmark credential decoding uncertain') from exc
                if following == decoded_text:
                    break
                decoded_text = following
            else:
                raise ValueError('Newmark credential decoding exceeds bound')
            # A data URL or standalone base64 string can carry another copy.
            encoded = decoded_text.split(',', 1)[1] if decoded_text.startswith('data:') and ';base64,' in decoded_text else decoded_text
            if len(encoded) >= 8 and len(encoded) % 4 == 0 and re.fullmatch(r'[A-Za-z0-9+/]*={0,2}', encoded):
                try:
                    decoded_bytes = base64.b64decode(encoded, validate=True)
                except (ValueError, binascii.Error):
                    decoded_bytes = b''
                budget += len(decoded_bytes) * max(1, len(secrets))
                if budget > 128 * 1024 * 1024:
                    raise ValueError('Newmark credential scan exceeds bound')
                if any(secret.encode('utf8') in decoded_bytes for secret in secrets):
                    raise ValueError('unresolved Newmark credential copy')
            candidate = item.strip()
            if candidate and candidate[0] in '{["':
                try:
                    decoded = json.loads(candidate)
                except (ValueError, RecursionError):
                    continue
                if encoded_depth >= 16:
                    raise ValueError('Newmark encoded credential scan exceeds bound')
                stack.append((decoded, encoded_depth + 1))


def validate_event_payload(payload, source_key):
    """Check Newmark-specific text copies before a source-observation event is admitted."""
    from source_evidence import encoded_json_inspection

    source = payload.get('sourceObservation') if isinstance(payload, dict) else None
    if isinstance(source, dict) and source.get('schema') == SCHEMA and payload.get('newmarkRcmObservation') is not True:
        raise ValueError('Newmark RCM observation marker missing')
    if not isinstance(payload, dict) or payload.get('newmarkRcmObservation') is not True:
        if isinstance(source, dict) and 'literal' in source:
            encoded_json_inspection._inspect_encoded_credentials(source['literal'], inspect_keys=True)
        return
    if source_key != 'newmark': raise ValueError('Newmark RCM observation source mismatch')
    literal = source.get('literal') if isinstance(source, dict) else None
    if not isinstance(source, dict) or source.get('schema') != SCHEMA or not isinstance(literal, str):
        doc, context = payload.get('scrapedDoc'), payload.get('pageContext')
        if isinstance(doc, dict) and (doc.get('rawHtml') is not None or doc.get('markdown') is not None):
            raise ValueError('Newmark RCM unverified text requires projection')
        if (isinstance(context, dict) and isinstance(context.get('scripts'), list)
                and any(isinstance(script, dict) and script.get('text') is not None for script in context['scripts'])):
            raise ValueError('Newmark RCM unverified script requires projection')
        if isinstance(source, dict) and source.get('parsed') is not None:
            raise ValueError('Newmark RCM unverified parse requires projection')
        encoded_json_inspection._inspect_encoded_credentials(payload, inspect_keys=True)
        return
    decoded, _ = acquisition.decode_json(literal.encode('utf8'))
    if not isinstance(decoded, dict): raise ValueError('Newmark RCM literal is not an object')  # noqa: TRY004
    encoded_json_inspection._inspect_encoded_credentials(payload, inspect_keys=True)
    doc = payload.get('scrapedDoc')
    html = doc.get('rawHtml') if isinstance(doc, dict) else None
    if isinstance(html, str):
        scripts = re.findall(r'\bvar\s+data\s*=\s*(\{[\s\S]*?\})\s*</script>', html, re.IGNORECASE)
        if not scripts or any(script != literal for script in scripts):
            raise ValueError('Newmark RCM raw HTML literal mismatch')


def _project_missing(row, base, withheld, literals):
    """Retain generic context but do not admit uninspectable full text as RCM data."""
    targets = []
    source = row.get('sourceObservation')
    if isinstance(source, dict):
        for key in ('literal', 'parsed'):
            if key in source: targets.append((source, key, base + '/sourceObservation/' + key))
    doc = row.get('scrapedDoc')
    if isinstance(doc, dict):
        if 'rawHtml' in doc: targets.append((doc, 'rawHtml', base + '/scrapedDoc/rawHtml'))
        if 'markdown' in doc:
            targets.append((doc, 'markdown', base + '/scrapedDoc/markdown'))
    context = row.get('pageContext')
    if isinstance(context, dict):
        scripts = context.get('scripts')
        if isinstance(scripts, list):
            for i, script in enumerate(scripts):
                if isinstance(script, dict) and 'text' in script:
                    targets.append((script, 'text', base + f'/pageContext/scripts/{i}/text'))
        tables = context.get('tables')
        provenance = tables.get('provenance') if isinstance(tables, dict) else None
        if isinstance(provenance, dict) and 'inputSha256' in provenance:
            targets.append((provenance, 'inputSha256', base + '/pageContext/tables/provenance/inputSha256'))
    changed = False
    for parent, key, pointer in targets:
        if parent[key] is not None:
            parent[key] = None
            withheld.append({'pointer':pointer, 'reason':'unverified_content_omitted'})
            changed = True
    if changed:
        for pointer in list(literals):
            if pointer == base + '/sourceObservation/parsed' or pointer.startswith(base + '/sourceObservation/parsed/'):
                del literals[pointer]
        omissions = row.setdefault('omissions', {})
        if not isinstance(omissions, dict): raise ValueError('invalid Newmark omissions')
        omissions['newmarkRcmProjection'] = 'unverified_content_omitted'


def project(document, *, source_key, numeric_literals=None):
    result = copy.deepcopy(document)
    observations = _scope(result, source_key)
    buildout_projection._bounded(result)
    if len(events.encoded(result)) > MAX_BYTES:
        raise ValueError('Newmark projection exceeds bound')
    literals = dict(numeric_literals or {})
    withheld = []
    all_removed = []
    guard = 'evidencePurpose' not in result['runMeta']
    if guard: result['runMeta']['evidencePurpose'] = 'private_unqualified_source_observations'
    elif result['runMeta']['evidencePurpose'] != 'private_unqualified_source_observations':
        raise ValueError('conflicting evidence scope')
    for index, listing in enumerate(result['listings']):
        facts = listing.get('extraFacts')
        if isinstance(facts, dict):
            for key in ('detail_tables', 'detail_tables_attempt'):
                tables = facts.get(key)
                provenance = tables.get('provenance') if isinstance(tables, dict) else None
                if isinstance(provenance, dict) and provenance.get('inputSha256') is not None:
                    provenance['inputSha256'] = None
                    withheld.append({'pointer':f'/listings/{index}/extraFacts/{key}/provenance/inputSha256',
                                     'reason':'original_html_hash'})
    for index, row in enumerate(observations):
        if not isinstance(row, dict): raise ValueError('invalid Newmark observation')  # noqa: TRY004
        source = row.get('sourceObservation')
        if isinstance(source, dict) and source.get('schema') == SCHEMA and row.get('newmarkRcmObservation') is not True:
            raise ValueError('Newmark RCM observation marker missing')
        if row.get('newmarkRcmObservation') is not True:
            if isinstance(source, dict) and 'literal' in source:
                from source_evidence import encoded_json_inspection
                encoded_json_inspection._inspect_encoded_credentials(source['literal'], inspect_keys=True)
            continue
        base = f'/sources/0/retainedObservations/{index}'
        if not isinstance(source, dict) or source.get('schema') != SCHEMA or not isinstance(source.get('literal'), str):
            _project_missing(row, base, withheld, literals)
            continue
        try:
            _literal(source['literal'])
        except (ValueError, TypeError, RecursionError):
            _project_missing(row, base, withheld, literals)
            continue
        if (source.get('identity') != 'unverified' or source.get('visibility') != 'unverified'
                or source.get('sourceClockStatus') != 'unverified'
                or (source.get('sourceClock') is not None and not isinstance(source.get('sourceClock'), str))
                or source.get('extraction') != 'observed' or source.get('match') not in ('matched', 'unresolved')
                or source.get('matchMethod') != 'strict_json_visitor_source_candidate'
                or source.get('numericFidelity') != 'literal_only' or type(source.get('parsedSerializable')) is not bool
                or (source.get('parsed') is not None and not isinstance(source.get('parsed'), dict))):
            raise ValueError('invalid Newmark RCM source observation')
        original = source.get('literal'); data, numeric = _literal(original)
        if 'projectedNumericLiterals' in source:
            if source['projectedNumericLiterals'] != numeric or (source.get('parsed') is not None and source['parsed'] != data):
                raise ValueError('Newmark projected parse mismatch')
        elif source.get('parsed') is not None and not _same_js_value(json.loads(original), source['parsed']):
            raise ValueError('Newmark original parsed structure mismatch')
        removed = _remove(data)
        for pointer, _value in removed:
            if _value is not None and not isinstance(_value, str):
                raise ValueError('unsupported Newmark credential value')
            all_removed.append(_value)
            numeric = {k:v for k,v in numeric.items() if k != pointer and not k.startswith(pointer + '/')}
            withheld.append({'pointer': base + '/sourceObservation/literal' + pointer, 'reason':'credential_key_removed'})
        replacement = acquisition.encode_json(data, numeric)
        for pointer in list(literals):
            if pointer.startswith(base + '/sourceObservation/parsed/'):
                del literals[pointer]
        source['literal'] = replacement
        source['projectedNumericLiterals'] = numeric
        if source.get('parsed') is not None: source['parsed'] = copy.deepcopy(data)
        removed_values = [value for _, value in removed]
        doc = row.get('scrapedDoc')
        context = row.get('pageContext')
        candidates = []
        if isinstance(doc, dict):
            for key in ('rawHtml','markdown'):
                if key in doc: candidates.append((doc, key, base + '/scrapedDoc/' + key))
        if isinstance(context, dict):
            scripts = context.get('scripts')
            if isinstance(scripts, list):
                for n, script in enumerate(scripts):
                    if isinstance(script, dict) and 'text' in script:
                        candidates.append((script, 'text', base + f'/pageContext/scripts/{n}/text'))
        for parent, key, pointer in candidates:
            value = parent[key]
            if not isinstance(value, str): continue
            if original != replacement and original in value:
                parent[key] = value.replace(original, replacement)
                withheld.append({'pointer':pointer,'reason':'literal_copy_projected'})
            if (isinstance(parent[key], str) and
                    (any(isinstance(x,str) and x and x in parent[key] for x in removed_values)
                     or any(label in parent[key] for label in ('bingMapKey', 'captchaKey', 'TransPerfectAPIKey')))):
                parent[key] = None
                if withheld and withheld[-1] == {'pointer':pointer,'reason':'literal_copy_projected'}:
                    withheld.pop()
                withheld.append({'pointer':pointer,'reason':'contaminated_text_omitted'})
        if isinstance(doc, dict) and isinstance(doc.get('markdown'), str) and sensitive_string(doc['markdown']):
            doc['markdown'] = None
            withheld.append({'pointer':base + '/scrapedDoc/markdown','reason':'contaminated_text_omitted'})
        if isinstance(context, dict):
            images = context.get('images')
            if isinstance(images, list):
                for n, image in enumerate(images):
                    value = image.get('src') if isinstance(image, dict) else None
                    if isinstance(value, str) and sensitive_string(value):
                        try:
                            url = urlsplit(value)
                            if (url.scheme not in ('http','https') or not url.hostname
                                    or any(ord(ch) <= 32 or ord(ch) == 127 for ch in value)):
                                raise ValueError('unsupported Newmark image reference')
                            _ = url.port
                        except ValueError:
                            raise ValueError('unsupported Newmark image reference') from None
                        image['src'] = None
                        withheld.append({'pointer':base + f'/pageContext/images/{n}/src','reason':'credential_reference_omitted'})
            tables = context.get('tables')
            provenance = tables.get('provenance') if isinstance(tables, dict) else None
            if isinstance(provenance, dict) and 'inputSha256' in provenance and provenance['inputSha256'] is not None:
                provenance['inputSha256'] = None
                withheld.append({'pointer':base + '/pageContext/tables/provenance/inputSha256','reason':'original_html_hash'})
        _scan_removed(row, removed_values)
    _scan_removed(result, all_removed)
    withheld.sort(key=lambda item: (item['pointer'], item['reason']))
    if len(withheld) > MAX_RECEIPTS or len(events.encoded(withheld)) > 1000000:
        raise ValueError('Newmark withholding exceeds bound')
    receipt = {'version':VERSION,'sourceKey':source_key,'withheld':withheld,'scopeGuardAdded':guard,
               'projectedCanonicalSha256':map_projection.canonical_digest(result, literals or None)}
    return result, literals, receipt


def prepare(raw, *, source_key):
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES: raise ValueError('Newmark projection input exceeds bound')
    document, literals = acquisition.decode_json(raw, max_bytes=MAX_BYTES)
    return project(document, source_key=source_key, numeric_literals=literals)


def validate(document, receipt, *, source_key, numeric_literals=None):
    _scope(document, source_key)
    if (not isinstance(receipt, dict) or set(receipt) != {'version','sourceKey','withheld','scopeGuardAdded','projectedCanonicalSha256'}
            or receipt['version'] != VERSION or receipt['sourceKey'] != source_key
            or type(receipt['scopeGuardAdded']) is not bool or not isinstance(receipt['withheld'], list)
            or document['runMeta'].get('evidencePurpose') != 'private_unqualified_source_observations'
            or receipt['projectedCanonicalSha256'] != map_projection.canonical_digest(document, numeric_literals)):
        raise ValueError('invalid Newmark projection receipt')
    if (len(receipt['withheld']) > MAX_RECEIPTS or len(events.encoded(receipt['withheld'])) > 1000000
            or any(not isinstance(item, dict) or set(item) != {'pointer','reason'}
                   or not isinstance(item['pointer'], str) or not isinstance(item['reason'], str) for item in receipt['withheld'])
            or receipt['withheld'] != sorted(receipt['withheld'], key=lambda item: (item['pointer'], item['reason']))):
        raise ValueError('invalid ordered Newmark withholding witnesses')
    seen = set()
    for item in receipt['withheld']:
        if not isinstance(item, dict) or set(item) != {'pointer','reason'} or item['pointer'] in seen:
            raise ValueError('invalid Newmark withholding witness')
        seen.add(item['pointer'])
        if not isinstance(item['pointer'], str):
            raise ValueError('invalid Newmark withholding pointer')  # noqa: TRY004
        pointer, reason = item['pointer'], item['reason']
        base = r'/sources/0/retainedObservations/(0|[1-9][0-9]*)'
        credential = re.fullmatch(base + r'/sourceObservation/literal(/(?:bingMapKey|captchaKey|translation/languages/(?:0|[1-9][0-9]*)/TransPerfectAPIKey|translation/selected/TransPerfectAPIKey))', pointer)
        copies = re.fullmatch(base + r'/(?:scrapedDoc/(?:rawHtml|markdown)|pageContext/scripts/(?:0|[1-9][0-9]*)/text)', pointer)
        htmlhash = (re.fullmatch(base + r'/pageContext/tables/provenance/inputSha256', pointer)
                    or re.fullmatch(r'/listings/(?:0|[1-9][0-9]*)/extraFacts/(?:detail_tables|detail_tables_attempt)/provenance/inputSha256', pointer))
        image = re.fullmatch(base + r'/pageContext/images/(?:0|[1-9][0-9]*)/src', pointer)
        missing_content = re.fullmatch(base + r'/(?:sourceObservation/(?:literal|parsed)|scrapedDoc/(?:rawHtml|markdown)|pageContext/scripts/(?:0|[1-9][0-9]*)/text|pageContext/tables/provenance/inputSha256)', pointer)
        if reason == 'credential_key_removed' and credential:
            index = int(credential.group(1))
            try:
                literal = document['sources'][0]['retainedObservations'][index]['sourceObservation']['literal']
                decoded, _ = _literal(literal)
                node = decoded
                parts = acquisition._pointer_parts(credential.group(2))
                for part in parts[:-1]: node = node[acquisition._child(node, part)]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ValueError('invalid Newmark credential parent') from exc
            if not isinstance(node, dict) or parts[-1] in node:
                raise ValueError('withheld Newmark key still present')
            continue
        if ((reason in {'literal_copy_projected','contaminated_text_omitted'} and copies)
                or (reason == 'original_html_hash' and htmlhash) or (reason == 'credential_reference_omitted' and image)
                or (reason == 'unverified_content_omitted' and missing_content)):
            try:
                node = document
                for part in acquisition._pointer_parts(pointer): node = node[acquisition._child(node, part)]
            except (ValueError, KeyError, IndexError, TypeError) as exc:
                raise ValueError('Newmark withholding target missing') from exc
            if (reason == 'literal_copy_projected' and (not isinstance(node, str)
                    or document['sources'][0]['retainedObservations'][int(copies.group(1))]['sourceObservation']['literal'] not in node)) or (reason != 'literal_copy_projected' and node is not None):
                raise ValueError('invalid Newmark withholding target')
            continue
        raise ValueError('invalid Newmark withholding reason or pointer')
    again, _, checked = project(document, source_key=source_key, numeric_literals=numeric_literals)
    if again != document or checked['withheld']:
        raise ValueError('Newmark projection is not idempotent')
