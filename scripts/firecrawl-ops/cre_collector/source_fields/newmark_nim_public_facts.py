"""Bounded public facts from bound Newmark NIM inventory observations."""
import copy
import re
import unicodedata
from html import unescape
from urllib.parse import parse_qsl, quote, unquote, urlsplit

from buildout_observation_artifact import SECRET_QUERY, sensitive_string
from source_evidence import events
from source_evidence.projections import newmark_native
from source_fields.public_facts import (
    _build_facts,
    _InspectionLimit,
    _sanitize_encoded_labels,
    _sensitive_encoded_string,
    _stage_referenced_brokers,
    _unstage_referenced_brokers,
)

SCHEMA = 'cre-public-listing-facts/v1'
CODEC = 'newmark-nim-inventory/v1'
SLOTS = {'sale': 'cre_newmark_nim_sale_public_listing_facts',
         'lease': 'cre_newmark_nim_lease_public_listing_facts'}
_SLUG = re.compile(r'^[A-Za-z0-9._~-]+$')


def _clean(value):
    return ' '.join(value.split()) or None if isinstance(value, str) else None


def _identity(raw):
    if not isinstance(raw, dict):
        return None, None
    slug = _clean(raw.get('slug'))
    valid_slug = slug if slug and slug not in ('.', '..') and _SLUG.fullmatch(slug) else None
    provider_url = _clean(raw.get('externalWebsiteUrl'))
    if not provider_url:
        return (valid_slug, 'https://www.nmrk.com/properties/' + quote(valid_slug, safe='._~-')) if valid_slug else (None, None)
    try:
        parsed = urlsplit(provider_url)
        if parsed.scheme.lower() != 'https' or parsed.username or parsed.password or parsed.port not in (None, 443):
            return None, None
        host = (parsed.hostname or '').lower()
        if host in ('my.rcm1.com', 'properties.nmrk.com'):
            return (valid_slug, 'https://www.nmrk.com/properties/' + quote(valid_slug, safe='._~-')) if valid_slug else (None, None)
        if host not in ('www.nmrk.com', 'nmrk.com'):
            return None, None
        match = re.fullmatch(r'/properties/([^/]+)/?', parsed.path)
        if not match:
            return None, None
        segment = match.group(1)
        if re.search(r'%(?![0-9A-Fa-f]{2})', segment):
            return None, None
        identity = unquote(segment, errors='strict')
        if identity in ('.', '..') or not _SLUG.fullmatch(identity):
            return None, None
        return identity, 'https://www.nmrk.com/properties/' + segment
    except (ValueError, UnicodeError):
        return None, None


def _safe_url(value):
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
        if parsed.scheme != 'https' or parsed.netloc != 'www.nmrk.com' or parsed.username or parsed.password:
            return None
    except ValueError:
        return None
    return value


def _clock(listing, marker):
    provenance = listing.get('freshnessProvenance')
    value = listing.get('inventoryObservedAt')
    try:
        if (not isinstance(provenance, dict) or provenance.get('detailScope') != 'authoritative_inventory_feed'
                or provenance.get('method') != 'newmark_nim_public_inventory'
                or provenance.get('cacheDisposition') != 'live'
                or provenance.get('cacheDisposition') != marker.get('cacheDisposition')
                or value != marker.get('inventoryObservedAt')
                or not isinstance(value, str) or not value.endswith('Z')):
            return None
        events.timestamp(value)
        return value
    except ValueError:
        return None


def _document_secrets(raw):
    if not isinstance(raw, dict):
        return []
    # Exact native document-reference rule plus credentials in the mapper's
    # source identity URL; neither source URL is fetched here.
    candidates = []
    document = raw.get('secureDocumentsUrl')
    if newmark_native.withheld_reference(document):
        candidates.append(document)
    identity_url = raw.get('externalWebsiteUrl')
    if isinstance(identity_url, str) and sensitive_string(identity_url):
        candidates.append(identity_url)
    secrets = []
    for value in candidates:
        parsed = urlsplit(value)
        secrets.append(value)
        if parsed.username:
            secrets.append(unquote(parsed.username))
        if parsed.password:
            secrets.append(unquote(parsed.password))
        for key, token in parse_qsl(parsed.query + '&' + parsed.fragment.replace('?', '&'), keep_blank_values=True):
            candidate = key
            for _ in range(5):
                if SECRET_QUERY.fullmatch(candidate) and token:
                    secrets.append(token)
                    break
                following = unquote(candidate, errors='replace')
                if following == candidate:
                    break
                candidate = following
    return secrets


