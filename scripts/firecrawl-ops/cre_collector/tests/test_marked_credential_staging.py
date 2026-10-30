"""Offline marked staging credential boundaries; synthetic values only."""
import json
import os
import re
import subprocess
import sys
from html import escape
from urllib.parse import quote

import pytest
from test_srs_public_facts import listing as srs_listing

import cre_ingest
from source_fields.public_facts import _InspectionLimit, _sanitize_encoded_labels


@pytest.mark.parametrize('text', [
    'apiKey=EXAMPLE_PRIVATE_MATERIAL',
    'https://example.test/?apiKey=EXAMPLE_PRIVATE_MATERIAL&next=ok',
    'api%4Bey%3DEXAMPLE_PRIVATE_MATERIAL',
    'api&#75;ey&#61;EXAMPLE_PRIVATE_MATERIAL',
    '{"note":"apiKey=EXAMPLE_PRIVATE_MATERIAL"}',
    json.dumps(json.dumps({'note': 'apiKey=EXAMPLE_PRIVATE_MATERIAL'})),
    'Basic dXNlcjpwYXNz',
    'Basic%20dXNlcjpwYXNz',
    'Basic&#32;dXNlcjpwYXNz',
    'Authorization: Basic EXAMPLE_PRIVATE_MATERIAL',
    'Authorization: Basic \"EXAMPLE_PRIVATE_MATERIAL\"',
])
def test_marked_inspection_omits_credentials_and_copies(text):
    token = 'dXNlcjpwYXNz' if 'dXNlcjpwYXNz' in text else 'EXAMPLE_PRIVATE_MATERIAL'
    raw = {'observation': text, 'array': [None, False, 0, '', [], {'copy': token}],
           'key_' + token: 'metadata', 'encoded_copy': token.replace('E', '%45', 1)}
    staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
    encoded = json.dumps(staged)
    assert token not in encoded
    assert staged['array'][:5] == [None, False, 0, '', []]
    assert changes
    assert raw['observation'] == text


def test_basic_credentials_remove_decoded_copy_and_ordinary_prose_survives():
    raw = {'credential': 'Basic dXNlcjpwYXNz', 'copy': 'user:pass', 'other_copy': 'pass',
           'escaped': '%70ass', 'pass': 'key-copy', 'username': 'user', 'prose': 'Basic rent'}
    # Use a non-credential label so the test exercises the value grammar.
    raw['observation'] = raw.pop('credential')
    staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
    assert staged['observation'] is None and staged['copy'] is None
    assert staged['other_copy'] is None and staged['escaped'] is None
    assert 'pass' not in staged and staged['username'] == 'user'
    assert staged['prose'] == 'Basic rent' and changes


@pytest.mark.parametrize(('other', 'token'), [('Bearer DIFFERENT_TOKEN', 'DIFFERENT_TOKEN'),
                                           ('Basic dXNlcjpwYXNz', 'dXNlcjpwYXNz')])
def test_json_mixed_credential_types_remove_each_independent_copy(other, token):
    encoded = json.dumps({'apiKey': 'EXAMPLE_PRIVATE_MATERIAL', 'other': other})
    raw = {'observation': encoded, 'first': 'EXAMPLE_PRIVATE_MATERIAL', 'second': token,
           'values': [None, False, 0, '']}
    staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
    assert staged['observation'] is None and staged['first'] is None and staged['second'] is None
    assert staged['values'] == [None, False, 0, ''] and changes
    assert raw['first'] == 'EXAMPLE_PRIVATE_MATERIAL'


@pytest.mark.parametrize('encode', [lambda value: quote(value, safe=''), escape])
def test_encoded_json_credential_key_value_and_independent_copy(encode):
    encoded = encode(json.dumps({'apiKey': 'EXAMPLE_PRIVATE_MATERIAL', 'note': 'ordinary'}))
    raw = {'observation': encoded, 'copy': 'EXAMPLE_PRIVATE_MATERIAL', 'other': [None, False, 0, '']}
    staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
    assert staged['observation'] is None and staged['copy'] is None
    assert staged['other'] == [None, False, 0, ''] and changes
    assert raw['copy'] == 'EXAMPLE_PRIVATE_MATERIAL'


