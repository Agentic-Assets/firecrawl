"""Source-bound public facts from retained Buildout inventory rows."""
import copy
import math
import unicodedata
from functools import lru_cache
from html import unescape
from urllib.parse import parse_qs, unquote, urlsplit

from source_evidence import buildout_projection, buildout_scope, events
from source_fields.public_facts import (
    _build_facts,
    _InspectionLimit,
    _sanitize_encoded_labels,
    _sensitive_encoded_string,
    _stage_referenced_brokers,
    _unstage_referenced_brokers,
    _within,
)

SCHEMA = 'cre-public-listing-facts/v1'
CODEC = 'buildout-public-inventory/v1'


@lru_cache(maxsize=1)
def _registry():
    return {source: frozenset(feeds) for source, feeds in buildout_scope.supported_feed_keys().items()}


def _mapper_id(raw):
    value = raw.get('id')
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return 'true' if value else 'false'
    if isinstance(value, (int, float)) and math.isfinite(value) and abs(value) <= 9007199254740991 and int(value) == value:
        return str(int(value))
    # Parsed JS numbers and composite IDs cannot be independently reconstructed.
    raise ValueError('unverified Buildout mapper identity')


def _clean(value):
    return ' '.join(value.split()) or None if isinstance(value, str) else None


def safe_url(value):
    if not isinstance(value, str) or len(value) > 2048 or _sensitive_encoded_string(value):
        return None
    candidate = value
    for _ in range(5):
        if any(unicodedata.category(ch) == 'Cc' for ch in candidate):
            return None
        following = unquote(unescape(candidate), errors='replace')
        if following == candidate:
            break
        candidate = following
    else:
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return value


def _clock(listing, marker):
    provenance = listing.get('freshnessProvenance')
    value = listing.get('inventoryObservedAt')
    try:
        if (not isinstance(provenance, dict) or provenance.get('detailScope') != 'authoritative_inventory_feed'
                or provenance.get('method') != 'buildout_inventory_feed' or marker.get('inventoryObservedAt') != value
                or not isinstance(value, str) or not value.endswith('Z')):
            return None
        events.timestamp(value)
        return value
    except ValueError:
        return None


def _document_secrets(raw, listing):
    """Seed known native/collector document references and token copies."""
    candidates = [raw.get('pdf_url')] if isinstance(raw, dict) else []
    brochures = listing.get('brochures')
    if isinstance(brochures, list):
        candidates.extend(item.get('url') for item in brochures if isinstance(item, dict))
    secrets = []
    for value in candidates:
        if buildout_projection._recognized(value):
            secrets.append(value)
            token = parse_qs(urlsplit(value).query).get('token', [])
            secrets.extend(token)
    return secrets


