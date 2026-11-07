"""Transactional best-effort private listing history for canonical ingest.

Reports omissions after commit; does not activate the source-evidence writer.
The mapped staged observation is not original provider wire data.
"""

import json
import re

SETUP = r"""
CREATE TEMP TABLE _listing_history_outcome (slug text, artifact_run_key text, phase text, outcome text) ON COMMIT PRESERVE ROWS;
CREATE TEMP TABLE _listing_history_redactions
  (capture_id uuid, path_sha256 text, reason text) ON COMMIT DROP;
CREATE OR REPLACE FUNCTION pg_temp.sanitize_listing_history(
  p_value jsonb, p_path text, p_capture uuid
) RETURNS jsonb LANGUAGE plpgsql AS $$
DECLARE
  v_kind text := jsonb_typeof(p_value);
  v_key text;
  v_normalized text;
  v_child jsonb;
  v_result jsonb;
  v_output_key text;
  v_text text;
  v_scan text;
  v_index bigint;
  v_sensitive boolean;
BEGIN
  IF v_kind = 'object' THEN
    v_result := '{}'::jsonb;
    FOR v_key, v_child IN SELECT key, value FROM jsonb_each(p_value) LOOP
      v_normalized := lower(regexp_replace(v_key, '([a-z0-9])([A-Z])', '\1_\2', 'g'));
      v_sensitive := v_normalized ~ '(^|[^a-z0-9])(authorizations?|passwords?|passwds?|secrets?|credentials?|cookies?|api[ _-]?keys?|access[ _-]?tokens?|refresh[ _-]?tokens?|security[ _-]?tokens?|signatures?|private[ _-]?keys?|session[ _-]?(tokens?|ids?|keys?))($|[^a-z0-9])'
        OR v_normalized ~ '^(tokens?|sessions?)([ _-]?(value|id|key))?$'
        OR v_normalized IN ('auth','bearer','sig','key')
        OR v_key ~* 'https?://[^/[:space:]"<>]+@'
        OR v_key ~* '[?&#](token|access_token|api_key|apikey|authorization|auth|signature|sig|password|secret|session|access_key|access_key_id|awsaccesskeyid|googleaccessid|x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential|x-goog-security-token|client_secret|key)=';
      IF v_sensitive THEN
        v_output_key := '__withheld_key_sha256_' || encode(sha256(convert_to(v_key,'UTF8')),'hex');
        IF p_value ? v_output_key OR v_result ? v_output_key THEN RAISE EXCEPTION 'history redaction key collision'; END IF;
        INSERT INTO _listing_history_redactions VALUES
          (p_capture, encode(sha256(convert_to(p_path || '/' || replace(replace(v_key,'~','~0'),'/','~1'),'UTF8')),'hex'), 'credential_key');
        v_result := v_result || jsonb_build_object(v_output_key, NULL);
      ELSE
        v_result := v_result || jsonb_build_object(v_key,
          pg_temp.sanitize_listing_history(v_child, p_path || '/' || replace(replace(v_key,'~','~0'),'/','~1'), p_capture));
      END IF;
    END LOOP;
    RETURN v_result;
  ELSIF v_kind = 'array' THEN
    v_result := '[]'::jsonb;
    FOR v_child, v_index IN SELECT value, ordinality FROM jsonb_array_elements(p_value) WITH ORDINALITY LOOP
      v_result := v_result || jsonb_build_array(pg_temp.sanitize_listing_history(v_child, p_path || '/' || (v_index-1)::text, p_capture));
    END LOOP;
    RETURN v_result;
  ELSIF v_kind = 'string' THEN
    v_text := p_value #>> '{}';
    v_scan := replace(v_text, chr(92), '');
    IF position(chr(92) || 'u' in lower(v_text)) > 0
       OR v_scan ~* '"[^"]*(password|passwd|secret|authorization|credential|cookie|private[_-]?key|session[_-]?(id|key|token)|access[_-]?token|refresh[_-]?token|security[_-]?token|api[_-]?key|signature)[^"]*"[[:space:]]*:'
       OR v_scan ~* '"(token|session|auth|sig|key)"[[:space:]]*:'
       OR v_scan ~* 'https?://[^/[:space:]"<>]+@'
       OR v_scan ~* 'https?://[^[:space:]"<>]*[?#][^[:space:]"<>]*%'
       OR v_scan ~* '[?&#](token|access_token|api_key|apikey|authorization|auth|signature|sig|password|secret|session|access_key|access_key_id|awsaccesskeyid|googleaccessid|key|x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential|x-goog-security-token|client_secret)='
       OR v_scan ~* '(authorization|proxy-authorization)[[:space:]]*:[[:space:]]*(bearer|basic)[[:space:]]'
       OR v_scan ~* '(^|[^a-z])(set-)?cookie[[:space:]]*:' THEN
      INSERT INTO _listing_history_redactions VALUES
        (p_capture, encode(sha256(convert_to(p_path,'UTF8')),'hex'), 'credential_or_uninspectable_text');
      RETURN 'null'::jsonb;
    END IF;
  END IF;
  RETURN p_value;
END $$;
CREATE OR REPLACE FUNCTION pg_temp.capture_listing_history(
  p_brokerage uuid, p_external text, p_run text, p_phase text, p_staged jsonb,
  p_wait_for_child boolean
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
  v_listing uuid;
  v_parent jsonb;
  v_payload jsonb;
  v_children jsonb := '{}'::jsonb;
  v_table text;
  v_values jsonb;
  v_count integer;
  v_capture uuid := gen_random_uuid();
  v_omissions jsonb;
  v_lock_timeout text;
  v_wait boolean;
BEGIN
  IF to_regclass('credeals.cre_listing_snapshots') IS NULL THEN
    RETURN 'store_unavailable';
  END IF;
  -- Identity is also stored outside the payload; refuse it when it contains a
  -- known credential-bearing form rather than leaking it through metadata.
  IF pg_temp.sanitize_listing_history(to_jsonb(p_external), '/external_id', v_capture) = 'null'::jsonb THEN
    DELETE FROM _listing_history_redactions WHERE capture_id=v_capture;
    RETURN 'credential_identity_refused';
  END IF;
  IF p_phase = 'observation' THEN
    SELECT id INTO v_listing FROM credeals.cre_listings
      WHERE brokerage_id=p_brokerage AND external_id=p_external;
    v_payload := p_staged;
  ELSE
    SELECT id, to_jsonb(l) INTO v_listing, v_parent FROM credeals.cre_listings l
      WHERE brokerage_id=p_brokerage AND external_id=p_external;
    IF NOT FOUND THEN RETURN 'no_prior'; END IF;
    FOREACH v_table IN ARRAY ARRAY['cre_listing_contacts', 'cre_listing_documents',
      'cre_listing_images', 'cre_listing_media', 'cre_listing_links'] LOOP
      IF to_regclass('credeals.' || v_table) IS NULL THEN RETURN 'child_schema_unavailable'; END IF;
    END LOOP;
    -- Parent row is already locked by ingest. Lock existing children in a
    -- fixed order so independent UPDATE/DELETE cannot pass the snapshot.
    -- Known replacement paths use the transaction's existing lock timeout,
    -- because their canonical DELETE needs the same locks. Preserving or
    -- uncertain paths bound this extra wait and report an omission.
    v_lock_timeout := current_setting('lock_timeout');
    FOREACH v_table IN ARRAY ARRAY['cre_listing_contacts', 'cre_listing_documents',
      'cre_listing_images', 'cre_listing_media', 'cre_listing_links'] LOOP
      v_wait := p_wait_for_child;
      IF v_table = 'cre_listing_contacts' THEN
        -- Empty Buildout contact sets and some Colliers contacts are retained;
        -- do not extend their existing canonical write wait solely for history.
        v_wait := COALESCE(p_wait_for_child AND p_staged->>'slug' <> 'colliers'
          AND CASE WHEN jsonb_typeof(p_staged->'contacts')='array'
                   THEN jsonb_array_length(p_staged->'contacts') > 0 ELSE false END
          AND NOT jsonb_path_exists(p_staged->'raw_data', '$.**.preserveContactCollections ? (@ == true || @ == "true")'), false);
      END IF;
      IF NOT v_wait THEN
        PERFORM set_config('lock_timeout', '250ms', true);
      END IF;
      EXECUTE format('SELECT c.id FROM credeals.%I c WHERE c.listing_id=$1 ORDER BY c.id FOR UPDATE', v_table)
        USING v_listing;
      IF NOT v_wait THEN
        PERFORM set_config('lock_timeout', v_lock_timeout, true);
      END IF;
    END LOOP;
    FOREACH v_table IN ARRAY ARRAY['cre_listing_contacts', 'cre_listing_documents',
      'cre_listing_images', 'cre_listing_media', 'cre_listing_links'] LOOP
      EXECUTE format('SELECT COALESCE(jsonb_agg(to_jsonb(c) ORDER BY c.id), ''[]''::jsonb) FROM credeals.%I c WHERE c.listing_id=$1', v_table)
        INTO v_values USING v_listing;
      v_children := v_children || jsonb_build_object(v_table, v_values);
    END LOOP;
    v_payload := jsonb_build_object('parent', v_parent, 'children', v_children);
  END IF;
  v_payload := pg_temp.sanitize_listing_history(v_payload, '', v_capture);
  SELECT COALESCE(jsonb_agg(jsonb_build_object('path_sha256',path_sha256,'reason',reason)
                            ORDER BY path_sha256,reason),'[]'::jsonb)
    INTO v_omissions FROM _listing_history_redactions WHERE capture_id=v_capture;
  DELETE FROM _listing_history_redactions WHERE capture_id=v_capture;
  EXECUTE 'INSERT INTO credeals.cre_listing_snapshots
    (brokerage_id, external_id, listing_id, artifact_run_key, phase, payload_sha256, payload, omissions)
    VALUES ($1,$2,$3,$4,$5,encode(sha256(convert_to(jsonb_build_object(''payload'',$6,''omissions'',$7)::text,''UTF8'')),''hex''),$6,$7)
    ON CONFLICT (brokerage_id,external_id,artifact_run_key,phase,payload_sha256) DO NOTHING'
    USING p_brokerage, p_external, v_listing, p_run, p_phase, v_payload, v_omissions;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  IF jsonb_array_length(v_omissions) > 0 THEN
    RETURN CASE WHEN v_count = 1 THEN 'captured_with_omissions' ELSE 'duplicate_with_omissions' END;
  END IF;
  RETURN CASE WHEN v_count = 1 THEN 'captured' ELSE 'duplicate' END;
EXCEPTION WHEN lock_not_available THEN
  RETURN 'child_lock_unavailable';
WHEN OTHERS THEN
  -- Never expose source data or credentials in an error message.
  RETURN 'capture_error';
END $$;
"""