def test_json_key_with_bearer_and_independent_credential_label_retains_value_binding():
    encoded = json.dumps({'apiKey Bearer OTHER_TOKEN': 'EXAMPLE_PRIVATE_MATERIAL'})
    raw = {'observation': encoded, 'first': 'EXAMPLE_PRIVATE_MATERIAL', 'second': 'OTHER_TOKEN'}
    staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
    assert staged['observation'] is None and staged['first'] is None and staged['second'] is None
    assert changes and raw['first'] == 'EXAMPLE_PRIVATE_MATERIAL'


@pytest.mark.parametrize('layers', [5, 6])
def test_encoded_json_decode_bound_never_exposes_credential_copy(layers):
    encoded = json.dumps({'apiKey': 'EXAMPLE_PRIVATE_MATERIAL', 'other': 'Bearer OTHER_TOKEN'})
    for _ in range(layers):
        encoded = quote(encoded, safe='')
    raw = {'observation': encoded, 'copy': 'EXAMPLE_PRIVATE_MATERIAL'}
    if layers == 6:
        with pytest.raises(_InspectionLimit):
            _sanitize_encoded_labels(raw, {}, inspect_strings=True)
    else:
        staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
        assert staged['observation'] is None and staged['copy'] is None and changes
    assert raw['copy'] == 'EXAMPLE_PRIVATE_MATERIAL'


def test_safe_json_business_text_stays_unchanged():
    business = json.dumps(json.dumps({'description': 'Basic rent', 'items': [None, False, 0, '']}))
    raw = {'description': business}
    staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
    assert staged == raw and not changes


def test_newmark_default_does_not_enable_marked_value_grammar():
    raw = {'observation': 'apiKey=EXAMPLE_PRIVATE_MATERIAL', 'other': 'Basic dXNlcjpwYXNz'}
    staged, _, changes = _sanitize_encoded_labels(raw, {})
    assert staged == raw and not changes


@pytest.mark.parametrize('text,token', [
    ('apiKey=EXAMPLE_PRIVATE_MATERIAL', 'EXAMPLE_PRIVATE_MATERIAL'),
    ('Basic dXNlcjpwYXNz', 'dXNlcjpwYXNz'),
])
def test_srs_marked_staging_inspects_broker_copies_and_keeps_source(text, token):
    value = srs_listing()
    value['rawSrs']['apto_data']['description'] = text
    value['rawSrs']['apto_data']['unknown'] = [token, None, False, 0, '']
    value['description'] = text
    value['brokerIds'] = [0]
    brokers = {0: {'name': token, 'email': 'safe@example.test'}}
    row = cre_ingest.to_row(value, brokers, None)
    assert token not in str(row)
    assert value['rawSrs']['apto_data']['description'] == text
    assert brokers[0]['name'] == token
    assert row['extra_facts']['cre_public_listing_facts']['captureCompleteness'] == 'partial'