def attach(listing, brokers=None):
    if not isinstance(listing, dict):
        return (listing, brokers) if brokers is not None else listing
    marker = listing.get('buildoutRawRetention')
    if not isinstance(marker, dict) or marker.get('schema') != 'buildout-raw-retention-v1':
        return (listing, brokers) if brokers is not None else listing
    listing = copy.deepcopy(listing)
    broker_ids = _stage_referenced_brokers(listing, brokers)
    if isinstance(listing.get('extraFacts'), dict):
        listing['extraFacts'].pop('cre_public_listing_facts', None)
    raw = listing.get('rawBuildout')
    source = listing.get('sourceKey')
    url = safe_url(listing.get('url'))
    public_url = url if isinstance(url, str) and urlsplit(url).scheme == 'https' else None
    observed = _clock(listing, marker)
    original_id = listing.get('id')
    raw_id, raw_url = None, None
    if isinstance(raw, dict):
        try:
            raw_id = _mapper_id(raw)
        except (ValueError, OverflowError):
            raw_id = object()
        raw_url = _clean(raw.get('show_link'))
    binding = ('id_and_url' if raw_id is not None and raw_url is not None else
               'id_only' if raw_id is not None else 'url_only' if raw_url is not None else 'unbound')
    bound = (marker.get('status') == 'complete' and marker.get('binding') == binding and isinstance(source, str) and source in _registry()
             and marker.get('sourceKey') == source and marker.get('feedKey') in _registry()[source]
             and (source != 'franklin-street' or (listing.get('transactionMode') in ('sale', 'lease')
                  and marker['feedKey'] == 'franklin-street-' + listing['transactionMode']))
             and 'listingId' in marker and 'listingUrl' in marker
             and (raw_id is not None or raw_url is not None)
             and raw_id == listing.get('id') == marker['listingId']
             and raw_url == listing.get('url') == marker['listingUrl']
             and (raw_url is None or raw_url == url or raw_id is not None))
    prior = listing.get('buildoutRawStaging')
    prior_redacted = isinstance(prior, dict) and prior.get('schema') == 'buildout-raw-staging-v1' and prior.get('status') != 'complete'
    try:
        secrets = _document_secrets(raw, listing)
        if raw_url is not None and url is None:
            secrets.extend([raw_url])
        listing, _, changes = _sanitize_encoded_labels(listing, {}, inspect_strings=True, removed_strings=secrets)
        safe_brokers = _unstage_referenced_brokers(listing, brokers, broker_ids)
        safe = listing.get('rawBuildout')
        if safe is not None and len(events.encoded(safe)) > 2_000_000:
            raise _InspectionLimit('Buildout staged raw exceeds bound')
        if (listing.get('sourceKey') != source or (url is not None
                and (listing.get('url') != url or listing.get('id') != original_id))):
            raise ValueError('Buildout identity contains credential material')
    except (_InspectionLimit, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError('Buildout staged listing credential inspection failed') from exc
    listing['buildoutRawStaging'] = {'schema': 'buildout-raw-staging-v1',
                                    'status': 'omitted' if safe is None else
                                    'redacted' if changes or prior_redacted else 'complete'}
    if url is None:
        listing['buildoutRawStaging']['canonicalUnavailable'] = True
    omissions = []
    facts = []
    if not bound or not isinstance(safe, dict):
        omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    else:
        omissions.extend({'sourcePath': None, 'reason': reason} for _, reason in changes)
        if prior_redacted and not changes:
            omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
        try:
            facts = _build_facts(safe, {}, omissions)
        except (_InspectionLimit, ValueError, TypeError, RecursionError, OverflowError):
            omissions = [{'sourcePath': None, 'reason': 'input_limit'}]
    if observed is None or public_url is None:
        omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    if len(omissions) > 20:
        omissions = omissions[:19] + [{'sourcePath': None, 'reason': 'input_limit'}]
    envelope = {'schema': SCHEMA, 'sourceKey': source, 'codec': CODEC, 'observedAt': observed,
                'sourceUrl': public_url, 'facts': facts, 'omissions': omissions,
                'captureCompleteness': 'partial' if omissions else 'complete_for_captured_object',
                'sourceCoverage': 'unverified'}
    if len(events.encoded(envelope)) > 512000:
        envelope['facts'] = []
        envelope['omissions'] = [{'sourcePath': None, 'reason': 'input_limit'}]
        envelope['captureCompleteness'] = 'partial'
    extra = listing.get('extraFacts')
    if extra is not None and not isinstance(extra, dict):
        raise ValueError('Buildout public facts conflict with malformed extraFacts')
    extra = dict(extra or {})
    extra['cre_public_listing_facts'] = envelope
    listing['extraFacts'] = extra
    if brokers is not None:
        return listing, safe_brokers
    return listing


def _observation(envelope, raw):
    """Extract leaf observations from our own bounded merge wrapper."""
    facts = envelope.get('facts')
    if (isinstance(facts, list) and facts and all(
            isinstance(fact, dict) and fact.get('label') == 'Inventory observation'
            and fact.get('sourcePath') is None and isinstance(fact.get('value'), dict)
            and set(fact['value']) == {'feedKey', 'sourceKey', 'sourceUrl', 'observedAt',
                                       'captureCompleteness', 'omissions', 'facts'}
            for fact in facts)):
        return [copy.deepcopy(fact['value']) for fact in facts]
    marker = raw.get('buildoutRawRetention') if isinstance(raw, dict) else None
    feed = marker.get('feedKey') if isinstance(marker, dict) else None
    source = envelope.get('sourceKey')
    if not isinstance(feed, str) or feed not in _registry().get(source, frozenset()):
        feed = None
    return [{'feedKey': feed, 'sourceKey': source, 'sourceUrl': envelope.get('sourceUrl'),
             'observedAt': envelope.get('observedAt'), 'captureCompleteness': envelope.get('captureCompleteness'),
             'omissions': copy.deepcopy(envelope.get('omissions')), 'facts': copy.deepcopy(facts)}]


def merge_public_facts(a, b):
    """Keep separately captured Buildout passes under ordered fact values."""
    first, second = a.get('extra_facts'), b.get('extra_facts')
    left = first.get('cre_public_listing_facts') if isinstance(first, dict) else None
    right = second.get('cre_public_listing_facts') if isinstance(second, dict) else None
    if not (isinstance(left, dict) and isinstance(right, dict)
            and left.get('schema') == right.get('schema') == SCHEMA
            and left.get('codec') == right.get('codec') == CODEC
            and isinstance(left.get('sourceKey'), str)
            and left.get('sourceKey') == right.get('sourceKey')
            and left.get('sourceKey') in _registry()):
        return
    observations = []
    for entry in _observation(left, a.get('raw_data')) + _observation(right, b.get('raw_data')):
        if entry not in observations:
            observations.append(entry)
    if len(observations) == 1:
        return
    urls = {obs.get('sourceUrl') for obs in observations}
    clocks = {obs.get('observedAt') for obs in observations}
    prior_limit = any(isinstance(item, dict) and item.get('reason') == 'input_limit'
                      for envelope in (left, right)
                      for item in (envelope.get('omissions') if isinstance(envelope.get('omissions'), list) else []))
    partial = (len(urls) != 1 or len(clocks) != 1 or prior_limit
               or any(obs.get('captureCompleteness') != 'complete_for_captured_object' for obs in observations))
    omissions = [{'sourcePath': None, 'reason': 'unverified_content_omitted'}] if partial else []
    combined = {'schema': SCHEMA, 'sourceKey': left['sourceKey'], 'codec': CODEC,
                'observedAt': next(iter(clocks)) if len(clocks) == 1 else None,
                'sourceUrl': next(iter(urls)) if len(urls) == 1 else None,
                'facts': [], 'omissions': omissions,
                'captureCompleteness': 'partial' if partial else 'complete_for_captured_object',
                'sourceCoverage': 'unverified'}
    # Retain the largest ordered prefix admitted by the same bounded fact
    # budget used for individual source objects. Never silently discard a pass.
    budget, total_bytes = [1], 0
    omitted = prior_limit
    for obs in observations:
        fact = {'label': 'Inventory observation', 'sourcePath': None, 'value': obs}
        size = len(events.encoded(fact))
        trial = budget.copy()
        if (len(combined['facts']) >= 1000 or not _within(fact, budget=trial)
                or total_bytes + size > 440000):
            omitted = True
            break
        combined['facts'].append(fact)
        budget = trial
        total_bytes += size
    if omitted:
        combined['omissions'].append({'sourcePath': None, 'reason': 'input_limit'})
        combined['captureCompleteness'] = 'partial'
    if len(events.encoded(combined)) > 512000:
        combined['facts'] = []
        combined['omissions'] = [{'sourcePath': None, 'reason': 'input_limit'}]
        combined['captureCompleteness'] = 'partial'
    first['cre_public_listing_facts'] = combined
