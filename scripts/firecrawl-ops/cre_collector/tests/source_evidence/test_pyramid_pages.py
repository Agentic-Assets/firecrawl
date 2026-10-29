import base64
import hashlib
import json
import zlib

import pytest
from source_evidence import events, native_pages, pyramid_pages


def entry(body, *, page=1, run=1, compressed=None):
    compressed = zlib.compress(body, wbits=31) if compressed is None else compressed
    receipt = {'sourceKey': 'pyramid-brokerage', 'requestUrl': pyramid_pages.expected_url(page), 'page': page,
               'pass': run, 'status': 200, 'capturedAt': '2026-10-29T00:00:00Z', 'totalHeader': '2084',
               'totalPagesHeader': '21', 'bodySha256': hashlib.sha256(body).hexdigest(), 'bodyBytes': len(body),
               'storedSha256': hashlib.sha256(compressed).hexdigest(), 'storedBytes': len(compressed)}
    value = {'schema': 'cre-pyramid-decoded-page/v1', 'receipt': receipt, 'storageCodec': 'gzip_base64',
             'responseBodyBase64': base64.b64encode(compressed).decode(), 'provenance': pyramid_pages.PROVENANCE}
    raw = events.encoded(value)
    return raw, f"page-{run}-{page}-{hashlib.sha256(raw).hexdigest()}.json"


def test_reconstruct_all_rows_and_exceptional_numbers():
    raw, filename = entry(b'[{"id":9007199254740993,"amount":1e999},{"id":null},{"other":"x"}]')
    obs, diagnostic = native_pages._decode('pyramid-brokerage', raw, filename, {}, native_format=pyramid_pages.FORMAT)
    assert diagnostic is None and len(obs['records']) == 3 and obs['numericLiterals'] == {'/0/amount': '1e999'}
    events.validate_content_policy(events.plan(obs, source_key='pyramid-brokerage'))
    assert pyramid_pages.reconstruct(obs)[0]['id'] == 9007199254740993


def test_nonarray_is_whole_context_and_malformed_refuses():
    raw, filename = entry(b'{"other": [1,2]}')
    obs, _ = pyramid_pages.decode_entry(raw, filename, {})
    assert obs['records'] == [] and obs['arrayPointer'] is None and pyramid_pages.reconstruct(obs) == {'other': [1, 2]}
    raw, filename = entry(b'{"password":"DUMMY_PRIVATE_VALUE"')
    with pytest.raises(ValueError, match='unparsed Pyramid response is private'):
        pyramid_pages.decode_entry(raw, filename, {})
    forged = dict(obs, jsonStatus='malformed', context='{"password":"DUMMY_PRIVATE_VALUE"')
    with pytest.raises(ValueError, match='unparsed Pyramid response is private'):
        pyramid_pages.validate(forged)



def test_rejects_trailing_gzip_and_oversized_output():
    body = b'[]'
    raw, filename = entry(body, compressed=zlib.compress(body, wbits=31) + b'trailing')
    with pytest.raises(ValueError):
        pyramid_pages.decode_entry(raw, filename, {})
    bomb = b' ' * (pyramid_pages.MAX_BODY + 1)
    raw, filename = entry(bomb)
    with pytest.raises(ValueError):
        pyramid_pages.decode_entry(raw, filename, {})


def test_source_scope_and_credential_scan():
    raw, filename = entry(b'[{"url":"https://example.test/?token=secret"}]')
    with pytest.raises(ValueError):
        pyramid_pages.decode_entry(raw, filename, {})
    assert not native_pages.format_supported('transwestern', pyramid_pages.FORMAT)
    assert native_pages.format_supported('pyramid-brokerage', pyramid_pages.FORMAT)


@pytest.mark.parametrize("document", [
    '{"password":"DUMMY_PRIVATE_VALUE"}',
    {"payload": '{"password":"DUMMY_PRIVATE_VALUE"}'},
    [json.dumps('{"password":"DUMMY_PRIVATE_VALUE"}')],
    {"payload": '{"pass\\u0077ord":"DUMMY_PRIVATE_VALUE"}'},
    {"password": "DUMMY_PRIVATE_VALUE"},
])
def test_encoded_credential_labels_refuse(document):
    raw, filename = entry(json.dumps(document).encode())
    with pytest.raises(ValueError, match='credential'):
        pyramid_pages.decode_entry(raw, filename, {})


def test_clean_json_strings_are_preserved():
    document = [{"payload": '{"nested":[1,{"note":"hello"}]}', "text": '[ordinary prose'}]
    raw, filename = entry(json.dumps(document).encode())
    obs, _ = pyramid_pages.decode_entry(raw, filename, {})
    assert pyramid_pages.reconstruct(obs) == document
    events.validate_content_policy(events.plan(obs, source_key='pyramid-brokerage'))


def test_encoded_inspection_limits_refuse():
    value = '{}'
    for _ in range(17):
        value = json.dumps(value)
    with pytest.raises(ValueError, match='inspection exceeds limit'):
        pyramid_pages._inspect_encoded_credentials(value)
    with pytest.raises(ValueError, match='inspection exceeds limit'):
        pyramid_pages._inspect_encoded_credentials('[' + ' ' * pyramid_pages.MAX_BODY + ']')
