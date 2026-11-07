"""Transactional best-effort private listing history for canonical ingest.

Reports omissions after commit; does not activate the source-evidence writer.
The mapped staged observation is not original provider wire data.
"""

import json
import re

SETUP = r"""
CREATE TEMP TABLE _listing_history_outcome (slug text, phase text, outcome text) ON COMMIT PRESERVE ROWS;
CREATE OR REPLACE FUNCTION pg_temp.capture_listing_history(
  p_brokerage uuid, p_external text, p_run text, p_phase text, p_staged jsonb
) RETURNS text LANGUAGE plpgsql AS $$
DECLARE
  v_listing uuid;
  v_parent jsonb;
  v_payload jsonb;
  v_children jsonb := '{}'::jsonb;
  v_table text;
  v_values jsonb;
  v_count integer;
  v_text text;
  v_scan text;
BEGIN
  IF to_regclass('credeals.cre_listing_snapshots') IS NULL THEN
    RETURN 'store_unavailable';
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
      EXECUTE format('SELECT COALESCE(jsonb_agg(to_jsonb(c) ORDER BY c.id), ''[]''::jsonb) FROM credeals.%I c WHERE c.listing_id=$1', v_table)
        INTO v_values USING v_listing;
      v_children := v_children || jsonb_build_object(v_table, v_values);
    END LOOP;
    v_payload := jsonb_build_object('parent', v_parent, 'children', v_children);
  END IF;
  v_text := v_payload::text;

  -- Inspect JSON text and escaped embedded JSON conservatively. Unknown
  -- percent-encoded URL queries and unicode escapes are held, not decoded or
  -- silently passed. This does not claim universal credential detection.
  v_scan := replace(v_text, chr(92), '');
  IF position(chr(92) || 'u' in lower(v_text)) > 0
     OR v_scan ~* '"[^"]*(password|secret|authorization|credential|access[_-]?token|refresh[_-]?token|security[_-]?token|api[_-]?key|signature)[^"]*"[[:space:]]*:'
     OR v_scan ~* '"(token|auth|sig|key)"[[:space:]]*:'
     OR v_scan ~* 'https?://[^/[:space:]"<>]+@'
     OR v_scan ~* 'https?://[^[:space:]"<>]*[?#][^[:space:]"<>]*%'
     OR v_scan ~* '[?&#](token|access_token|api_key|apikey|authorization|auth|signature|sig|key|x-amz-signature|x-amz-credential|x-amz-security-token|x-goog-signature|x-goog-credential|x-goog-security-token|client_secret)='
     OR v_scan ~* '(authorization|proxy-authorization)[[:space:]]*:[[:space:]]*(bearer|basic)[[:space:]]' THEN
    RETURN 'credential_like_content';
  END IF;
  EXECUTE 'INSERT INTO credeals.cre_listing_snapshots
    (brokerage_id, external_id, listing_id, artifact_run_key, phase, payload_sha256, payload)
    VALUES ($1,$2,$3,$4,$5,encode(sha256(convert_to($6::text,''UTF8'')),''hex''),$6)
    ON CONFLICT (brokerage_id,external_id,artifact_run_key,phase,payload_sha256) DO NOTHING'
    USING p_brokerage, p_external, v_listing, p_run, p_phase, v_payload;
  GET DIAGNOSTICS v_count = ROW_COUNT;
  RETURN CASE WHEN v_count = 1 THEN 'captured' ELSE 'duplicate' END;
EXCEPTION WHEN OTHERS THEN
  -- Never expose source data or credentials in an error message.
  RETURN 'capture_error';
END $$;
"""

BEFORE = r"""
INSERT INTO _listing_history_outcome
SELECT s.slug, 'preimage', pg_temp.capture_listing_history(s.brokerage_id,s.external_id,jm.artifact_run_key,'preimage',NULL)
FROM _src s JOIN _jobmeta jm ON jm.slug=s.slug;
INSERT INTO _listing_history_outcome
SELECT s.slug, 'observation', pg_temp.capture_listing_history(s.brokerage_id,s.external_id,jm.artifact_run_key,'observation',to_jsonb(s))
FROM _src s JOIN _jobmeta jm ON jm.slug=s.slug;
"""

AFTER = r"""
INSERT INTO _listing_history_outcome
SELECT s.slug, 'postimage', pg_temp.capture_listing_history(u.brokerage_id,u.external_id,jm.artifact_run_key,'postimage',NULL)
FROM _up u JOIN _src s USING (brokerage_id,external_id) JOIN _jobmeta jm ON jm.slug=s.slug;
"""

RETIRED_BEFORE = r"""
INSERT INTO _listing_history_outcome
SELECT b.slug, 'retired_preimage', pg_temp.capture_listing_history(r.brokerage_id,r.external_id,jm.artifact_run_key,'preimage',NULL)
FROM _retired r JOIN credeals.cre_brokerages b ON b.id=r.brokerage_id JOIN _jobmeta jm ON jm.slug=b.slug;
"""

RETIRED_AFTER = r"""
INSERT INTO _listing_history_outcome
SELECT b.slug, 'retired_postimage', pg_temp.capture_listing_history(r.brokerage_id,r.external_id,jm.artifact_run_key,'postimage',NULL)
FROM _retired r JOIN credeals.cre_brokerages b ON b.id=r.brokerage_id JOIN _jobmeta jm ON jm.slug=b.slug;
"""
RECEIPT = r"""
\pset format unaligned
\pset tuples_only on
SELECT 'CRE_LISTING_HISTORY ' || jsonb_build_object(
  'schema','cre-listing-history-outcome/v1',
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
                   "child_schema_unavailable", "credential_like_content", "capture_error"}
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