@pytest.mark.parametrize('text,token', [
    ('apiKey=EXAMPLE_PRIVATE_MATERIAL', 'EXAMPLE_PRIVATE_MATERIAL'),
    ('Basic dXNlcjpwYXNz', 'dXNlcjpwYXNz'),
])
def test_buildout_mapper_to_dryrun_sql_keeps_private_source_and_removes_copies(tmp_path, text, token):
    script = r'''
import { allBuildoutFeeds } from './sources/buildout-feeds/index.js';
import { buildoutCache, buildoutQueryFingerprint, srcBuildout } from './sources/buildout.js';
import { brokers } from './lib/broker.js';
globalThis.fetch = async () => { throw Error('unexpected transport blocked'); };
const feed = allBuildoutFeeds.find(f => f.sourceKey === 'bradford-allen');
const opts = {cacheSlug:feed.feedKey};
const [text,token] = JSON.parse(process.env.SYNTHETIC_TEST_VALUES);
const raw = {id:42,sale:true,show_link:'https://broker.example/show?propertyId=42-sale',
 index_attributes:[['Cap Rate','6.25%'],['Other',text]], unknown:{copy:token},
 broker_contacts:[{name:token,email:'safe@example.test'}]};
buildoutCache.set(`${feed.pluginKey}\u0000${buildoutQueryFingerprint(opts)}`, {items:[raw],total:1});
const result = await srcBuildout(feed.company,feed.pluginKey,feed.listingsPage,'sale',1,false,opts);
process.stdout.write(JSON.stringify({listing:{sourceKey:feed.sourceKey,transactionMode:'sale',...result.listings[0]},brokers}));
'''
    env = {**os.environ, 'SYNTHETIC_TEST_VALUES': json.dumps([text, token])}
    mapped = subprocess.run(['node', '--import', 'tsx', '--input-type=module', '-e', script],
                            capture_output=True, text=True, env=env, check=True)
    result = json.loads(mapped.stdout)
    value = result['listing']
    assert value['rawBuildout']['unknown']['copy'] == token
    assert value.get('capRatePct') is None
    row = cre_ingest.to_row(value, dict(enumerate(result['brokers'])), None)
    assert row['cap_rate'] is None and token not in str(row)
    assert row['extra_facts']['cre_public_listing_facts']['captureCompleteness'] == 'partial'
    artifact = {'runMeta': {'mode': 'full', 'finishedAt': '2026-10-29T00:00:00Z'},
                'sources': [{'sourceKey': 'bradford-allen'}], 'listings': [value],
                'brokers': result['brokers'], 'totalListings': 1}
    path = tmp_path / 'artifact.json'
    path.write_text(json.dumps(artifact))
    path.chmod(0o600)
    dryrun = subprocess.run([sys.executable, 'cre_ingest.py', '--in', str(path), '--dry-run'],
                            capture_output=True, text=True, env={**env, 'TMPDIR': str(tmp_path)}, check=True)
    match = re.search(r'^sql: (.+?) \(', dryrun.stderr, re.MULTILINE)
    assert match is not None
    with open(match.group(1)) as staged:
        assert token not in staged.read()
    assert json.loads(path.read_text())['listings'][0]['rawBuildout']['unknown']['copy'] == token


def test_supported_credential_matrix_fails_closed_and_preserves_safe_business():
    """Exercise known forms, nested encodings and independent copy locations."""
    cases = [
        (json.dumps({'apiKey': 'X_PRIVATE_ONE'}), ['X_PRIVATE_ONE']),
        ('apiKey=X_PRIVATE_ONE', ['X_PRIVATE_ONE']),
        ('Basic dXNlcjpwYXNz', ['dXNlcjpwYXNz', 'user:pass', 'pass']),
        ('Authorization: Basic X_PRIVATE_ONE', ['X_PRIVATE_ONE']),
        ('Bearer X_PRIVATE_ONE', ['X_PRIVATE_ONE']),
        (json.dumps({'apiKey': 'X_PRIVATE_ONE', 'other': 'Bearer X_PRIVATE_TWO'}),
         ['X_PRIVATE_ONE', 'X_PRIVATE_TWO']),
    ]
    for case_index, (base, tokens) in enumerate(cases):
        for mode in ('plain', 'json-string', 'html'):
            initial = json.dumps(base) if mode == 'json-string' else escape(base) if mode == 'html' else base
            for layers in range(7):
                encoded = initial
                for _ in range(layers):
                    encoded = quote(encoded, safe='')
                copies = {f'key_{token}': 'key-copy' for token in tokens}
                copies.update({f'key_%{ord(token[0]):02X}{token[1:]}': 'encoded-key-copy' for token in tokens})
                copies.update({f'direct_{i}': token for i, token in enumerate(tokens)})
                copies['array'] = [None, False, 0, '', *tokens]
                copies['encoded'] = [f'%{ord(token[0]):02X}{token[1:]}' for token in tokens]
                copies['html'] = [f'&#{ord(token[0])};{token[1:]}' for token in tokens]
                raw = {'observation': encoded, 'copies': copies}
                decode_layers = layers + (1 if mode == 'html' and initial != base else 0)
                try:
                    staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
                except _InspectionLimit:
                    if decode_layers <= 5:
                        raise AssertionError(f'unexpected inspection refusal: case={case_index}, mode={mode}, layers={layers}') from None
                    continue
                if decode_layers > 5:
                    raise AssertionError(f'unresolved encoding was not refused: case={case_index}, mode={mode}')
                rendered = json.dumps(staged)
                if any(token in rendered for token in tokens) or not changes:
                    raise AssertionError(f'credential copy survived: case={case_index}, mode={mode}, layers={layers}')
                if (staged['copies']['array'][:4] != [None, False, 0, '']
                        or staged['copies']['array'][4:] != [None] * len(tokens)):
                    raise AssertionError('array positions or credential copies changed unexpectedly')
                if (staged['copies']['encoded'] != [None] * len(tokens)
                        or staged['copies']['html'] != [None] * len(tokens)):
                    raise AssertionError('encoded credential copy survived')
                if any(f'key_%{ord(token[0]):02X}{token[1:]}' in staged['copies'] for token in tokens):
                    raise AssertionError('encoded credential key copy survived')
                if raw['observation'] != encoded:
                    raise AssertionError('private source mutated')
    for business in ('Basic rent', json.dumps({'note': 'Basic rent', 'values': [None, False, 0, '']})):
        raw = {'business': business}
        staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
        if staged != raw or changes:
            raise AssertionError('ordinary business text changed')


