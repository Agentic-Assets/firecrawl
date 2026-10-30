"""Derived, bounded public Newmark facts for accepted enrichment rows.

The original artifact remains the canonical input and private custody copy.
This projection supplies facts only; it does not establish source identity,
freshness, production admission, or permission to fetch a linked resource.
"""
import copy
import re
from urllib.parse import quote

from buildout_observation_artifact import sensitive_string
from source_evidence import collector, events
from source_evidence.projections import newmark_rcm
from source_fields.public_facts import (
    _build_facts,
    _InspectionLimit,
    _sanitize_encoded_labels,
)

SCHEMA = 'cre-public-listing-facts/v1'
_CODEC = newmark_rcm.VERSION
_DETAIL_URL = re.compile(r'^https://my\.rcm1\.com/handler/modern\.aspx\?pv=[A-Za-z0-9_-]{1,512}$')
_SLUG = re.compile(r'^[A-Za-z0-9._~-]{1,256}$')


def _field(value):
    if not isinstance(value, dict) or value.get('state') != 'value':
        return None
    result = value.get('value')
    return result if (isinstance(result, str) and len(result.encode('utf8')) <= 256
                      and not any(ord(ch) < 32 or ord(ch) == 127 for ch in result)) else None


def _comparable(value):
    return ' '.join(value.strip().split()).lower() if isinstance(value, str) else ''


def _witness_valid(witness):
    required = {'status', 'schema', 'sourceKey', 'sourcePath', 'recordId', 'slug', 'url',
                'inventoryObservedAt', 'sourceName', 'sourceProperty'}
    if not isinstance(witness, dict) or set(witness) != required:
        return False
    record = witness.get('recordId')
    if not isinstance(record, str) or not _SLUG.fullmatch(record):
        return False
    clock = witness.get('inventoryObservedAt')
    if clock is not None:
        try:
            if not isinstance(clock, str) or not clock.endswith('Z'):
                return False
            events.timestamp(clock)
        except ValueError:
            return False
    prop = witness.get('sourceProperty')
    if not isinstance(prop, dict) or set(prop) != {'selection', 'fields'} or prop.get('selection') != 'single':
        return False
    fields = prop.get('fields')
    keys = {'address', 'city', 'stateAbbreviation', 'stateDescription', 'zip', 'countryCode'}
    if not isinstance(fields, dict) or set(fields) != keys:
        return False
    for value in [witness.get('sourceName'), *fields.values()]:
        if not isinstance(value, dict):
            return False
        if value.get('state') in ('missing', 'null', 'unresolved'):
            if set(value) != {'state'}:
                return False
        elif set(value) != {'state', 'value'} or _field(value) is None:
            return False
    return True


def _matched(witness, data):
    if not isinstance(witness, dict) or not isinstance(data, dict):
        return False
    prop = witness.get('sourceProperty')
    if not isinstance(prop, dict) or prop.get('selection') != 'single' or not isinstance(prop.get('fields'), dict):
        return False
    fields, addr = prop['fields'], data.get('addresses')
    if not isinstance(addr, dict):
        return False
    pairs = [(_field(witness.get('sourceName')), data.get('name'), True),
             (_field(fields.get('address')), addr.get('street'), True),
             (_field(fields.get('city')), addr.get('city'), True),
             (_field(fields.get('stateAbbreviation')), addr.get('state'), False),
             (_field(fields.get('zip')), addr.get('zip'), False)]
    if pairs[3][0] is None and pairs[4][0] is None:
        return False
    return all((not required and left is None) or (_comparable(left) and _comparable(left) == _comparable(right))
               for left, right, required in pairs)


def _envelope(observation, projection, index):
    source = observation['sourceObservation']
    data, numeric = newmark_rcm._literal(source['literal'])
    data, numeric, encoded_changes = _sanitize_encoded_labels(data, numeric)
    # A sensitive encoded label is itself not retained in a public pointer.
    omissions = [{'sourcePath': None, 'reason': reason} for _, reason in encoded_changes]
    prefix = f'/sources/0/retainedObservations/{index}/sourceObservation/literal'
    for change in projection['withheld']:
        if change['pointer'].startswith(prefix + '/') and change['reason'] == 'credential_key_removed':
            omissions.append({'sourcePath': change['pointer'][len(prefix):], 'reason': 'credential_key_removed'})
    facts = _build_facts(data, numeric, omissions)
    clock = observation.get('capturedAt')
    if clock is None:
        omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    else:
        try:
            if not isinstance(clock, str) or not clock.endswith('Z'):
                raise ValueError('not UTC')
            events.timestamp(clock)
        except ValueError:
            clock = None
            omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    page = observation.get('requestedPageUrl')
    if not isinstance(page, str) or not _DETAIL_URL.fullmatch(page) or sensitive_string(page):
        page = None
        omissions.append({'sourcePath': None, 'reason': 'credential_reference_omitted'})
    if len(omissions) > 20:
        omissions = omissions[:19] + [{'sourcePath': None, 'reason': 'input_limit'}]
    result = {'schema': SCHEMA, 'sourceKey': 'newmark', 'codec': _CODEC,
              'observedAt': clock, 'sourceUrl': page, 'facts': facts, 'omissions': omissions,
              'captureCompleteness': 'partial' if omissions else 'complete_for_captured_object',
              'sourceCoverage': 'unverified'}
    # The consumer has finite envelope budgets. Do not silently cut values.
    if len(facts) > 1000 or len(events.encoded(result)) > 512000:
        raise ValueError('Newmark public facts exceed consumer bounds')
    return result


