"""Offline private native-feed entry verification; no persistence admission."""
import base64
import binascii
import hashlib
import json
import re
import zlib

from buildout_observation_artifact import invalid_number, unique_object

from source_evidence import acquisition, events, native_feed_projection

MAX_BODY = 16 * 1024 * 1024
MAX_ENTRY = ((MAX_BODY + 65536) * 4 + 2) // 3 + 16384
PROFILES = {'srs_search_post_v1': 'srs', 'kidder_bulk_post_v1': 'kidder-mathews',
            'kidder_paged_post_v1': 'kidder-mathews', 'hanley_embedded_array_v1': 'hanley',
            'newmark_nim_sale_post_v1': 'newmark', 'newmark_nim_lease_post_v1': 'newmark'}
ROW_KEYS = {'srs': 'properties', 'kidder-mathews': 'results', 'hanley': None, 'newmark': 'data'}
SRS_FILTERS = {'availabilityType': ['sale', 'lease', 'investment-sale'],
    'propertyType': ['retail', 'industrial', 'office', 'land', 'multifamily', 'hospitality', 'healthcare', 'special_purpose'],
    'address': None, 'tenant': '', 'ownershipType': ['fee-simple-land-building', 'ground-lease-land-only', 'leasehold-lease-only', 'other'],
    'portfolio': [], 'tenancyType': ['single-tenant', 'multi-tenant', 'land'], 'subType': None,
    'orderDirection': 'DESC', 'orderBy': 'date', 'office': '', 'broker': '', 'sizeRange': {'required': False},
    'lotSizeRange': {'required': False}, 'priceRange': {'required': True}, 'capRateRange': {'required': True},
    'latLong': None, 'searchTerms': ''}


def request_bytes(profile, offset):
    if profile == 'srs_search_post_v1':
        body = {'query': {'offset': offset, 'pageSize': 12, **SRS_FILTERS}, 'client_ip': ''}
    elif profile in ('kidder_bulk_post_v1', 'kidder_paged_post_v1'):
        body = {'startIndex': offset, 'numResults': 8000 if profile == 'kidder_bulk_post_v1' else 50,
                'includeAggregations': False}
    elif profile in ('newmark_nim_sale_post_v1', 'newmark_nim_lease_post_v1'):
        body = {'type': 2 if profile == 'newmark_nim_sale_post_v1' else 1, 'listingIds': [],
                'propertyTypes': [], 'brokers': [], 'statuses': [], 'propertySubtypes': [], 'spaceTypes': [],
                'buildingClasses': [], 'leaseTypes': [], 'excludeUnpriced': False, 'page': offset // 100,
                'take': 100, 'sortBy': 'createdOn', 'isAscending': True}
    else:
        return None
    return json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()


def _validate_request_receipt(profile, receipt, source):
    """Validate fields shared by private and projected source receipts."""
    sequence, page, offset, variant, status = (receipt[k] for k in ('sequence', 'page', 'offset', 'requestVariant', 'status'))
    hanley = source == 'hanley'
    if (PROFILES.get(profile) != source or type(sequence) is not int
            or not 1 <= sequence <= (256 if source == 'srs' else 128) or
            (hanley and (page is not None or offset is not None or variant not in ('direct', 'firecrawl'))) or
            (not hanley and (type(page) is not int or type(offset) is not int or not 0 <= page <= 9007199254740991
             or not 0 <= offset <= 9007199254740991 or variant not in ('strict', 'standard'))) or
            (profile in ('newmark_nim_sale_post_v1', 'newmark_nim_lease_post_v1') and offset != page * 100) or
            (profile == 'srs_search_post_v1' and offset != page * 12) or
            (profile == 'kidder_bulk_post_v1' and (variant != 'strict' or page != 0 or offset != 0)) or
            (profile == 'kidder_paged_post_v1' and (variant != 'standard' or offset != page * 50)) or
            (hanley and variant == 'firecrawl' and status is not None) or
            (status is None and not (hanley and variant == 'firecrawl')) or
            (status is not None and (type(status) is not int or not 200 <= status <= 299))):
        raise ValueError('native feed receipt mismatch')
    request = request_bytes(profile, offset)
    if receipt['requestSha256'] != (events.sha256(request) if request is not None else None):
        raise ValueError('native feed request mismatch')
    events.timestamp(receipt['capturedAt'])


