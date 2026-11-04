"""Offline Docling JSON artifact construction; no container lifecycle or database access."""
import hashlib
import json
import math
import os
import re
import sys
from collections import deque
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import ocr
import poppler_modes

FORMAT = 'docling_json'
MAX_TEXT = ocr.MAX_TEXT


def validate_core_references(document):
    """Check pinned known hierarchy references without walking unknown source fields."""
    nodes = {}
    for root in ('body', 'furniture'):
        item = document[root]
        if item.get('self_ref') != '#/' + root or not isinstance(item.get('children'), list):
            raise ValueError('invalid Docling root')
        nodes[item['self_ref']] = item
    for key in ('groups', 'texts', 'pictures', 'tables', 'key_value_items', 'form_items'):
        items = document.get(key, [])
        if not isinstance(items, list) or len(items) > 50000 - len(nodes):
            raise ValueError('invalid or oversized Docling node list')
        for ordinal, item in enumerate(items):
            ref = f'#/{key}/{ordinal}'
            if not isinstance(item, dict) or item.get('self_ref') != ref or not isinstance(item.get('children'), list):
                raise ValueError('invalid Docling node identity')
            nodes[ref] = item
    edges = {}
    degree = {key: 0 for key in nodes}
    count = 0
    for ref, item in nodes.items():
        targets = []
        for child in item['children']:
            count += 1
            if count > 100000 or not isinstance(child, dict) or not isinstance(child.get('$ref'), str):
                raise ValueError('invalid or oversized Docling child references')
            target = child['$ref']
            if target not in nodes:
                raise ValueError('unresolved Docling child reference')
            targets.append(target)
            degree[target] += 1
        edges[ref] = targets
    ready = deque(ref for ref, count in degree.items() if count == 0)
    visited = 0
    while ready:
        current = ready.popleft()
        visited += 1
        for target in edges[current]:
            degree[target] -= 1
            if degree[target] == 0:
                ready.append(target)
    if visited != len(nodes):
        raise ValueError('cyclic Docling child references')


def initialize(pdf, first_raw, second_raw, *, config):
    """Validate ordered normal-mode priors and create an explicitly failed attempt."""
    first = json.loads(first_raw, object_pairs_hook=ocr.unique_object, parse_constant=ocr.invalid_number)
    second = json.loads(second_raw, object_pairs_hook=ocr.unique_object, parse_constant=ocr.invalid_number)
    if not isinstance(first, dict) or not isinstance(second, dict):
        raise TypeError('prior attempt must be an object')
    attempt = ocr.make_attempt(first, first_raw, pdf, 'poppler', 'validated', relationship='complement')
    fp = first.get('parser', {})
    expected_first = {'ocr': 'disabled', 'max_bytes': 100 * 1024 * 1024, 'max_text_bytes': MAX_TEXT, 'timeout_ms': 60000}
    if fp.get('version') != '0.2.4' or fp.get('config') != expected_first:
        raise ValueError('invalid AnyDoc parser')
    parser, extraction = second.get('parser', {}), second.get('extraction', {})
    pc = parser.get('config', {})
    executable = pc.get('executable') if isinstance(pc, dict) else None
    mode = poppler_modes.mode_from_config(pc)
    expected = poppler_modes.config(mode, executable=executable)
    if (second.get('schema_version') != 'cre-document-text/v1' or second.get('source') != first.get('source')
            or parser.get('name') != 'poppler-pdftotext' or not isinstance(parser.get('version'), str)
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)+', parser['version'])
            or not isinstance(executable, str) or not os.path.isabs(executable) or pc != expected
            or extraction.get('coverage') != 'unknown' or extraction.get('status') not in ('extracted', 'failed')
            or extraction.get('text_format') != 'plain' or extraction.get('relationship') != 'complement_of'
            or extraction.get('previous_attempt_sha256') != ocr.digest(first_raw)
            or not isinstance(extraction.get('warnings'), list)
            or any(not isinstance(w, str) for w in extraction['warnings'])):
        raise ValueError('invalid normal Poppler attempt')
    text = extraction.get('text')
    if extraction['status'] == 'extracted':
        if (not isinstance(text, str) or not text.strip() or len(text.encode()) > MAX_TEXT
                or extraction.get('text_sha256') != ocr.digest(text.encode()) or extraction.get('error_code') is not None
                or type(extraction.get('converter_exit_code')) is not int):
            raise ValueError('invalid prior Poppler text')
        if extraction['converter_exit_code'] != 0 and 'converter_failed_partial_output' not in extraction['warnings']:
            raise ValueError('unreported partial Poppler output')
    elif text is not None or extraction.get('text_sha256') is not None or not isinstance(extraction.get('error_code'), str):
        raise ValueError('invalid failed Poppler outcome')
    attempt['parser'] = {'name': 'docling-document-json', 'version': '2.95.0', 'config': config}
    attempt['extraction'] = {'status': 'failed', 'coverage': 'unknown', 'text_format': FORMAT,
        'text': None, 'text_sha256': None, 'error_code': None, 'warnings': [],
        'relationship': 'complement_of', 'previous_attempt_sha256': ocr.digest(second_raw),
        'prior_attempt_sha256': [ocr.digest(first_raw), ocr.digest(second_raw)],
        'page_count': None, 'page_range': None, 'omitted_pages': None}
    return attempt