def _incomplete_envelope(observation, reason):
    """Report an uninspected record without exposing any of its source values."""
    clock = observation.get('capturedAt')
    try:
        if clock is not None:
            if not isinstance(clock, str) or not clock.endswith('Z'):
                raise ValueError('not UTC')
            events.timestamp(clock)
    except ValueError:
        clock = None
    page = observation.get('requestedPageUrl')
    if not isinstance(page, str) or not _DETAIL_URL.fullmatch(page) or sensitive_string(page):
        page = None
    return {'schema': SCHEMA, 'sourceKey': 'newmark', 'codec': _CODEC,
            'observedAt': clock, 'sourceUrl': page, 'facts': [],
            'omissions': [{'sourcePath': None, 'reason': reason}],
            'captureCompleteness': 'partial', 'sourceCoverage': 'unverified'}


def qualified(raw):
    """Return copied listing facts by exact claim identity, from an accepted artifact."""
    projected, literals, receipt = newmark_rcm.prepare(raw, source_key='newmark')
    newmark_rcm.validate(projected, receipt, source_key='newmark', numeric_literals=literals)
    wrapped = collector.convert(projected, source_key='newmark', numeric_literals=literals, projection=receipt)
    plan = events.plan(wrapped, source_key='newmark')
    events.validate_content_policy(plan)
    observations = projected['sources'][0].get('retainedObservations', [])
    candidates = {}
    for index, row in enumerate(observations):
        source = row.get('sourceObservation') if isinstance(row, dict) else None
        claim = row.get('claim') if isinstance(row, dict) else None
        if not isinstance(source, dict) or not isinstance(claim, dict):
            continue
        witness = source.get('witness')
        if (row.get('newmarkRcmObservation') is not True or source.get('schema') != newmark_rcm.SCHEMA
                or source.get('extraction') != 'observed' or source.get('match') != 'matched'
                or source.get('parsedSerializable') is not True or not isinstance(source.get('literal'), str)
                or claim.get('sourceKey') != 'newmark' or claim.get('identity') != 'unverified'
                or not _witness_valid(witness) or not isinstance(row.get('scrapedDoc'), dict)
                or not isinstance(row['scrapedDoc'].get('metadata'), dict)
                or type(row['scrapedDoc']['metadata'].get('statusCode')) is not int
                or not 200 <= row['scrapedDoc']['metadata']['statusCode'] < 300):
            continue
        slug = witness.get('slug')
        if (not isinstance(slug, str) or not _SLUG.fullmatch(slug) or slug in ('.', '..')
                or witness.get('schema') != 'newmark-nim-link-witness/v1'
                or witness.get('status') != 'observed' or witness.get('sourceKey') != 'newmark'
                or witness.get('sourcePath') != 'raw_data.rawNewmarkNimRecord'
                or witness.get('url') != row.get('requestedPageUrl')
                or not isinstance(witness.get('url'), str) or not _DETAIL_URL.fullmatch(witness['url'])
                or claim.get('externalId') != slug
                or claim.get('url') != 'https://www.nmrk.com/properties/' + quote(slug, safe='._~-')):
            continue
        data, _ = newmark_rcm._literal(source['literal'])
        if not _matched(witness, data):
            continue
        key = (claim['externalId'], claim['url'])
        try:
            envelope = _envelope(row, receipt, index)
        except _InspectionLimit:
            envelope = _incomplete_envelope(row, 'input_limit')
        except (ValueError, RecursionError):
            # Preserve a visible refusal without publishing uninspected source values.
            envelope = _incomplete_envelope(row, 'unverified_content_omitted')
        candidates.setdefault(key, []).append(envelope)
    counts = {}
    for listing in projected['listings']:
        key = (listing.get('id'), listing.get('url'))
        counts[key] = counts.get(key, 0) + 1
    return {key: values[0] for key, values in candidates.items() if len(values) == 1 and counts.get(key) == 1}


def attach(listing, mapping):
    if not isinstance(listing, dict) or listing.get('sourceKey') != 'newmark':
        return listing
    envelope = mapping.get((listing.get('id'), listing.get('url')))
    if envelope is None:
        return listing
    result = copy.deepcopy(listing)
    facts = result.get('extraFacts')
    if facts is not None and not isinstance(facts, dict):
        raise ValueError('Newmark public facts conflict with malformed extraFacts')
    facts = dict(facts or {})
    if 'cre_public_listing_facts' in facts:
        raise ValueError('Newmark public facts already supplied')
    facts['cre_public_listing_facts'] = copy.deepcopy(envelope)
    result['extraFacts'] = facts
    return result
