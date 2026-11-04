"""Bounded projection of retained SRS inventory; raw custody stays private."""
import copy
import unicodedata
from html import unescape
from urllib.parse import unquote, urlsplit

from source_evidence import events
from source_fields.public_facts import (
    _build_facts,
    _InspectionLimit,
    _sanitize_encoded_labels,
    _sensitive_encoded_string,
    _stage_referenced_brokers,
    _unstage_referenced_brokers,
)

SCHEMA = 'cre-public-listing-facts/v1'
CODEC = 'srs-public-inventory/v1'
_PROFESSIONAL = 'Professional_Credentials_Certification__c'


def _allowed_paths(raw):
    data = raw.get('apto_data')
    if not isinstance(data, dict):
        return set()
    paths = set()
    if isinstance(data.get('marketing_approver'), dict):
        paths.add('/apto_data/marketing_approver/' + _PROFESSIONAL)
    brokers = data.get('related_brokers')
    if isinstance(brokers, list):
        paths.update('/apto_data/related_brokers/' + str(i) + '/' + _PROFESSIONAL
                     for i, broker in enumerate(brokers) if isinstance(broker, dict))
    return paths



def _clean(value):
    return ' '.join(value.split()) or None if isinstance(value, str) else None


def _identity(raw):
    if not isinstance(raw, dict):
        return None, None
    data = raw.get('apto_data')
    if data is None:
        data = {}
    if not isinstance(data, dict):
        return None, None
    # Only safe integral JS numbers have an unambiguous cross-runtime string.
    provider = data.get('SRS_Listings_ID__c')
    if isinstance(provider, (int, float)) and not isinstance(provider, bool):
        if not (-9007199254740991 <= provider <= 9007199254740991) or int(provider) != provider:
            return None, None
        identity = str(int(provider))
    else:
        identity = _clean(provider) or _clean(raw.get('id')) or _clean(data.get('Id'))
    permalink = raw.get('permalink')
    url = 'https://www.srsre.com' + permalink if isinstance(permalink, str) and permalink else None
    return identity, url


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
        if parsed.scheme != 'https' or parsed.netloc != 'www.srsre.com' or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return url


def _clock(listing):
    provenance = listing.get('freshnessProvenance')
    value = listing.get('inventoryObservedAt')
    try:
        if (not isinstance(provenance, dict)
                or provenance.get('detailScope') != 'authoritative_inventory_feed'
                or provenance.get('method') != 'srs_cloud_run_inventory_feed'
                or not isinstance(value, str) or not value.endswith('Z')):
            return None
        events.timestamp(value)
        return value
    except ValueError:
        return None


def attach(listing, brokers=None):
    if not isinstance(listing, dict) or listing.get('sourceKey') != 'srs':
        return (listing, brokers) if brokers is not None else listing
    marker = listing.get('srsRawRetention')
    marked = isinstance(marker, dict) and marker.get('schema') == 'srs-raw-retention-v1'
    if not marked:
        return (listing, brokers) if brokers is not None else listing
    listing = copy.deepcopy(listing)
    broker_ids = _stage_referenced_brokers(listing, brokers)
    if isinstance(listing.get('extraFacts'), dict):
        listing['extraFacts'].pop('cre_public_listing_facts', None)
    original_id = listing.get('id')
    raw = listing.get('rawSrs')
    url = _safe_url(listing.get('url'))
    observed = _clock(listing)
    identity, raw_url = _identity(raw)
    bound = (marked and marker.get('status') == 'complete'
             and 'listingId' in marker and 'listingUrl' in marker
             and (identity is not None or raw_url is not None)
             and identity == listing.get('id') == marker['listingId']
             and raw_url == listing.get('url') == marker['listingUrl']
             and (raw_url is None or raw_url == url or identity is not None))
    # Inspect the whole staged listing, so copies in normalized description,
    # contacts or other siblings cannot bypass redaction of the source value.
    # The private input artifact remains unchanged.
    allowed = _allowed_paths(raw) if isinstance(raw, dict) else set()
    prior = listing.get('srsRawStaging')
    prior_redacted = isinstance(prior, dict) and prior.get('schema') == 'srs-raw-staging-v1' and prior.get('status') != 'complete'
    try:
        wrapper = {'payload': listing}
        if raw_url is not None and url is None:
            wrapper['apiKey'] = [raw_url, raw.get('permalink')]
        inspected, _, changes = _sanitize_encoded_labels(
            wrapper, {}, allow_label=lambda path, key: key == _PROFESSIONAL
            and path.startswith('/payload/rawSrs')
            and path[len('/payload/rawSrs'):] in allowed,
            inspect_strings=True,
        )
        listing = inspected['payload']
        safe_brokers = _unstage_referenced_brokers(listing, brokers, broker_ids)
        safe = listing.get('rawSrs')
        if safe is not None and len(events.encoded(safe)) > 2_000_000:
            raise _InspectionLimit('SRS staged raw exceeds bound')
        if (listing.get('sourceKey') != 'srs' or (url is not None
                and (listing.get('url') != url or listing.get('id') != original_id))):
            raise ValueError('SRS identity contains credential material')
    except (_InspectionLimit, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError('SRS staged listing credential inspection failed') from exc
    listing['srsRawStaging'] = {'schema': 'srs-raw-staging-v1',
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
        listing['srsRawStaging']['canonicalUnavailable'] = True
    if observed is None:
        omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    if url is None:
        reason = 'unverified_content_omitted' if listing.get('url') is None else 'credential_reference_omitted'
        omissions.append({'sourcePath': None, 'reason': reason})
    if len(omissions) > 20:
        omissions = omissions[:19] + [{'sourcePath': None, 'reason': 'input_limit'}]
    envelope = {'schema': SCHEMA, 'sourceKey': 'srs', 'codec': CODEC,
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
        raise ValueError('SRS public facts conflict with malformed extraFacts')
    extra = dict(extra or {})
    extra['cre_public_listing_facts'] = envelope
    listing['extraFacts'] = extra
    return (listing, safe_brokers) if brokers is not None else listing