BEFORE = r"""
INSERT INTO _listing_history_outcome
SELECT s.slug, jm.artifact_run_key, 'preimage', pg_temp.capture_listing_history(s.brokerage_id,s.external_id,jm.artifact_run_key,'preimage',to_jsonb(s),
  COALESCE((s.slug <> 'avison-young'
   AND NOT jsonb_path_exists(s.raw_data, '$.**.detailError')
   AND NOT jsonb_path_exists(s.raw_data, '$.**.preserveChildCollections ? (@ == true || @ == "true")')
   AND NOT jsonb_path_exists(s.raw_data, '$.**.jllPriceWithheld ? (@ == true)')),false))
FROM _src s JOIN _jobmeta jm ON jm.slug=s.slug;
INSERT INTO _listing_history_outcome
SELECT s.slug, jm.artifact_run_key, 'observation', pg_temp.capture_listing_history(s.brokerage_id,s.external_id,jm.artifact_run_key,'observation',to_jsonb(s),false)
FROM _src s JOIN _jobmeta jm ON jm.slug=s.slug;
"""

AFTER = r"""
INSERT INTO _listing_history_outcome
SELECT s.slug, jm.artifact_run_key, 'postimage', pg_temp.capture_listing_history(u.brokerage_id,u.external_id,jm.artifact_run_key,'postimage',NULL,false)
FROM _up u JOIN _src s USING (brokerage_id,external_id) JOIN _jobmeta jm ON jm.slug=s.slug;
"""

