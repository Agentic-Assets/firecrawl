"""Bounded private Pyramid collector-decoded JSON pages, not wire receipts."""
import base64
import binascii
import copy
import hashlib
import json
import re
import zlib
from urllib.parse import urlencode

from buildout_observation_artifact import invalid_number, unique_object

from source_evidence import acquisition, buildout_response, events
from source_evidence.encoded_json_inspection import _inspect_encoded_credentials

FORMAT = 'pyramid_inventory_decoded_v1'
SCHEMA = 'cre-pyramid-decoded-observation/v1'
MAX_BODY = 16 * 1024 * 1024
MAX_ENTRY = ((MAX_BODY + 65536) * 4 + 2) // 3 + 16384
PROVENANCE = {'representation': 'collector_decoded_utf8_text_not_wire', 'utf8Decoding': 'replacement_possible',
              'httpStatusOrigin': 'collector_final_response', 'finalUrl': 'unobserved',
              'sourceFreshness': 'unverified', 'identity': 'unverified', 'visibility': 'unverified', 'coverage': 'unverified'}

def expected_url(page):
    return 'https://www.pyramidbrokerage.com/wp-json/wp/v2/pbc-listings?' + urlencode(
        {'per_page': '100', 'page': str(page), 'orderby': 'id', 'order': 'asc', '_embed': 'wp:term'})

def receipt_checked(receipt):
    keys = {'sourceKey', 'requestUrl', 'page', 'pass', 'status', 'capturedAt', 'totalHeader', 'totalPagesHeader',
            'bodySha256', 'bodyBytes', 'storedSha256', 'storedBytes'}
    if not isinstance(receipt, dict) or set(receipt) != keys or receipt.get('sourceKey') != 'pyramid-brokerage':
        raise ValueError('Pyramid receipt mismatch')
    page, run = receipt['page'], receipt['pass']
    if (type(page) is not int or not 1 <= page <= 9007199254740991 or type(run) is not int or not 1 <= run <= 5
            or receipt['requestUrl'] != expected_url(page) or type(receipt['status']) is not int
            or not 200 <= receipt['status'] <= 299 or type(receipt['bodyBytes']) is not int
            or not 0 <= receipt['bodyBytes'] <= MAX_BODY or type(receipt['storedBytes']) is not int
            or not 0 <= receipt['storedBytes'] <= MAX_BODY + 65536
            or any(not isinstance(receipt[k], str) or not re.fullmatch(r'[a-f0-9]{64}', receipt[k])
                   for k in ('bodySha256', 'storedSha256'))
            or any(receipt[k] is not None and (not isinstance(receipt[k], str) or not re.fullmatch(r'[0-9]{1,16}', receipt[k]))
                   for k in ('totalHeader', 'totalPagesHeader'))):
        raise ValueError('Pyramid receipt mismatch')
    events.timestamp(receipt['capturedAt'])


def convert(document, literals, *, receipt, capture_context, json_status="parsed"):
    receipt_checked(receipt)
    buildout_response._binding_checked(capture_context)
    if json_status != "parsed":
        raise ValueError("unparsed Pyramid response is private and cannot be admitted")
    _inspect_encoded_credentials(document)
    if not isinstance(literals, dict):
        raise ValueError('invalid numeric witnesses')  # noqa: TRY004
    encoded = acquisition.encode_json(document, literals).encode('utf8')
    decoded, witnesses = acquisition.decode_json(encoded)
    if events.encoded(decoded) != events.encoded(document) or events.encoded(witnesses) != events.encoded(literals):
        raise ValueError('numeric witness mismatch')
    if isinstance(document, list):
        if len(document) > 10000:
            raise ValueError('page exceeds row bound')
        records, context, pointer = copy.deepcopy(document), copy.deepcopy(acquisition.PLACEHOLDER), ''
    else:
        records, context, pointer = [], copy.deepcopy(document), None
    return {'schema': SCHEMA, 'receipt': copy.deepcopy(receipt), 'arrayPointer': pointer, 'context': context,
            'records': records, 'numericLiterals': copy.deepcopy(literals), 'captureContext': copy.deepcopy(capture_context), 'jsonStatus': json_status,
            'provenance': copy.deepcopy(PROVENANCE)}

def reconstruct(observation):
    pointer = observation.get('arrayPointer')
    if pointer not in (None, ''):
        raise ValueError('invalid Pyramid array pointer')
    return acquisition._reconstruct_values({**observation, 'schema': acquisition.SCHEMA_CONTEXT if pointer is None else acquisition.SCHEMA})

def validate(observation):
    if not isinstance(observation, dict) or observation.get('schema') != SCHEMA:
        raise ValueError('invalid Pyramid observation')
    if convert(reconstruct(observation), observation.get('numericLiterals'), receipt=observation.get('receipt'),
               capture_context=observation.get('captureContext'), json_status=observation.get('jsonStatus')) != observation:
        raise ValueError('Pyramid observation mismatch')

def decode_entry(raw, filename, binding):
    if not isinstance(raw, bytes) or len(raw) > MAX_ENTRY:
        raise ValueError('Pyramid entry exceeds bound')
    entry = json.loads(raw.decode('utf8'), object_pairs_hook=unique_object, parse_constant=invalid_number)
    if (not isinstance(entry, dict) or set(entry) != {'schema', 'receipt', 'storageCodec', 'responseBodyBase64', 'provenance'}
            or entry['schema'] != 'cre-pyramid-decoded-page/v1' or entry['storageCodec'] != 'gzip_base64'
            or entry['provenance'] != PROVENANCE):
        raise ValueError('Pyramid entry mismatch')
    receipt = entry['receipt']
    receipt_checked(receipt)
    if filename != f"page-{receipt['pass']}-{receipt['page']}-{hashlib.sha256(raw).hexdigest()}.json":
        raise ValueError('Pyramid filename mismatch')
    try:
        compressed = base64.b64decode(entry['responseBodyBase64'], validate=True)
    except (TypeError, ValueError, binascii.Error) as exc:
        raise ValueError('invalid compressed encoding') from exc
    if (base64.b64encode(compressed).decode() != entry['responseBodyBase64'] or len(compressed) != receipt['storedBytes']
            or hashlib.sha256(compressed).hexdigest() != receipt['storedSha256']):
        raise ValueError('stored body mismatch')
    try:
        decoder = zlib.decompressobj(31)
        body = decoder.decompress(compressed, MAX_BODY + 1)
        if len(body) > MAX_BODY or decoder.unconsumed_tail or not decoder.eof or decoder.unused_data:
            raise ValueError('invalid or oversized compressed body')
        body += decoder.flush()
    except zlib.error as exc:
        raise ValueError('invalid compressed body') from exc
    if len(body) != receipt['bodyBytes'] or hashlib.sha256(body).hexdigest() != receipt['bodySha256']:
        raise ValueError('decoded body mismatch')
    try:
        document, literals = acquisition.decode_json(body)
    except (ValueError, UnicodeError) as exc:
        raise ValueError('unparsed Pyramid response is private and cannot be admitted') from exc
    return convert(document, literals, receipt=receipt, capture_context=binding), None