def decode_entry(raw, filename, binding, *, source):
    """Verify private bytes and return a projected local representation only."""
    if not isinstance(raw, bytes) or len(raw) > MAX_ENTRY:
        raise ValueError('native feed entry exceeds bound')
    entry = json.loads(raw.decode('utf8'), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (not isinstance(entry, dict) or set(entry) != {'schema', 'profile', 'receipt', 'storageCodec', 'responseBodyBase64', 'provenance'}
            or entry.get('schema') != 'cre-native-json-feed-page/v1' or entry.get('storageCodec') != 'gzip_base64'
            or PROFILES.get(entry.get('profile')) != source):
        raise ValueError('native feed entry mismatch')
    profile = entry['profile']; receipt = entry['receipt']; hanley = source == 'hanley'
    fields = {'sourceKey', 'requestVariant', 'requestSha256', 'sequence', 'page', 'offset', 'status', 'capturedAt',
              'bodySha256', 'bodyBytes', 'storedSha256', 'storedBytes'}
    if not isinstance(receipt, dict) or set(receipt) != fields or receipt.get('sourceKey') != source:
        raise ValueError('native feed receipt mismatch')
    _validate_request_receipt(profile, receipt, source)
    sequence, status = receipt['sequence'], receipt['status']
    if (any(type(receipt[k]) is not int or not 0 <= receipt[k] <= bound for k, bound in
            (('bodyBytes', MAX_BODY), ('storedBytes', MAX_BODY + 65536))) or
            any(not isinstance(receipt[k], str) or not re.fullmatch(r'[a-f0-9]{64}', receipt[k])
                for k in ('bodySha256', 'storedSha256'))):
        raise ValueError('native feed receipt mismatch')
    provenance = {'representation': 'collector_decoded_html_embedded_json_literal_not_wire' if hanley else 'collector_decoded_utf8_text_not_wire',
                  'utf8Decoding': 'collector_string_encoding_unobserved' if hanley else 'fatal_utf8' if profile == 'kidder_bulk_post_v1' else 'replacement_possible',
                  'httpStatusOrigin': 'unobserved' if status is None else 'direct_response', 'finalUrl': 'unobserved',
                  'sourceFreshness': 'unverified', 'identity': 'unverified', 'visibility': 'unverified', 'coverage': 'unverified'}
    if entry['provenance'] != provenance or filename != f"page-{sequence}-{events.sha256(raw)}.json":
        raise ValueError('native feed provenance or filename mismatch')
    try:
        stored = base64.b64decode(entry['responseBodyBase64'], validate=True)
    except (ValueError, TypeError, binascii.Error) as exc:
        raise ValueError('invalid native feed encoding') from exc
    if (base64.b64encode(stored).decode() != entry['responseBodyBase64'] or len(stored) != receipt['storedBytes']
            or hashlib.sha256(stored).hexdigest() != receipt['storedSha256']):
        raise ValueError('native feed stored body mismatch')
    try:
        decoder = zlib.decompressobj(31); body = decoder.decompress(stored, MAX_BODY + 1)
        if len(body) > MAX_BODY or decoder.unconsumed_tail or decoder.unused_data or not decoder.eof:
            raise ValueError('invalid or oversized native feed body')
        body += decoder.flush()
    except zlib.error as exc:
        raise ValueError('invalid native feed compression') from exc
    if len(body) != receipt['bodyBytes'] or events.sha256(body) != receipt['bodySha256']:
        raise ValueError('native feed body mismatch')
    document, literals = acquisition.decode_json(body)
    projected, witnesses, projection = native_feed_projection.project(document, literals, source=source)
    return make_observation(projected, witnesses, projection, receipt=receipt, profile=profile, binding=binding, provenance=provenance), None

SCHEMA = 'cre-native-feed-projected-observation/v1'


def make_observation(projected, literals, projection, *, receipt, profile, binding, provenance):
    """Build an evidence candidate using only projected content and nonsecret metadata."""
    from source_evidence import buildout_response
    buildout_response._binding_checked(binding)
    source = receipt['sourceKey']
    canonical = acquisition.encode_json(json.loads(events.encoded(projected)), literals).encode('utf8')
    if projection.get('projectedCanonicalSha256') != events.sha256(canonical):
        raise ValueError('native projected hash mismatch')
    rows_key = ROW_KEYS[source]
    if source == 'hanley':
        pointer = ''; context = acquisition.PLACEHOLDER; records = projected
    elif isinstance(projected, dict) and isinstance(projected.get(rows_key), list):
        pointer = '/' + rows_key
        context = dict(projected); records = context[rows_key]; context[rows_key] = acquisition.PLACEHOLDER
    else:
        pointer = None; context = projected; records = []
    safe_receipt = {k: receipt[k] for k in ('sourceKey', 'requestVariant', 'requestSha256', 'sequence', 'page', 'offset', 'status', 'capturedAt')}
    safe_receipt.update(projectedCanonicalSha256=events.sha256(canonical), projectedCanonicalBytes=len(canonical))
    return {'schema': SCHEMA, 'profile': profile, 'receipt': safe_receipt, 'arrayPointer': pointer,
            'context': context, 'records': records, 'numericLiterals': literals,
            'captureContext': binding, 'projection': projection,
            'provenance': {**provenance, 'representation': 'source_projected_canonical_json_not_original_body',
                           'originalReconstruction': 'not_possible'}}


def reconstruct(value):
    if not isinstance(value, dict) or value.get('schema') != SCHEMA:
        raise ValueError('invalid native projected observation')
    source = value.get('receipt', {}).get('sourceKey')
    pointer = value.get('arrayPointer')
    allowed = {key: ('',) if row is None else (None, '/' + row) for key, row in ROW_KEYS.items()}
    if source not in allowed or pointer not in allowed[source]:
        raise ValueError('invalid native projected array scope')
    if pointer is None:
        if value.get('records') != []:
            raise ValueError('whole context cannot claim selected rows')
        rows_key = ROW_KEYS[source]
        context = value.get('context')
        if isinstance(context, dict) and isinstance(context.get(rows_key), list):
            raise ValueError('recognized native row array must be selected')
    proxy = {**value, 'schema': acquisition.SCHEMA_CONTEXT if pointer is None else acquisition.SCHEMA}
    return acquisition._reconstruct_values(proxy)



def validate(value):
    """Validate the projected evidence itself, not the inaccessible original."""
    if not isinstance(value, dict) or set(value) != {'schema', 'profile', 'receipt', 'arrayPointer', 'context', 'records',
                                                    'numericLiterals', 'captureContext', 'projection', 'provenance'}:
        raise ValueError('invalid native projected observation')
    receipt = value.get('receipt'); profile = value.get('profile')
    fields = {'sourceKey', 'requestVariant', 'requestSha256', 'sequence', 'page', 'offset', 'status', 'capturedAt',
              'projectedCanonicalSha256', 'projectedCanonicalBytes'}
    if not isinstance(receipt, dict) or set(receipt) != fields or PROFILES.get(profile) != receipt.get('sourceKey'):
        raise ValueError('invalid native projected receipt')
    source = receipt['sourceKey']; status = receipt['status']; hanley = source == 'hanley'
    _validate_request_receipt(profile, receipt, source)
    doc = reconstruct(value); literals = value.get('numericLiterals'); projection = value.get('projection')
    if not isinstance(literals, dict) or not isinstance(projection, dict):
        raise TypeError('invalid native projected representation')
    canonical = acquisition.encode_json(json.loads(events.encoded(doc)), literals).encode('utf8')
    if (type(receipt['projectedCanonicalBytes']) is not int or receipt['projectedCanonicalBytes'] != len(canonical)
            or receipt['projectedCanonicalSha256'] != events.sha256(canonical)):
        raise ValueError('native projected content changed')
    from source_evidence import buildout_response
    buildout_response._binding_checked(value['captureContext'])
    expected_provenance = {'representation': 'source_projected_canonical_json_not_original_body',
        'utf8Decoding': 'collector_string_encoding_unobserved' if hanley else 'fatal_utf8' if profile == 'kidder_bulk_post_v1' else 'replacement_possible',
        'httpStatusOrigin': 'unobserved' if status is None else 'direct_response', 'finalUrl': 'unobserved',
        'sourceFreshness': 'unverified', 'identity': 'unverified', 'visibility': 'unverified', 'coverage': 'unverified',
        'originalReconstruction': 'not_possible'}
    if value['provenance'] != expected_provenance:
        raise ValueError('invalid native projected provenance')
    from source_evidence import native_feed_projection
    again, _, reprojection = native_feed_projection.project(doc, literals, source=source)
    if events.encoded(again) != events.encoded(doc):
        raise ValueError('unprojected native data')
    allowed = {'hanley': {'nonpublic_or_unknown_visibility'},
               'srs': set(),
               'kidder-mathews': set(), 'newmark': set()}[source]
    if (set(projection) != {'schema', 'sourceKey', 'withheld', 'projectedCanonicalSha256'}
            or projection.get('schema') != 'cre-native-feed-projection/v1' or projection.get('sourceKey') != source
            or projection.get('projectedCanonicalSha256') != events.sha256(canonical)
            or not isinstance(projection.get('withheld'), list) or len(events.encoded(projection['withheld'])) > 1_000_000):
        raise ValueError('invalid native withholding receipt')
    seen = set()
    for notice in projection['withheld']:
        if (not isinstance(notice, dict) or set(notice) != {'pointer', 'reason'} or notice.get('reason') not in allowed
                or not isinstance(notice.get('pointer'), str) or notice['pointer'] in seen):
            raise ValueError('invalid native withholding witness')
        pointer = notice['pointer']; seen.add(pointer)
        if source == 'hanley' and not re.fullmatch(r'/(?:0|[1-9][0-9]*)', pointer):
            raise ValueError('invalid Hanley withholding pointer')
        if any(k == pointer or k.startswith(pointer + '/') for k in literals):
            raise ValueError('withheld numeric witness remains')
        parts = acquisition._pointer_parts(pointer)
        node = doc
        for part in parts:
            node = node[acquisition._child(node, part)]
        if node is not None:
            raise ValueError('native withholding target is not null')
    if not {item['pointer'] for item in reprojection['withheld']}.issubset(seen):
        raise ValueError('native withholding witness missing')