def test_credential_and_copy_decoding_cross_product():
    """Both original forms and their value/key/array copies obey the same bound."""
    forms = [
        (json.dumps({'apiKey': 'X_PRIVATE_ONE'}), 'X_PRIVATE_ONE'),
        ('apiKey=X_PRIVATE_ONE', 'X_PRIVATE_ONE'),
        ('Basic dXNlcjpwYXNz', 'dXNlcjpwYXNz'),
        ('Authorization: Basic X_PRIVATE_ONE', 'X_PRIVATE_ONE'),
        ('Bearer X_PRIVATE_ONE', 'X_PRIVATE_ONE'),
        (json.dumps({'apiKey': 'X_PRIVATE_ONE', 'other': 'Bearer X_PRIVATE_TWO'}), 'X_PRIVATE_ONE'),
    ]

    def encoded(value, layers, style):
        if not layers:
            return value
        if style == 'percent':
            value = ''.join(f'%{byte:02X}' for byte in value.encode())
        else:
            value = ''.join(f'&#{ord(char)};' for char in value)
        for _ in range(layers - 1):
            value = quote(value, safe='')
        return value

    for case, (base, token) in enumerate(forms):
        for form in ('plain', 'json-string'):
            original = json.dumps(base) if form == 'json-string' else base
            for original_layers in range(7):
                for copy_layers in range(7):
                    for style in ('percent', 'html'):
                        credential = encoded(original, original_layers, 'percent')
                        copy = encoded(token, copy_layers, style)
                        raw = {'observation': credential, 'copy': copy,
                               'array': [None, False, 0, '', copy], f'key_{copy}': 'safe'}
                        try:
                            staged, _, changes = _sanitize_encoded_labels(raw, {}, inspect_strings=True)
                        except _InspectionLimit:
                            if original_layers <= 5 and copy_layers <= 5:
                                raise AssertionError(f'unexpected refusal: case={case}, form={form}, original={original_layers}, copy={copy_layers}, style={style}') from None
                            continue
                        if original_layers > 5 or copy_layers > 5:
                            raise AssertionError(f'excess decode was not refused: case={case}, form={form}, original={original_layers}, copy={copy_layers}, style={style}')
                        if (not changes or staged['copy'] is not None or staged['array'] != [None, False, 0, '', None]
                                or f'key_{copy}' in staged or token in json.dumps(staged)):
                            raise AssertionError(f'credential copy survived: case={case}, form={form}, original={original_layers}, copy={copy_layers}, style={style}')
                        if raw['copy'] != copy:
                            raise AssertionError('private source mutated')
