import json

import pytest
from source_evidence import document_export, events


def plan(value):
    text = json.dumps(value, ensure_ascii=True, separators=(',', ':'))
    artifact = {'schema_version': 'cre-document-text/v1',
                'source': {'url': 'https://example.test/file.pdf', 'observed_at': '2026-10-28T12:00:00Z'},
                'parser': {'name': 'docling-document-json', 'version': '2.95.0'},
                'extraction': {'status': 'extracted', 'coverage': 'unknown', 'text_format': 'docling_json',
                               'text': text, 'text_sha256': events.sha256(text.encode())}}
    return events.plan(artifact, source_key='svn')


def test_export_exact_json_utf8_and_unknown_values(tmp_path):
    value = {'unknown': {'unicode': 'Δ', 'cells': [{'x': 1.2, 'empty': None}]}}
    source = plan(value)
    events.validate_content_policy(source)
    result = document_export.export(source['events'], tmp_path / 'result')
    assert result['text_file'] == 'document.docling.json'
    assert (tmp_path / 'result/document.docling.json').read_text() == json.dumps(value, ensure_ascii=True, separators=(',', ':'))


@pytest.mark.parametrize('value', [
    {'payload': {'password': 'DUMMY'}},
    {'payload': json.dumps({'password': 'DUMMY'})},
    {'payload': 'https://example.test/?token=DUMMY'},
    {'payload': 'https://jll.com/private'},
    {'payload': json.dumps({'url': 'https://jll.com/private'})},
    {'payload': json.dumps(json.dumps({'password': 'DUMMY'}))},
    {'payload': {'sourceKey': 'jll'}},
    {'payload': '{"pass\\u0077ord":"DUMMY"}'},
])
def test_decoded_nested_content_is_scanned(value):
    with pytest.raises(ValueError):
        events.validate_content_policy(plan(value))


def test_duplicate_native_json_is_refused():
    source = plan({'safe': 1})
    header = source['events'][0]['facts'][0]['value']
    raw = '{"x":1,"x":2}'
    header['extraction'].update(text=raw, text_sha256=events.sha256(raw.encode()))
    with pytest.raises(ValueError):
        events.validate_content_policy(events.plan(header, source_key='svn'))