RETIRED_BEFORE = r"""
INSERT INTO _listing_history_outcome
SELECT b.slug, jm.artifact_run_key, 'retired_preimage', pg_temp.capture_listing_history(r.brokerage_id,r.external_id,jm.artifact_run_key,'preimage',NULL,false)
FROM _retired r JOIN credeals.cre_brokerages b ON b.id=r.brokerage_id JOIN _jobmeta jm ON jm.slug=b.slug;
"""

RETIRED_AFTER = r"""
INSERT INTO _listing_history_outcome
SELECT b.slug, jm.artifact_run_key, 'retired_postimage', pg_temp.capture_listing_history(r.brokerage_id,r.external_id,jm.artifact_run_key,'postimage',NULL,false)
FROM _retired r JOIN credeals.cre_brokerages b ON b.id=r.brokerage_id JOIN _jobmeta jm ON jm.slug=b.slug;
"""
RECEIPT = r"""
\pset format unaligned
\pset tuples_only on
SELECT 'CRE_LISTING_HISTORY ' || jsonb_build_object(
  'schema','cre-listing-history-outcome/v1',
  'artifact_run_key',(SELECT min(artifact_run_key) FROM _listing_history_outcome),
  'outcomes',COALESCE(jsonb_object_agg(slug || ':' || phase || ':' || outcome,n),'{}'::jsonb)
)::text FROM (SELECT slug,phase,outcome,count(*) n FROM _listing_history_outcome GROUP BY slug,phase,outcome) x;
\pset format aligned
\pset tuples_only off
"""

