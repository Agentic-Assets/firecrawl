"""Opt-in, bounded source-published link witness from a matching listing row.

This is an unverified local database assertion, not authority to fetch or to
change a canonical identity. Unsupported source shapes are left unresolved.
"""
import json
import re
from datetime import datetime

MAX_WITNESS_BYTES = 4096


FIELDS = ('address', 'city', 'stateAbbreviation', 'stateDescription', 'zip', 'countryCode')


def _field_sql(obj, key):
    return f"""CASE WHEN NOT ({obj} ? '{key}') THEN jsonb_build_object('state','missing')
          WHEN {obj}->'{key}' = 'null'::jsonb THEN jsonb_build_object('state','null')
          WHEN jsonb_typeof({obj}->'{key}') = 'string' AND octet_length({obj}->>'{key}') <= 256
               AND ({obj}->>'{key}') !~ '[[:cntrl:]]'
          THEN jsonb_build_object('state','value','value',{obj}->'{key}')
          ELSE jsonb_build_object('state','unresolved') END"""


def returning_sql():
    """A correlated, single-row SQL expression; never emit the full raw record."""
    field_parts = ", ".join(f"'{key}', " + _field_sql("c.record->'properties'->0", key) for key in FIELDS)
    name = _field_sql("c.record", "name")
    expression = """(SELECT CASE WHEN c.matches <> 1 THEN NULL
             WHEN jsonb_typeof(c.record) IS DISTINCT FROM 'object'
               OR octet_length(c.record->>'id') > 256
               OR ((c.record->>'id') ~ '^[A-Za-z0-9._~-]+$') IS NOT TRUE
               OR jsonb_typeof(c.record->'id') IS DISTINCT FROM 'string'
               OR octet_length(c.record->>'slug') > 256
               OR ((c.record->>'slug') ~ '^[A-Za-z0-9._~-]+$') IS NOT TRUE
               OR octet_length(c.record->>'externalWebsiteUrl') > 560
               OR ((c.record->>'externalWebsiteUrl') ~ '^https://my[.]rcm1[.]com/handler/modern[.]aspx[?]pv=[A-Za-z0-9_-]+$') IS NOT TRUE
               OR (c.observed_at IS NOT NULL AND (c.observed_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?Z$') IS NOT TRUE)
             THEN jsonb_build_object('status','unresolved','reason','unsupported_source_fields')
             ELSE jsonb_build_object('status','observed', 'schema','newmark-nim-link-witness/v1',
                  'sourceKey','newmark','sourcePath','raw_data.rawNewmarkNimRecord',
                  'recordId',c.record->'id','slug',c.record->'slug',
                  'url',c.record->'externalWebsiteUrl',
                  'sourceName', SOURCE_NAME,
                  'sourceProperty', CASE WHEN jsonb_typeof(c.record->'properties') = 'array'
                       THEN CASE WHEN jsonb_array_length(c.record->'properties') = 1
                                      AND jsonb_typeof(c.record->'properties'->0) = 'object'
                            THEN jsonb_build_object('selection','single','fields', jsonb_build_object(SOURCE_FIELDS))
                            ELSE jsonb_build_object('selection','unresolved','fields','{}'::jsonb) END
                       ELSE jsonb_build_object('selection','unresolved','fields','{}'::jsonb) END,
                  'inventoryObservedAt',CASE WHEN c.observed_at ~ '^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}([.][0-9]{1,6})?Z$'
                                             THEN to_jsonb(c.observed_at) ELSE 'null'::jsonb END)
             END
       FROM (SELECT l.raw_data->'rawNewmarkNimRecord' AS record,
                    l.raw_data->>'inventoryObservedAt' AS observed_at, count(*) OVER () AS matches
             FROM credeals.cre_listings l
             WHERE l.brokerage_id=q.brokerage_id AND l.external_id=q.external_id AND l.source_url=q.url
             LIMIT 1) c)::text"""
    return expression.replace("SOURCE_NAME", name).replace("SOURCE_FIELDS", field_parts)


def parse(value):
    if not value:
        return None
    if not isinstance(value, str) or len(value.encode('utf8')) > MAX_WITNESS_BYTES:
        return {'status': 'unresolved', 'reason': 'invalid_carrier'}
    try:
        parsed = json.loads(value)
    except (ValueError, RecursionError):
        return {'status': 'unresolved', 'reason': 'invalid_carrier'}
    invalid = {'status': 'unresolved', 'reason': 'invalid_carrier'}
    if parsed == {'status': 'unresolved', 'reason': 'unsupported_source_fields'}:
        return parsed
    if not isinstance(parsed, dict) or set(parsed) != {'status', 'schema', 'sourceKey', 'sourcePath',
                                                       'recordId', 'slug', 'url', 'inventoryObservedAt', 'sourceName', 'sourceProperty'}:
        return invalid
    record_id, slug, url, clock = (parsed[k] for k in ('recordId', 'slug', 'url', 'inventoryObservedAt'))
    if (parsed['status'] != 'observed' or parsed['schema'] != 'newmark-nim-link-witness/v1'
            or parsed['sourceKey'] != 'newmark' or parsed['sourcePath'] != 'raw_data.rawNewmarkNimRecord'
            or not isinstance(record_id, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{1,256}', record_id)
            or not isinstance(slug, str) or not re.fullmatch(r'[A-Za-z0-9._~-]{1,256}', slug)
            or not isinstance(url, str) or not re.fullmatch(r'https://my\.rcm1\.com/handler/modern\.aspx\?pv=[A-Za-z0-9_-]{1,512}', url)
            or (clock is not None and (not isinstance(clock, str) or not re.fullmatch(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}(?:\.[0-9]{1,6})?Z', clock)))):
        return invalid
    if clock is not None:
        try:
            datetime.fromisoformat(clock.replace('Z', '+00:00'))
        except ValueError:
            return invalid
    def valid_field(field):
        if not isinstance(field, dict) or field.get('state') not in ('missing', 'null', 'value', 'unresolved'):
            return False
        if field['state'] != 'value':
            return set(field) == {'state'}
        value = field.get('value')
        return (set(field) == {'state', 'value'} and isinstance(value, str) and len(value.encode('utf8')) <= 256
                and not any(ord(ch) < 32 or 127 <= ord(ch) <= 159 for ch in value))
    prop = parsed['sourceProperty']
    if (not valid_field(parsed['sourceName']) or not isinstance(prop, dict) or set(prop) != {'selection', 'fields'}
            or prop['selection'] not in ('single', 'unresolved') or not isinstance(prop['fields'], dict)
            or (prop['selection'] == 'single' and (set(prop['fields']) != set(FIELDS)
                or not all(valid_field(value) for value in prop['fields'].values())))
            or (prop['selection'] == 'unresolved' and prop['fields'])):
        return invalid
    return parsed