def validate_native(document_json, *, first_page, last_page):
    """Check native core without changing or normalizing its original bytes."""
    text = document_json.decode('utf-8')
    parsed = json.loads(text, object_pairs_hook=ocr.unique_object, parse_constant=ocr.invalid_number)
    if (not isinstance(parsed, dict) or parsed.get('schema_name') != 'DoclingDocument'
            or parsed.get('version') != '1.10.0' or not isinstance(parsed.get('name'), str)
            or any(not isinstance(parsed.get(k), dict) for k in ('body', 'furniture', 'pages'))
            or any(not isinstance(parsed.get(k), list) for k in ('groups', 'texts', 'pictures', 'tables'))):
        raise ValueError('unsupported native Docling JSON')
    validate_core_references(parsed)
    pages = parsed['pages']
    for key, value in pages.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[1-9][0-9]*', key) or not isinstance(value, dict)
                or type(value.get('page_no')) is not int or value['page_no'] != int(key)
                or not isinstance(value.get('size'), dict)
                or any(type(value['size'].get(k)) not in (int, float) or not math.isfinite(value['size'][k]) or value['size'][k] <= 0 for k in ('width', 'height'))
                or not first_page <= int(key) <= last_page):
            raise ValueError('invalid Docling page identity')
    expected = {str(i) for i in range(first_page, last_page + 1)}
    missing = len(expected - set(pages))
    return text, missing

def build(pdf, first_raw, second_raw, document_json, *, config, page_count, last_page, first_page=1, partial=False):
    """Bind exact native converter bytes to the PDF and two ordered prior attempts."""
    attempt = initialize(pdf, first_raw, second_raw, config=config)
    if (type(page_count) is not int or type(first_page) is not int or type(last_page) is not int
            or not 1 <= first_page <= last_page <= page_count or last_page - first_page + 1 > 1000
            or type(partial) is not bool):
        raise ValueError('invalid page coverage')
    if not isinstance(document_json, bytes) or not 1 <= len(document_json) <= MAX_TEXT:
        raise ValueError('invalid or oversized Docling JSON')
    text, missing = validate_native(document_json, first_page=first_page, last_page=last_page)
    attempt['extraction'].update(status='extracted', text=text, text_sha256=hashlib.sha256(document_json).hexdigest(),
        page_count=page_count, page_range=[first_page, last_page],
        omitted_pages=page_count - (last_page - first_page + 1), unrepresented_requested_pages=missing,
        warnings=['coverage_unknown'] + (['pages_omitted'] if page_count > last_page - first_page + 1 else [])
                 + (['converter_partial'] if partial else []) + (['requested_pages_unrepresented'] if missing else []))
    return attempt