def read_receipt(path, expected_run_key):
    """Parse one bounded post-commit report without blocking canonical progress."""
    prefix = b"CRE_LISTING_HISTORY "
    try:
        found = []
        with open(path, "rb") as stream:
            total = 0
            while True:
                line = stream.readline(65537)
                if not line:
                    break
                total += len(line)
                if total > 16 * 1024 * 1024 or len(line) > 65536:
                    return {"state": "unknown", "reason": "log_bound"}
                if line.startswith(prefix):
                    found.append(json.loads(line[len(prefix):]))
                    if len(found) > 1:
                        return {"state": "unknown", "reason": "duplicate_receipt"}
        if len(found) != 1:
            return {"state": "unknown", "reason": "missing_receipt"}
        result = found[0]
        if not isinstance(expected_run_key, str) or not expected_run_key:
            return {"state": "unknown", "reason": "unbound_artifact"}
        if (not isinstance(result, dict) or set(result) != {"schema", "artifact_run_key", "outcomes"}
                or result["schema"] != "cre-listing-history-outcome/v1"
                or result["artifact_run_key"] != expected_run_key
                or not isinstance(result["outcomes"], dict) or len(result["outcomes"]) > 1024):
            return {"state": "unknown", "reason": "invalid_receipt"}
        if not result["outcomes"]:
            return {"state": "unknown", "reason": "no_listing_rows"}
        allowed = {"captured", "duplicate", "no_prior", "store_unavailable",
                   "child_schema_unavailable", "credential_like_content", "capture_error",
                   "captured_with_omissions", "duplicate_with_omissions", "child_lock_unavailable",
                   "credential_identity_refused"}
        for key, count in result["outcomes"].items():
            if (not isinstance(key, str) or not re.fullmatch(r"[a-z0-9-]+:(?:preimage|observation|postimage|retired_preimage|retired_postimage):[a-z_]+", key)
                    or key.rsplit(":", 1)[-1] not in allowed
                    or type(count) is not int or count < 0):
                return {"state": "unknown", "reason": "invalid_receipt"}
        result["state"] = ("reported_omissions" if any(
            key.rsplit(":", 1)[-1] not in {"captured", "duplicate", "no_prior"}
            for key in result["outcomes"]) else "reported_no_omissions")
        return result
    except (OSError, ValueError, TypeError, UnicodeError):
        return {"state": "unknown", "reason": "unreadable_receipt"}