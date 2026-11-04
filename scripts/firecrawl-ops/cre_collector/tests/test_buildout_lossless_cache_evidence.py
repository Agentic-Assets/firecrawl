"""Synthetic offline evidence; no provider access or database writes."""
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import buildout_lossless_cache_evidence as evidence


def fixture():
    rows = [b'{"id":null,"x":1.00e+02,"x":-0,"nested":{"unknown":false,"unknown":null},"index_attributes":[["Spaces","1"],["Spaces","2"]]}' for _ in range(84)]
    return b'{"data":{"inventory":[' + b','.join(rows) + b']}}'


def entries(node, key):
    return [entry['value'] for entry in node['entries'] if entry.get('key') == key]


def test_duplicate_keys_numbers_and_eighty_four_rows_are_preserved():
    raw = fixture()
    result, summary = evidence.build(raw)
    doc = json.loads(result)
    assert summary['records'] == 84
    assert doc['inputSha256'] == hashlib.sha256(raw).hexdigest()
    assert doc['identityResolution'] == 'unresolved' and doc['promotion'] == 'not_authorized'
    inventory = entries(entries(doc['tree'], 'data')[0], 'inventory')[0]
    first = inventory['items'][0]
    assert [x['token'] for x in entries(first, 'x')] == ['1.00e+02', '-0']
    assert [x['kind'] for x in entries(entries(first, 'nested')[0], 'unknown')] == ['boolean', 'null']
    assert len(doc['recordOrdinalPaths']) == 84


@pytest.mark.parametrize('raw', [b'{"data":{},"data":{"inventory":[{}]}}', b'{"data":{"inventory":[{}],"inventory":[{}]}}'])
def test_ambiguous_inventory_is_preserved_without_guessing(raw):
    result, summary = evidence.build(raw)
    doc = json.loads(result)
    assert summary['inventoryResolution'] == 'ambiguous_or_missing'
    assert doc['recordOrdinalPaths'] == []
    assert len(doc['tree']['entries']) > 0


def test_recognized_nested_secret_pairs_quarantined_with_safe_paths():
    raw = b'{"data":{"inventory":[{"nested":{"index_attributes":[["API Key","dummy-secret"],["City","Safe"]]},"buildoutInventoryObservation":{"attributes":[["password","private"]]}}]}}'
    result, summary = evidence.build(raw)
    assert summary['quarantinedItems'] == 2
    assert b'dummy-secret' not in result and b'private"' not in result
    assert b'Safe' in result
    assert all(all(isinstance(part, int) for part in item['ordinalPath']) for item in json.loads(result)['quarantine'])


def test_input_bounds_and_invalid_values_fail_without_partial_output(monkeypatch):
    monkeypatch.setattr(evidence, 'MAX_INPUT', 3)
    with pytest.raises(ValueError, match='input exceeds'):
        evidence.build(fixture())
    monkeypatch.setattr(evidence, 'MAX_INPUT', 1024)
    with pytest.raises(ValueError, match='non-finite'):
        evidence.build(b'{"x":NaN}')


def test_quarantined_object_entries_count_toward_node_budget(monkeypatch):
    raw = b'{"password":1,"password":2,"password":3,"password":4}'
    monkeypatch.setattr(evidence, 'MAX_NODES', 4)
    with pytest.raises(ValueError, match='structure exceeds bound'):
        evidence.build(raw)
    monkeypatch.setattr(evidence, 'MAX_NODES', 5)
    result, summary = evidence.build(raw)
    assert summary['quarantinedItems'] == 4
    assert b'password' not in result


def test_cli_private_exclusive_and_input_unmodified(tmp_path):
    raw = fixture()
    source, target = tmp_path / 'cache.json', tmp_path / 'output'
    source.write_bytes(raw)
    os.chmod(source, 0o600)
    cmd = [sys.executable, str(Path(evidence.__file__)), '--input', str(source), '--out-dir', str(target)]
    result = subprocess.run(cmd, text=True, capture_output=True, check=True)
    assert json.loads(result.stdout)['records'] == 84
    assert source.read_bytes() == raw
    assert target.stat().st_mode & 0o077 == 0
    assert (target / 'evidence.json').stat().st_mode & 0o077 == 0
    assert subprocess.run(cmd, text=True, capture_output=True, check=False).returncode != 0