def attach(listing, brokers=None, *, publish=False):
    if not isinstance(listing, dict) or listing.get('sourceKey') != 'newmark':
        return (listing, brokers) if brokers is not None else listing
    marker = listing.get('newmarkNimRawRetention')
    if not isinstance(marker, dict) or marker.get('schema') != 'newmark-nim-raw-retention-v1':
        extra = listing.get('extraFacts')
        if isinstance(extra, dict) and any(name in extra for name in SLOTS.values()):
            listing = copy.deepcopy(listing)
            for name in SLOTS.values():
                listing['extraFacts'].pop(name, None)
        return (listing, brokers) if brokers is not None else listing
    listing = copy.deepcopy(listing)
    broker_ids = _stage_referenced_brokers(listing, brokers)
    tx = listing.get('transactionMode')
    slot = SLOTS.get(tx)
    if slot is None:
        raise ValueError('Newmark NIM transaction mode missing or invalid')
    if isinstance(listing.get('extraFacts'), dict):
        for name in SLOTS.values():
            listing['extraFacts'].pop(name, None)
    raw = listing.get('rawNewmarkNimRecord')
    original_id = listing.get('id')
    source_url = _safe_url(listing.get('url'))
    raw_provenance = listing.get('freshnessProvenance')
    provenance = raw_provenance if isinstance(raw_provenance, dict) else {}
    observed = _clock(listing, marker)
    identity, raw_url = _identity(raw)
    provider_id = _clean(raw.get('id')) if isinstance(raw, dict) else None
    bound = (marker.get('status') == 'complete' and marker.get('sourceKey') == 'newmark'
             and marker.get('transactionMode') == tx and marker.get('binding') == 'id_and_url'
             and all(key in marker for key in ('listingId', 'listingUrl', 'providerId', 'inventoryObservedAt', 'cacheDisposition'))
             and identity is not None and provider_id == marker['providerId']
             and identity == listing.get('id') == marker['listingId']
             and raw_url == listing.get('url') == marker['listingUrl'] == source_url
             and marker['inventoryObservedAt'] == listing.get('inventoryObservedAt')
             and marker['cacheDisposition'] == provenance.get('cacheDisposition')
             and (observed is not None or (marker['inventoryObservedAt'] is None and marker['cacheDisposition'] is None)))
    prior = listing.get('newmarkNimRawStaging')
    prior_redacted = isinstance(prior, dict) and prior.get('schema') == 'newmark-nim-raw-staging-v1' and prior.get('status') != 'complete'
    try:
        secrets = _document_secrets(raw)
        listing, _, changes = _sanitize_encoded_labels(listing, {}, inspect_strings=True, removed_strings=secrets)
        safe_brokers = _unstage_referenced_brokers(listing, brokers, broker_ids)
        safe = listing.get('rawNewmarkNimRecord')
        if safe is not None and len(events.encoded(safe)) > 2_000_000:
            raise _InspectionLimit('Newmark NIM staged raw exceeds bound')
        if (listing.get('sourceKey') != 'newmark' or listing.get('transactionMode') != tx
                or (source_url is not None and (listing.get('id') != original_id or listing.get('url') != source_url))):
            raise ValueError('Newmark NIM identity contains credential material')
        staged_identity, staged_url = _identity(safe)
        staged_provider = _clean(safe.get('id')) if isinstance(safe, dict) else None
        if (listing.get('newmarkNimRawRetention') != marker
                or listing.get('inventoryObservedAt') != marker.get('inventoryObservedAt')
                or listing.get('freshnessProvenance') != raw_provenance
                or staged_identity != identity or staged_url != raw_url or staged_provider != provider_id):
            bound = False
            observed = None
    except (_InspectionLimit, ValueError, TypeError, RecursionError, OverflowError) as exc:
        raise ValueError('Newmark NIM staged listing credential inspection failed') from exc
    listing['newmarkNimRawStaging'] = {'schema': 'newmark-nim-raw-staging-v1',
                                      'status': 'omitted' if safe is None else
                                      'redacted' if changes or prior_redacted else 'complete'}
    if not publish:
        return (listing, safe_brokers) if brokers is not None else listing
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
    if observed is None or source_url is None:
        omissions.append({'sourcePath': None, 'reason': 'unverified_content_omitted'})
    if len(omissions) > 20:
        omissions = omissions[:19] + [{'sourcePath': None, 'reason': 'input_limit'}]
    envelope = {'schema': SCHEMA, 'sourceKey': 'newmark', 'codec': CODEC, 'transactionMode': tx,
                'observedAt': observed, 'sourceUrl': source_url, 'facts': facts, 'omissions': omissions,
                'captureCompleteness': 'partial' if omissions else 'complete_for_captured_object',
                'sourceCoverage': 'unverified'}
    if len(events.encoded(envelope)) > 512000:
        envelope['facts'] = []
        envelope['omissions'] = [{'sourcePath': None, 'reason': 'input_limit'}]
        envelope['captureCompleteness'] = 'partial'
    extra = listing.get('extraFacts')
    if extra is not None and not isinstance(extra, dict):
        raise ValueError('Newmark NIM public facts conflict with malformed extraFacts')
    extra = dict(extra or {})
    extra[slot] = envelope
    listing['extraFacts'] = extra
    return (listing, safe_brokers) if brokers is not None else listing
