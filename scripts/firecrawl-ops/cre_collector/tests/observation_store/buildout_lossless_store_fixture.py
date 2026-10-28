"""Synthetic-only lossless evidence to fixture store arguments; no DB access.

Marker and hashes do not establish source authorization or confidentiality.
This planner is not permitted for real data.
"""
import hashlib
import json
import re

import buildout_lossless_cache_evidence as lossless

MAX_ARTIFACT = 16 * 1024 * 1024
CAPTURE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def plan(*, original, evidence, capture_id, slot=0):
    if not isinstance(capture_id, str) or not CAPTURE.fullmatch(capture_id):
        raise ValueError('invalid capture identifier')
    if type(slot) is not int or not 0 <= slot <= 1_000_000:
        raise ValueError('invalid fixture slot')
    source = lossless.decode(original)
    marker = lossless.unique_field(source, 'syntheticFixture')
    if marker is None or marker[1] is not True:
        raise ValueError('synthetic fixture marker required')
    rebuilt, _ = lossless.build(original)
    if rebuilt != evidence:
        raise ValueError('lossless evidence differs from reconstructed input')
    doc = json.loads(evidence)
    paths = doc['recordOrdinalPaths']
    inventory_status = doc['inventoryResolution']
    omitted_record_views = 0
    if len(paths) >= 1000:
        omitted_record_views = len(paths)
        paths = []
        inventory_status = 'record_limit_capture_only'
    tree = doc['tree']
    records = []
    for index, path in enumerate(paths):
        if len(path) != 3 or path[2] != index:
            raise ValueError('invalid ordinal paths')
        data = tree['entries'][path[0]]['value']
        inventory = data['entries'][path[1]]['value']
        row = inventory['items'][index]
        records.append({'sourceOrdinal': index, 'identityResolution': 'unresolved', 'evidence': row})
    # Keep the screened full capture as unresolved evidence too, including
    # ambiguous envelope candidates and unmapped wrapper fields. The original
    # bytes are never sent; screening does not establish confidentiality.
    records.append({'kind': 'capture_evidence', 'identityResolution': 'unresolved',
                    'inventoryResolution': inventory_status, 'evidence': tree})
    payload = {'schema': 'buildout-lossless-synthetic-store-fixture-v1', 'records': records}
    artifact = (json.dumps(payload, ensure_ascii=True, separators=(',', ':')) + '\n').encode('ascii')
    if len(artifact) > MAX_ARTIFACT:
        raise ValueError('fixture artifact exceeds bound')
    assertions = {'source': None, 'generation': None, 'page': None, 'transaction': 'unknown',
                  'origin': 'caller_asserted_unverified'}
    provenance = {'sourceAuthorization': 'not_evaluated', 'sourceCompleteness': 'unverified',
                  'promotion': 'not_authorized', 'privacy': 'synthetic_fixture_only_unreviewed',
                  'quarantinedItems': len(doc['quarantine']), 'inventoryResolution': inventory_status,
                  'omittedRecordViews': omitted_record_views,
                  'sourceEvidenceSha256': digest(evidence)}
    return {'p_capture_id': capture_id, 'p_capture_page': slot, 'p_assertions': assertions,
            'p_provenance': provenance, 'p_original_sha256': digest(original),
            'p_original_bytes_length': len(original), 'p_artifact_bytes': artifact,
            'p_artifact_sha256': digest(artifact), 'p_observations': records}
