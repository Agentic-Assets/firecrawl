"""Bounded projection of retained Kidder inventory; raw custody stays private."""
import copy
import unicodedata
from html import unescape
from urllib.parse import unquote, urlsplit

from source_evidence import events
from source_fields.public_facts import (
    _build_facts,
    _InspectionLimit,
    _merge_public_facts,
    _sanitize_encoded_labels,
    _sensitive_encoded_string,
    _stage_referenced_brokers,
    _unstage_referenced_brokers,
)

SCHEMA = 'cre-public-listing-facts/v1'
CODEC = 'kidder-public-inventory/v1'
def _clean(value):
    return ' '.join(value.split()) or None if isinstance(value, str) else None


def _identity(raw):
    if not isinstance(raw, dict):
        return None, None
    def key(value):
        if isinstance(value, str):
            return _clean(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            if not (-9007199254740991 <= value <= 9007199254740991) or int(value) != value:
                raise ValueError('unverified Kidder mapper identity')
            return str(int(value))
        return None
    identity = key(raw.get('listing_key')) or key(raw.get('property_key'))
    return identity, ('https://www.kidder.com/listings/' + identity if identity is not None
                      else 'https://www.kidder.com/properties/')


def _safe_url(url):
    if not isinstance(url, str) or len(url) > 2048 or _sensitive_encoded_string(url):
        return None
    candidate = url
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
        parsed = urlsplit(url)
        if parsed.scheme != 'https' or parsed.netloc != 'www.kidder.com' or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return url


def _clock(listing, marker):
    provenance = listing.get('freshnessProvenance')
    value = listing.get('inventoryObservedAt')
    try:
        if (not isinstance(provenance, dict)
                or provenance.get('detailScope') != 'authoritative_inventory_feed'
                or provenance.get('method') != 'kidder_public_inventory_feed'
                or provenance.get('cacheDisposition') not in ('live', 'generation_cache', 'source_revision_cache')
                or provenance.get('cacheDisposition') != marker.get('cacheDisposition')
                or value != marker.get('inventoryObservedAt')
                or not isinstance(value, str) or not value.endswith('Z')):
            return None
        events.timestamp(value)
        return value
    except ValueError:
        return None


def attach(listing, brokers=None):
    if not isinstance(listing, dict) or listing.get('sourceKey') != 'kidder-mathews':
        return (listing, brokers) if brokers is not None else listing
    marker = listing.get('kidderRawRetention')
    marked = isinstance(marker, dict) and marker.get('schema') == 'kidder-raw-retention-v1'
    if not marked:
        return (listing, brokers) if brokers is not None else listing
    listing = copy.deepcopy(listing)
    broker_ids = _stage_referenced_brokers(listing, brokers)
    if isinstance(listing.get('extraFacts'), dict):
        listing['extraFacts'].pop('cre_public_listing_facts', None)
    original_id = listing.get('id')
    raw = listing.get('rawKidder')
    url = _safe_url(listing.get('url'))
    provenance = listing.get('freshnessProvenance')
    provenance = provenance if isinstance(provenance, dict) else {}
    observed = _clock(listing, marker)
    try:
        identity, raw_url = _identity(raw)
    except (ValueError, OverflowError):
        identity, raw_url = None, None
    bound = (marked and marker.get('status') == 'complete'
             and all(key in marker for key in ('listingId', 'listingUrl', 'inventoryObservedAt', 'cacheDisposition'))
             and marker.get('sourceKey') == 'kidder-mathews'
             and marker.get('binding') == 'id_and_url'
             and identity is not None
             and marker.get('inventoryObservedAt') == listing.get('inventoryObservedAt')
             and (observed is not None or (marker['inventoryObservedAt'] is None and marker['cacheDisposition'] is None))
             and provenance.get('cacheDisposition') == marker.get('cacheDisposition')
             and identity == listing.get('id') == marker['listingId']
             and raw_url == listing.get('url') == marker['listingUrl']
             and raw_url == url)
    # Inspect the whole staged listing, so copies in normalized description,
    # contacts or other siblings cannot bypass redaction of the source value.
    # The private input artifact remains unchanged.
    prior = listing.get('kidderRawStaging')
    prior_redacted = isinstance(prior, dict) and prior.get('schema') == 'kidder-raw-staging-v1' and prior.get('status') != 'complete'
    try:
        wrapper = {'payload': listing}
        if raw_url is not None and url is None:
            wrapper['apiKey'] = [raw_url, raw.get('permalink')]
        inspected, _, changes = _sanitize_encoded_labels(
            wrapper, {}, inspect_strings=True,
        )
        listing = inspected['payload']
        safe_brokers = _unstage_referenced_brokers(listing, brokers, broker_ids)
        safe = listing.get('rawKidder')
        if safe is not None and len(events.encoded(safe)) > 2_000_000:
            raise _InspectionLimit('Kidder staged raw exceeds bound')
        if (listing.get('sourceKey') != 'kidder-mathews' or (url is not None
                and (listing.get('url') != url or listing.get('id') != original_id))):
            raise ValueError('Kidder identity contains credential material')
    except (_InspectionLimit, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError('Kidder staged listing credential inspection failed') from exc
    listing['kidderRawStaging'] = {'schema': 'kidder-raw-staging-v1',
                                'status': 'omitted' if safe is None else
                                'redacted' if changes or prior_redacted else 'complete'}
    omissions = []
    facts = []
    if not bound or safe is None:
        omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    else:
        omissions.extend({'sourcePath': None, 'reason': reason} for _, reason in changes)
        if prior_redacted and not changes:
            omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
        try:
            facts = _build_facts(safe, {}, omissions)
        except (_InspectionLimit, ValueError, TypeError, RecursionError, OverflowError):
            facts = []
            omissions = [{'sourcePath': None, 'reason': 'input_limit'}]
    if url is None:
        listing['kidderRawStaging']['canonicalUnavailable'] = True
    if observed is None:
        omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    if url is None:
        reason = 'unverified_content_omitted' if listing.get('url') is None else 'credential_reference_omitted'
        omissions.append({'sourcePath': None, 'reason': reason})
    if len(omissions) > 20:
        omissions = omissions[:19] + [{'sourcePath': None, 'reason': 'input_limit'}]
    envelope = {'schema': SCHEMA, 'sourceKey': 'kidder-mathews', 'codec': CODEC,
                'observedAt': observed, 'sourceUrl': url, 'facts': facts,
                'omissions': omissions,
                'captureCompleteness': 'partial' if omissions else 'complete_for_captured_object',
                'sourceCoverage': 'unverified'}
    if len(events.encoded(envelope)) > 512000:
        envelope['facts'] = []
        envelope['omissions'] = [{'sourcePath': None, 'reason': 'input_limit'}]
        envelope['captureCompleteness'] = 'partial'
    extra = listing.get('extraFacts')
    if extra is not None and not isinstance(extra, dict):
        raise ValueError('Kidder public facts conflict with malformed extraFacts')
    extra = dict(extra or {})
    extra['cre_public_listing_facts'] = envelope
    listing['extraFacts'] = extra
    return (listing, safe_brokers) if brokers is not None else listing


def merge_public_facts(a, b):
    _merge_public_facts(a, b, schema=SCHEMA, codec=CODEC,
                        source_allowed=lambda source: source == 'kidder-mathews',
                        feed_resolver=lambda raw, source: None)
